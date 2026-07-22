"""
services/arrival_notice/arrival_notice.py — Arrival Notice Service
===================================================================
ArrivalNoticeService owns:
  - Its own Playwright instance (Outlook + Jordex, separate profile per label)
  - The email processing loop for label "03.Arrival Notice"
  - State management: idle / running / stopping / error

This file is the main entry point for the Arrival Notice service.
Extraction logic lives in extractor.py (same folder).
Shared Gemini engine lives in root extractor.py.

FCS CARRIER EDGE CASE:
  When carrier is FCS / Famous Pacific Shipping, additional post-upload
  steps are performed:
    - Upload with doc_type "Arrival Notice" renamed as "AN"
    - Update Vessel Name on Carrier tab
    - Add Warehouse Address on Destination tab
    - Update Arrival Date on Lane tab
  See fcs_handler.py for the Jordex UI automation.
"""

import os, json, logging, threading, time
from datetime import datetime
from playwright.sync_api import sync_playwright

from config import OUTPUT_DIR, LABELS, JORDEX_MAPPING, ROUND_ROBIN_BATCH
from extractor import gemini_model, save_result
from shared.tracker import Tracker
from shared.helpers import (
    navigate_to_folder, collect_unread, click_row, get_subject,
    download_attachments_to_temp, move_file_to_folder, cleanup_temp,
    subject_folder_fallback, normalize_oi_reference,
    should_skip_multi_attachment,
    mark_as_unread, search_jordex_with_fallback,
)
from outlook.session import OutlookSession
from jordex.login import JordexSession
from jordex.browser import normalize_dashboard_filters, search_and_open, go_back
from jordex.documents import upload_attachments
from services.arrival_notice.extractor import extract_arrival_notice
from services.arrival_notice.fcs_handler import is_fcs_carrier, handle_fcs_post_upload

log = logging.getLogger("service.arrival_notice")

SERVICE_KEY   = "arrival_notice"
OUTLOOK_LABEL = "03.Arrival Notice"
CAT           = "Arrival_Notice"
MODE          = "mbl"

# FCS-specific upload config
FCS_DOC_TYPE     = "Arrival Notice"   # doc_type selector value in Jordex
FCS_DISPLAY_NAME = "AN"               # rename the uploaded file


