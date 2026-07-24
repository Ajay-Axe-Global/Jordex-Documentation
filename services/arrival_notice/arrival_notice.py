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

from shared import tracker
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
    is_page_responsive, recover_page,
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
        pw = None
        MAX_RESTARTS = 3      # restart sessions up to 3 times before giving up
    
        try:
            pw = sync_playwright().start()
            restart_count = 0
    
            # ── Session startup (extracted so we can call it again) ──────
            def start_sessions():
                outlook_s = OutlookSession(service_key=SERVICE_KEY, pw=pw)
                jordex_s  = JordexSession(service_key=SERVICE_KEY, pw=pw)
                o_page    = outlook_s.start()
                j_page    = jordex_s.start()
                log.info(f"[{SERVICE_KEY}] Both sessions ready.")
                return outlook_s, jordex_s, o_page, j_page
    
            outlook_session, jordex_session, outlook_page, jordex_page = start_sessions()
            tracker = Tracker()
    
            log.info(f"[{SERVICE_KEY}] Starting processing loop.")
    
            consecutive_empty = 0
    
            while not self._stop_evt.is_set():
                self.last_run = datetime.now().isoformat()
                items = self._process_batch(outlook_page, tracker)
    
                if items:
                    consecutive_empty = 0
                    self._upload_to_jordex(jordex_page, outlook_page, tracker, items)
                else:
                    consecutive_empty += 1
    
                    # ── After 2 consecutive empty batches, restart sessions ──
                    if consecutive_empty >= 2:
                        restart_count += 1
                        if restart_count > MAX_RESTARTS:
                            log.error(f"[{SERVICE_KEY}] {MAX_RESTARTS} session restarts "
                                    f"exhausted — stopping service")
                            self.error = (f"Page unresponsive after {MAX_RESTARTS} "
                                        f"session restarts")
                            self.status = "error"
                            break
    
                        log.warning(f"[{SERVICE_KEY}] {consecutive_empty} consecutive "
                                    f"empty batches — restarting sessions "
                                    f"(attempt {restart_count}/{MAX_RESTARTS})")
    
                        # Close old sessions
                        for s in (outlook_session, jordex_session):
                            try:
                                s.close()
                            except Exception:
                                pass
    
                        # Start fresh sessions
                        try:
                            (outlook_session, jordex_session,
                            outlook_page, jordex_page) = start_sessions()
                            consecutive_empty = 0
                            log.info(f"[{SERVICE_KEY}] Session restart SUCCEEDED")
                        except Exception as e:
                            log.error(f"[{SERVICE_KEY}] Session restart FAILED: {e}",
                                    exc_info=True)
                            self.error = f"Session restart failed: {e}"
                            self.status = "error"
                            break
    
                if self._stop_evt.is_set():
                    break
    
                for _ in range(ROUND_ROBIN_BATCH * 2):
                    if self._stop_evt.is_set():
                        break
                    time.sleep(1)
    
        except Exception as e:
            log.error(f"[{SERVICE_KEY}] Fatal error: {e}", exc_info=True)
            self.error  = str(e)
            self.status = "error"
        finally:
            # Close everything
            for obj in (outlook_session, jordex_session):
                try:
                    obj.close()
                except Exception:
                    pass
            if pw:
                try:
                    pw.stop()
                except Exception:
                    pass
            if self.status != "error":
                self.status = "idle"
            log.info(f"[{SERVICE_KEY}] Service stopped")

    def _process_batch(self, page, tracker) -> list:
        """Navigate to AN label, download and extract up to ROUND_ROBIN_BATCH emails."""
    
        # ── Navigate to folder (with recovery on failure) ────────────────
        try:
            navigate_to_folder(page, OUTLOOK_LABEL)
        except Exception as nav_err:
            log.warning(f"[{SERVICE_KEY}] navigate_to_folder failed: {nav_err}")
            # NOW try recovery — the page actually failed to respond
            if recover_page(page, SERVICE_KEY, wait_selector="#MailList"):
                try:
                    navigate_to_folder(page, OUTLOOK_LABEL)
                except Exception as retry_err:
                    log.error(f"[{SERVICE_KEY}] navigate_to_folder failed after recovery: {retry_err}")
                    return []
            else:
                log.error(f"[{SERVICE_KEY}] Page recovery failed — skipping batch")
                return []
    
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
    
            # Skip emails with multiple PDF attachments
            if should_skip_multi_attachment(page, max_allowed=1):
                log.info(f"[{SERVICE_KEY}] Skipping multi-attachment email: {subject}")
                tracker.mark(CAT, cid, subject, "", [], "skipped_multi_attach")
                continue
    
            temp_files = download_attachments_to_temp(page)
    
            if not temp_files:
                # Distinguish "no attachment at all" from "download failed"
                has_attachments = not should_skip_multi_attachment(page, max_allowed=999)
                if has_attachments:
                    status = "download_failed"
                    log.warning(f"[{SERVICE_KEY}] Attachments exist but download failed: {subject}")
                    mark_as_unread(page, cid)
                else:
                    status = "no_attachment"
    
                tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], status)
                continue
    
            # Skip emails with Excel attachments
            if any(f.lower().endswith(('.xls', '.xlsx')) for f in temp_files):
                log.info(f"[{SERVICE_KEY}] Skipping email with Excel attachment: {subject}")
                cleanup_temp(temp_files)
                tracker.mark(CAT, cid, subject, "", [], "skipped_excel")
                continue
    
            pdf_files = [f for f in temp_files if f.lower().endswith(".pdf")]
    
            if pdf_files and gemini_model:
                extraction  = extract_arrival_notice(pdf_files[0], gemini_model, subject=subject)
                folder_name = extraction.get("reference") or subject_folder_fallback(subject)
    
                folder_name = normalize_oi_reference(folder_name)
                if extraction.get("reference"):
                    extraction["reference"] = normalize_oi_reference(extraction["reference"])
    
                # Logical duplicate check
                res_path = os.path.join(base, folder_name, "arrival_notice.json")
                if os.path.exists(res_path):
                    try:
                        with open(res_path) as f:
                            old = json.load(f)
                        if old.get("arrival_date_raw") == extraction.get("arrival_date_raw"):
                            log.info(f"[{SERVICE_KEY}] Logical duplicate for {folder_name}, skipping")
                            cleanup_temp(temp_files)
                            tracker.mark(CAT, cid, subject, folder_name, [], "downloaded",
                                        mbl=folder_name, carrier_code=extraction.get("carrier_code"))
                            continue
                    except Exception:
                        pass
            else:
                extraction  = None
                folder_name = subject_folder_fallback(subject)
                folder_name = normalize_oi_reference(folder_name)
    
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
            carrier_code = extraction.get("carrier_code") if extraction else None
            tracker.mark(CAT, cid, subject, folder_name, saved_files, "downloaded",
                        mbl=mbl_val, secondary_ref=sec_ref, carrier_code=carrier_code)
            self._processed += 1
    
            processed_items.append({
                "conv_id":        cid,
                "cat":            CAT,
                "folder_path":    final_dir,
                "folder_name":    folder_name,
                "mbl":            mbl_val,
                "secondary_ref":  sec_ref,
                "extraction":     extraction,
            })
    
        return processed_items

    def _upload_to_jordex(self, jordex_page, outlook_page, tracker, items: list):
        """Upload downloaded files to Jordex — no double search."""
        doc_type, display_name = JORDEX_MAPPING[CAT]
        uploaded_folders: set[str] = set()

        for item in items:
            if self._stop_evt.is_set():
                break

            query = item.get("mbl") or item.get("folder_name")
            if not query:
                continue

            query = normalize_oi_reference(query)

            if query.startswith("OE"):
                log.info(f"[{SERVICE_KEY}] Skipping OE reference: {query}")
                tracker.update_status(CAT, item["conv_id"], "Skipped")
                continue

            folder_name = item.get("folder_name") or query
            if folder_name in uploaded_folders:
                log.info(f"[{SERVICE_KEY}] Skipping duplicate folder '{folder_name}'")
                tracker.update_status(CAT, item["conv_id"], "uploaded")
                continue

            if tracker.is_uploaded_elsewhere(CAT, folder_name=folder_name, mbl=item.get("mbl"),
                                              exclude_conv_id=item["conv_id"]):
                log.info(f"[{SERVICE_KEY}] Skipping '{folder_name}' — already uploaded under a different email")
                tracker.update_status(CAT, item["conv_id"], "uploaded")
                uploaded_folders.add(folder_name)
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

            extraction = item.get("extraction") or {}
            fcs_mode = extraction.get("is_fcs", False)
            if fcs_mode:
                log.info(f"[{SERVICE_KEY}] FCS carrier detected — using Arrival Notice doc type")
                upload_doc_type = FCS_DOC_TYPE
                upload_display  = FCS_DISPLAY_NAME
            else:
                upload_doc_type = doc_type
                upload_display  = display_name

            row_index = 0
            uploaded  = False
            mismatch_found = False

            try:
                while row_index < rows_found:
                    success, current_rows = search_and_open(jordex_page, used_ref, row_index=row_index)
                    if not success:
                        break

                    skip_an = self._check_arrival_date_mismatch(jordex_page, item)
                    if skip_an:
                        tracker.update_status(CAT, item["conv_id"], "Data Mismatch")
                        mark_as_unread(outlook_page, item["conv_id"])
                        mismatch_found = True
                        go_back(jordex_page)
                        break

                    upload_attachments(jordex_page, item["folder_path"],
                                       upload_doc_type, upload_display)

                    if fcs_mode:
                        try:
                            handle_fcs_post_upload(jordex_page, extraction)
                        except Exception as e:
                            log.warning(f"[{SERVICE_KEY}] FCS post-upload failed: {e}")

                    go_back(jordex_page)
                    uploaded = True
                    self._uploaded += 1
                    row_index += 1

            except Exception as e:
                log.error(f"[{SERVICE_KEY}] Error during upload loop for {query}: {e}",
                          exc_info=True)
            finally:
                if uploaded:
                    tracker.update_status(CAT, item["conv_id"], "uploaded")
                    uploaded_folders.add(folder_name)
                elif not mismatch_found:
                    log.warning(f"[{SERVICE_KEY}] Could not open/upload shipment for {query}")
                    
    def _check_arrival_date_mismatch(self, jordex_page, item: dict) -> bool:
        """
        Return True if Jordex arrival date is OUTSIDE ±5 days of extracted date.
    
        Safety net: if both DD/MM and MM/DD interpretations are possible,
        pick the one closest to Jordex (handles pre-fix data with swapped dates).
        """
        try:
            # ── Step 1: Read Jordex arrival date from UI ─────────────────
            jordex_date_str = jordex_page.evaluate("""() => {
                const els = [...document.querySelectorAll("strong")];
                const strong = els.find(el => el.textContent.trim() === "Arrival");
                if (strong && strong.parentElement) {
                    const span = strong.parentElement.querySelector(".routing-update-date");
                    return span ? span.textContent.trim() : null;
                }
                return null;
            }""")
    
            if not jordex_date_str:
                log.info(f"[{SERVICE_KEY}] No Jordex arrival date found — skipping check")
                return False
    
            # ── Step 2: Parse Jordex date ────────────────────────────────
            j_date = None
            for fmt_j in ("%d %b %y", "%d %b %Y", "%d-%b-%y", "%d-%b-%Y",
                        "%d %B %y", "%d %B %Y"):
                try:
                    j_date = datetime.strptime(jordex_date_str.strip(), fmt_j)
                    break
                except ValueError:
                    continue
            if j_date is None:
                log.warning(f"[{SERVICE_KEY}] Could not parse Jordex date: '{jordex_date_str}'")
                return False
    
            # ── Step 3: Read extracted arrival date from JSON ────────────
            json_path = os.path.join(item["folder_path"], "arrival_notice.json")
            if not os.path.exists(json_path):
                log.info(f"[{SERVICE_KEY}] No arrival_notice.json — skipping date check")
                return False
    
            with open(json_path) as f:
                an_data = json.load(f)
    
            ext_date_str = an_data.get("arrival_date")
            if not ext_date_str:
                log.info(f"[{SERVICE_KEY}] No extracted arrival_date — skipping check")
                return False
    
            # ── Step 4: Parse extracted date — try DD/MM AND MM/DD ───────
            ext_date_str = ext_date_str.strip()
            parts = ext_date_str.replace("-", "/").split("/")
    
            e_date = None
            e_date_swapped = None  # the MM/DD interpretation
    
            if len(parts) == 3:
                a, b, y = parts
                # Determine year
                if len(y) == 4:
                    year_val = int(y)
                elif len(y) == 2:
                    year_val = 2000 + int(y)
                else:
                    year_val = int(y)
    
                a_int, b_int = int(a), int(b)
    
                # Primary: DD/MM/YY (European, our intended format)
                if 1 <= b_int <= 12 and 1 <= a_int <= 31:
                    try:
                        e_date = datetime(year_val, b_int, a_int)
                    except ValueError:
                        pass
    
                # Secondary: MM/DD/YY (if date was stored before the fix)
                if 1 <= a_int <= 12 and 1 <= b_int <= 31:
                    try:
                        e_date_swapped = datetime(year_val, a_int, b_int)
                    except ValueError:
                        pass
    
            # Fallback: try common formats directly
            if e_date is None and e_date_swapped is None:
                for fmt_e in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d",
                            "%d-%m-%Y", "%d-%m-%y", "%m/%d/%Y", "%m/%d/%y"):
                    try:
                        e_date = datetime.strptime(ext_date_str, fmt_e)
                        break
                    except ValueError:
                        continue
    
            if e_date is None and e_date_swapped is None:
                log.warning(f"[{SERVICE_KEY}] Could not parse extracted date: '{ext_date_str}'")
                return False
    
            # ── Step 5: Pick the interpretation closest to Jordex ────────
            #  This handles BOTH correctly-normalized dates AND pre-fix
            #  dates that have month/day swapped.
            candidates = []
            if e_date is not None:
                candidates.append(("DD/MM", e_date, abs((j_date - e_date).days)))
            if e_date_swapped is not None and e_date_swapped != e_date:
                candidates.append(("MM/DD", e_date_swapped, abs((j_date - e_date_swapped).days)))
    
            # Sort by diff — closest to Jordex wins
            candidates.sort(key=lambda x: x[2])
            chosen_label, chosen_date, abs_diff = candidates[0]
    
            diff_days = (j_date - chosen_date).days  # signed for logging
    
            # ── Step 6: Log clearly ──────────────────────────────────────
            if len(candidates) > 1 and candidates[0][2] != candidates[1][2]:
                log.info(f"[{SERVICE_KEY}] Date ambiguity resolved: "
                        f"'{ext_date_str}' → {chosen_label} interpretation "
                        f"({chosen_date.strftime('%d/%m/%Y')}) is {abs_diff}d from Jordex, "
                        f"other interpretation is {candidates[1][2]}d away")
    
            log.info(f"[{SERVICE_KEY}] Date comparison: "
                    f"Extracted='{ext_date_str}' ({chosen_date.strftime('%d/%m/%Y')}) | "
                    f"Jordex='{jordex_date_str}' ({j_date.strftime('%d/%m/%Y')}) | "
                    f"Diff={diff_days:+d} days")
    
            # ── Step 7: Apply ±5 day window ──────────────────────────────
            if abs_diff > 5:
                log.warning(f"[{SERVICE_KEY}] MISMATCH: {abs_diff} days apart "
                            f"(outside ±5 day window) — skipping upload")
                return True  # mismatch
            else:
                log.info(f"[{SERVICE_KEY}] Date OK: {abs_diff} days apart")
                return False  # proceed
    
        except Exception as e:
            log.error(f"[{SERVICE_KEY}] Arrival date check error: {e}", exc_info=True)
            return False