class ArrivalNoticeService:
    """
    Manages the full lifecycle of the Arrival Notice label:
    Outlook login → email download → Gemini extraction → Jordex upload.

    Each instance has its OWN browser contexts (Outlook + Jordex),
    isolated from all other services.
    """

    def __init__(self):
        self.status    = "idle"       # idle | running | stopping | error
        self.error     = None
        self._thread   = None
        self._stop_evt = threading.Event()
        self._processed = 0
        self._uploaded  = 0
        self.last_run   = None

    # ── Public API (called by routes.py) ───────────────────────────────

    def start(self):
        if self.status == "running":
            return {"ok": False, "message": "Already running"}
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"svc-{SERVICE_KEY}")
        self._thread.start()
        self.status = "running"
        log.info(f"[{SERVICE_KEY}] Service started")
        return {"ok": True, "message": "Started"}

    def stop(self):
        if self.status != "running":
            return {"ok": False, "message": "Not running"}
        self._stop_evt.set()
        self.status = "stopping"
        log.info(f"[{SERVICE_KEY}] Stop requested")
        return {"ok": True, "message": "Stop signal sent"}

    def get_status(self) -> dict:
        tracker = Tracker()
        stats   = tracker.stats(CAT)
        return {
            "service":   SERVICE_KEY,
            "label":     OUTLOOK_LABEL,
            "status":    self.status,
            "error":     self.error,
            "processed": stats.get("total", 0),
            "uploaded":  stats.get("uploaded", 0),
            "last_run":  self.last_run,
            "stats":     stats,
        }

    # ── Internal processing loop ───────────────────────────────────────

    def _run(self):
        pw = outlook_session = jordex_session = None
        try:
            pw              = sync_playwright().start()
            outlook_session = OutlookSession(service_key=SERVICE_KEY, pw=pw)
            jordex_session  = JordexSession(service_key=SERVICE_KEY, pw=pw)

            outlook_page = outlook_session.start()
            jordex_page  = jordex_session.start()
            tracker      = Tracker()

            log.info(f"[{SERVICE_KEY}] Both sessions ready. Starting loop.")

            while not self._stop_evt.is_set():
                self.last_run = datetime.now().isoformat()
                items = self._process_batch(outlook_page, tracker)

                if items:
                    self._upload_to_jordex(jordex_page, outlook_page, tracker, items)

                if self._stop_evt.is_set():
                    break

                # Wait before next pass (checking stop every second)
                for _ in range(ROUND_ROBIN_BATCH * 2):
                    if self._stop_evt.is_set():
                        break
                    time.sleep(1)

        except Exception as e:
            log.error(f"[{SERVICE_KEY}] Fatal error: {e}", exc_info=True)
            self.error  = str(e)
            self.status = "error"
        finally:
            if outlook_session:
                try: outlook_session.close()
                except Exception: pass
            if jordex_session:
                try: jordex_session.close()
                except Exception: pass
            if pw:
                try: pw.stop()
                except Exception: pass
            if self.status != "error":
                self.status = "idle"
            log.info(f"[{SERVICE_KEY}] Service stopped")

    def _process_batch(self, page, tracker: Tracker) -> list:
        """Navigate to AN label, download and extract up to ROUND_ROBIN_BATCH emails."""
        navigate_to_folder(page, OUTLOOK_LABEL)
        msgs = collect_unread(page, tracker, CAT, limit=ROUND_ROBIN_BATCH)

        if not msgs:
            log.info(f"[{SERVICE_KEY}] No new unread emails")
            return []

        base = os.path.join(OUTPUT_DIR, CAT)
        processed_items = []

        for i, msg in enumerate(msgs):
            if self._stop_evt.is_set():
                break

            cid = msg["conv_id"]
            log.info(f"[{SERVICE_KEY}] [{i+1}/{len(msgs)}] Processing email")

            if not click_row(page, cid):
                tracker.mark(CAT, cid, "", "", [], "failed")
                continue

            subject = get_subject(page) or cid[:40]

            # ══════════════════════════════════════════════════════════
            #  Skip emails with multiple PDF attachments
            #  (bulk emails are duplicates of individual ones)
            # ══════════════════════════════════════════════════════════
            if should_skip_multi_attachment(page, max_allowed=1):
                log.info(f"[{SERVICE_KEY}] Skipping multi-attachment email: {subject}")
                tracker.mark(CAT, cid, subject, "", [], "skipped_multi_attach")
                continue
            # ══════════════════════════════════════════════════════════

            temp_files = download_attachments_to_temp(page)

            if not temp_files:
                tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], "no_attachment")
                continue

            # ══════════════════════════════════════════════════════════
            #  Skip emails with Excel attachments
            # ══════════════════════════════════════════════════════════
            if any(f.lower().endswith(('.xls', '.xlsx')) for f in temp_files):
                log.info(f"[{SERVICE_KEY}] Skipping email with Excel attachment: {subject}")
                cleanup_temp(temp_files)
                tracker.mark(CAT, cid, subject, "", [], "skipped_excel")
                continue
            # ══════════════════════════════════════════════════════════

            pdf_files = [f for f in temp_files if f.lower().endswith(".pdf")]

            if pdf_files and gemini_model:
                extraction  = extract_arrival_notice(pdf_files[0], gemini_model, subject=subject)
                folder_name = extraction.get("reference") or subject_folder_fallback(subject)

                # ══════════════════════════════════════════════════════
                #  Normalize OI/MBL reference (fix 0/O confusion)
                # ══════════════════════════════════════════════════════
                folder_name = normalize_oi_reference(folder_name)
                if extraction.get("reference"):
                    extraction["reference"] = normalize_oi_reference(extraction["reference"])
                # ══════════════════════════════════════════════════════

                # Logical duplicate check
                res_path = os.path.join(base, folder_name, "arrival_notice.json")
                if os.path.exists(res_path):
                    try:
                        with open(res_path) as f:
                            old = json.load(f)
                        if old.get("arrival_date_raw") == extraction.get("arrival_date_raw"):
                            log.info(f"[{SERVICE_KEY}] Logical duplicate for {folder_name}, skipping")
                            cleanup_temp(temp_files)
                            tracker.mark(CAT, cid, subject, folder_name, [], "downloaded", mbl=folder_name)
                            continue
                    except Exception:
                        pass
            else:
                extraction  = None
                folder_name = subject_folder_fallback(subject)
                # ══════════════════════════════════════════════════════
                #  Also normalize fallback folder names
                # ══════════════════════════════════════════════════════
                folder_name = normalize_oi_reference(folder_name)
                # ══════════════════════════════════════════════════════

            final_dir = os.path.join(base, folder_name)
            os.makedirs(final_dir, exist_ok=True)

            saved_files = []
            for tmp in temp_files:
                saved = move_file_to_folder(tmp, final_dir)
                if saved:
                    saved_files.append(saved)

            if extraction:
                extraction["subject"] = subject
                save_result(extraction, final_dir, "arrival_notice.json")

            cleanup_temp(temp_files)

            mbl_val = extraction.get("reference") if extraction else None
            sec_ref = extraction.get("container_no") if extraction else None
            tracker.mark(CAT, cid, subject, folder_name, saved_files, "downloaded", mbl=mbl_val, secondary_ref=sec_ref)
            self._processed += 1

            processed_items.append({
                "conv_id":        cid,
                "cat":            CAT,
                "folder_path":    final_dir,
                "folder_name":    folder_name,
                "mbl":            mbl_val,
                "secondary_ref":  sec_ref,
                # ══════════════════════════════════════════════════════
                #  Carry the full extraction dict for FCS post-upload
                # ══════════════════════════════════════════════════════
                "extraction":     extraction,
            })

        return processed_items

    def _upload_to_jordex(self, jordex_page, outlook_page, tracker: Tracker, items: list):
        """Upload downloaded files to Jordex for each processed item."""
        doc_type, display_name = JORDEX_MAPPING[CAT]

        normalize_dashboard_filters(jordex_page)

        for item in items:
            if self._stop_evt.is_set():
                break

            query = item.get("mbl") or item.get("folder_name")
            if not query:
                continue

            # ══════════════════════════════════════════════════════════
            #  Normalize query before Jordex search
            # ══════════════════════════════════════════════════════════
            query = normalize_oi_reference(query)
            # ══════════════════════════════════════════════════════════

            if query.startswith("OE"):
                log.info(f"[{SERVICE_KEY}] Skipping OE reference: {query}")
                tracker.update_status(CAT, item["conv_id"], "Skipped")
                continue

            success, used_ref, rows_found = search_jordex_with_fallback(
                jordex_page=jordex_page,
                outlook_page=outlook_page,
                primary_ref=query,
                secondary_ref=item.get("secondary_ref"),
                conv_id=item["conv_id"],
                tracker=tracker,
                cat=CAT,
                service_key=SERVICE_KEY,
                search_fn=search_and_open,
            )
            if not success:
                continue

            # ══════════════════════════════════════════════════════════
            #  Determine if this is an FCS carrier
            # ══════════════════════════════════════════════════════════
            extraction = item.get("extraction") or {}
            fcs_mode = extraction.get("is_fcs", False)
            if fcs_mode:
                log.info(f"[{SERVICE_KEY}] FCS carrier detected — using Arrival Notice doc type")
                upload_doc_type = FCS_DOC_TYPE
                upload_display  = FCS_DISPLAY_NAME
            else:
                upload_doc_type = doc_type
                upload_display  = display_name
            # ══════════════════════════════════════════════════════════

            row_index = 0
            uploaded  = False

            try:
                while row_index < 10:  # max 10 rows per shipment
                    success, rows_found = search_and_open(jordex_page, used_ref, row_index=row_index)
                    if not success:
                        break

                    # Arrival date guard — skip if Jordex date differs > 5 days
                    skip_an = self._check_arrival_date_mismatch(jordex_page, item)
                    if skip_an:
                        go_back(jordex_page)
                        break

                    # ══════════════════════════════════════════════════════
                    #  Upload with FCS-specific doc type if applicable
                    # ══════════════════════════════════════════════════════
                    upload_attachments(jordex_page, item["folder_path"],
                                       upload_doc_type, upload_display)

                    # ══════════════════════════════════════════════════════
                    #  FCS POST-UPLOAD: update Carrier, Destination, Lane
                    # ══════════════════════════════════════════════════════
                    if fcs_mode:
                        try:
                            handle_fcs_post_upload(jordex_page, extraction)
                        except Exception as e:
                            log.warning(f"[{SERVICE_KEY}] FCS post-upload failed: {e}")
                    # ══════════════════════════════════════════════════════

                    go_back(jordex_page)
                    uploaded = True
                    self._uploaded += 1
                    row_index += 1

                    if rows_found <= row_index:
                        break
            except Exception as e:
                log.error(f"[{SERVICE_KEY}] Error during upload loop for {query}: {e}", exc_info=True)
            finally:
                if uploaded:
                    tracker.update_status(CAT, item["conv_id"], "uploaded")
                else:
                    log.warning(f"[{SERVICE_KEY}] Could not open/upload shipment for {query}")

    def _check_arrival_date_mismatch(self, jordex_page, item: dict) -> bool:
        """Return True if Jordex arrival date differs from extracted date by > 5 days."""
        try:
            jordex_date_str = jordex_page.evaluate("""() => {
                const els = [...document.querySelectorAll("strong")];
                const strong = els.find(el => el.textContent.trim() === "Arrival");
                if (strong && strong.parentElement) {
                    const span = strong.parentElement.querySelector(".routing-update-date");
                    return span ? span.textContent.trim() : null;
                }
                return null;
            }""")

            json_path = os.path.join(item["folder_path"], "arrival_notice.json")
            if jordex_date_str and os.path.exists(json_path):
                with open(json_path) as f:
                    an_data = json.load(f)
                ext_date_str = an_data.get("arrival_date")
                if ext_date_str:
                    # Jordex may show "31 Jul 26" (2-digit year) or "31 Jul 2026" (4-digit year)
                    j_date = None
                    for fmt_j in ("%d %b %y", "%d %b %Y"):
                        try:
                            j_date = datetime.strptime(jordex_date_str, fmt_j)
                            break
                        except ValueError:
                            continue
                    if j_date is None:
                        log.warning(f"[{SERVICE_KEY}] Could not parse Jordex date: '{jordex_date_str}'")
                        return False

                    # Our arrival_date is always DD/MM/YY or DD/MM/YYYY
                    fmt_e = "%d/%m/%Y" if len(ext_date_str.split("/")[-1]) == 4 else "%d/%m/%y"
                    e_date = datetime.strptime(ext_date_str, fmt_e)
                    diff   = abs((j_date - e_date).days)
                    if diff > 5:
                        log.warning(f"[{SERVICE_KEY}] Arrival date mismatch {diff}d "
                                    f"(Jordex: '{jordex_date_str}' vs Extracted: '{ext_date_str}') — skipping upload")
                        return True
        except Exception as e:
            log.error(f"[{SERVICE_KEY}] Arrival date check error: {e}")
        return False