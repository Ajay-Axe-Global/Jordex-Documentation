"""
services/customer_docs/customer_docs.py — Customer Docs Service
================================================================
Handles "04.Customer Docs" Outlook label.
Owns its own Playwright instances with isolated profiles.
Classifies every PDF individually using Gemini.
"""
import os, json, glob, logging, threading, time
from datetime import datetime
from playwright.sync_api import sync_playwright

from config import OUTPUT_DIR, JORDEX_MAPPING, ROUND_ROBIN_BATCH
from extractor import gemini_model, save_result, extract_oi_from_subject
from shared.tracker import Tracker
from shared.helpers import (
    navigate_to_folder, collect_unread, click_row, get_subject,
    download_attachments_to_temp, move_file_to_folder, cleanup_temp,
    subject_folder_fallback, normalize_oi_reference,
    mark_as_unread, search_jordex_with_fallback,
)
from outlook.session import OutlookSession
from jordex.login import JordexSession
from jordex.browser import normalize_dashboard_filters, search_and_open, go_back
from jordex.documents import upload_attachments, build_customer_docs_file_map
from services.customer_docs.extractor import classify_all_customer_docs

log = logging.getLogger("service.customer_docs")

SERVICE_KEY   = "customer_docs"
OUTLOOK_LABEL = "04.Customer Docs"
CAT           = "Customer_Docs"


class CustomerDocsService:
    def __init__(self):
        self.status     = "idle"
        self.error      = None
        self._thread    = None
        self._stop_evt  = threading.Event()
        self._processed = 0
        self._uploaded  = 0
        self.last_run   = None

    def start(self):
        if self.status == "running":
            return {"ok": False, "message": "Already running"}
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"svc-{SERVICE_KEY}")
        self._thread.start()
        self.status = "running"
        return {"ok": True, "message": "Started"}

    def stop(self):
        if self.status != "running":
            return {"ok": False, "message": "Not running"}
        self._stop_evt.set()
        self.status = "stopping"
        return {"ok": True, "message": "Stop signal sent"}

    def get_status(self) -> dict:
        tracker = Tracker()
        stats   = tracker.stats(CAT)
        return {
            "service":   SERVICE_KEY,
            "label":     OUTLOOK_LABEL,
            "status":    self.status,
            "error":     self.error,
            "processed": stats.get("total", 0),      # from tracking.json
            "uploaded":  stats.get("uploaded", 0),   # from tracking.json
            "last_run":  self.last_run,
            "stats":     stats,
        }

    def _run(self):
        pw = outlook_session = jordex_session = None
        try:
            pw              = sync_playwright().start()
            outlook_session = OutlookSession(service_key=SERVICE_KEY, pw=pw)
            jordex_session  = JordexSession(service_key=SERVICE_KEY, pw=pw)
            outlook_page    = outlook_session.start()
            jordex_page     = jordex_session.start()
            tracker         = Tracker()

            while not self._stop_evt.is_set():
                self.last_run = datetime.now().isoformat()
                items = self._process_batch(outlook_page, tracker)
                if items:
                    self._upload_to_jordex(jordex_page, outlook_page, tracker, items)
                for _ in range(ROUND_ROBIN_BATCH * 2):
                    if self._stop_evt.is_set(): break
                    time.sleep(1)

        except Exception as e:
            log.error(f"[{SERVICE_KEY}] Fatal: {e}", exc_info=True)
            self.error  = str(e)
            self.status = "error"
        finally:
            for s in [outlook_session, jordex_session]:
                if s:
                    try: s.close()
                    except Exception: pass
            if pw:
                try: pw.stop()
                except Exception: pass
            if self.status != "error":
                self.status = "idle"

    def _process_batch(self, page, tracker: Tracker) -> list:
        navigate_to_folder(page, OUTLOOK_LABEL)
        msgs = collect_unread(page, tracker, CAT, limit=ROUND_ROBIN_BATCH)
        if not msgs:
            return []

        base            = os.path.join(OUTPUT_DIR, CAT)
        processed_items = []

        for msg in msgs:
            if self._stop_evt.is_set(): break
            cid = msg["conv_id"]

            if not click_row(page, cid):
                tracker.mark(CAT, cid, "", "", [], "failed")
                continue

            subject    = get_subject(page) or cid[:40]
            temp_files = download_attachments_to_temp(page)

            if not temp_files:
                tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], "no_attachment")
                continue

            pdf_files = [f for f in temp_files if f.lower().endswith((".pdf", ".jpg", ".jpeg", ".png"))]

            if pdf_files and gemini_model:
                cust_results = classify_all_customer_docs(pdf_files, gemini_model=gemini_model, subject=subject)
                folder_name  = None
                for r in cust_results:
                    fn = r.get("shared_folder_name") or r.get("folder_name")
                    if fn:
                        folder_name = fn
                        break

                extraction = {
                    "doc_type":       "customer_docs",
                    "classifications": cust_results,
                    "folder_name":    folder_name,
                    "source_files":   [os.path.basename(p) for p in pdf_files],
                    "extracted_at":   datetime.now().isoformat(),
                }
            else:
                cust_results = []
                folder_name  = None
                extraction   = None

            if not folder_name:
                import re
                m = re.search(r'(OI\d{4,}|0[Ii]\d{4,}|01\d{5,})', subject, re.IGNORECASE)
                oi_fallback = m.group(1).upper() if m else None
                folder_name = normalize_oi_reference(oi_fallback) if oi_fallback else subject_folder_fallback(subject)
            else:
                folder_name = normalize_oi_reference(folder_name)

            final_dir = os.path.join(base, folder_name)
            os.makedirs(final_dir, exist_ok=True)
            saved_files = []
            for tmp in temp_files:
                saved = move_file_to_folder(tmp, final_dir)
                if saved: saved_files.append(saved)

            # Save per-PDF classification JSONs
            if cust_results:
                self._save_classifications(final_dir, cust_results)

            if extraction:
                extraction["subject"] = subject
                save_result(extraction, final_dir)

            cleanup_temp(temp_files)

            # secondary_ref: whichever of container_no / reference_number was NOT
            # already used as the primary (folder_name), so the fallback search
            # actually tries a different value instead of repeating the primary.
            sec_ref = None
            if cust_results:
                for r in cust_results:
                    primary = r.get("folder_name")
                    for candidate in (r.get("container_no"), r.get("reference_number")):
                        if candidate and candidate != primary:
                            sec_ref = candidate
                            break
                    if sec_ref:
                        break
            
            tracker.mark(CAT, cid, subject, folder_name, saved_files, "downloaded", secondary_ref=sec_ref)
            self._processed += 1
            
            processed_items.append({
                "conv_id":       cid,
                "cat":           CAT,
                "folder_path":   final_dir,
                "folder_name":   folder_name,
                "mbl":           None,
                "secondary_ref": sec_ref,
            })

        return processed_items

    def _save_classifications(self, folder_path: str, results: list):
        for r in results:
            source = r.get("source_file", "")
            if not source: continue
            stem      = os.path.splitext(source)[0]
            json_path = os.path.join(folder_path, f"{stem}_classification.json")
            try:
                with open(json_path, "w") as f:
                    json.dump(r, f, indent=2, default=str)
            except Exception as e:
                log.warning(f"  Classification save failed {source}: {e}")

    def _count_bl_types(self, folder_path: str) -> tuple[int, int]:
        """Count HBL/MBL docs for this shipment by reading the saved
        <stem>_classification.json files (one per uploaded PDF)."""
        hbl_count = mbl_count = 0
        for json_path in glob.glob(os.path.join(folder_path, "*_classification.json")):
            try:
                with open(json_path) as f:
                    doc_type = (json.load(f).get("doc_type") or "").strip().upper()
            except Exception:
                continue
            if doc_type == "HOUSE BILL OF LADING":
                hbl_count += 1
            elif doc_type == "MASTER BILL OF LADING":
                mbl_count += 1
        return hbl_count, mbl_count

    def _check_carrier_bl_fields(self, jordex_page) -> bool:
        """
        On the currently-open shipment detail page, click the Carrier tab
        and check whether Master BL / House BL number fields are populated.
        Returns True if either field is empty (needs manual review).
        """
        try:
            jordex_page.get_by_role("tab", name="Carrier").click()
            jordex_page.wait_for_timeout(1500)
            master_val = jordex_page.locator("#masterBLNumber").input_value().strip()
            house_val  = jordex_page.locator("#houseBLNumber").input_value().strip()
            log.info(
                f"[{SERVICE_KEY}]   Carrier tab: masterBLNumber='{master_val}' "
                f"houseBLNumber='{house_val}'"
            )
            return not master_val or not house_val
        except Exception as e:
            log.warning(f"[{SERVICE_KEY}]   Could not read Carrier tab BL fields: {e}")
            return False

    def _upload_to_jordex(self, jordex_page, outlook_page, tracker: Tracker, items: list):
        doc_type, display_name = JORDEX_MAPPING[CAT]


        # Deduplicate: track folder_names already uploaded this batch
        uploaded_folders: set[str] = set()

        for item in items:
            if self._stop_evt.is_set(): break
            query = item.get("mbl") or item.get("folder_name")
            if not query: continue

            folder_name = item.get("folder_name") or query
            if folder_name in uploaded_folders:
                log.info(
                    f"[{SERVICE_KEY}] Skipping duplicate folder '{folder_name}' "
                    f"(conv_id={item['conv_id'][:20]}…) — already uploaded this batch"
                )
                tracker.update_status(CAT, item["conv_id"], "uploaded")
                continue

            if tracker.is_uploaded_elsewhere(CAT, folder_name=folder_name, mbl=item.get("mbl"),
                                              exclude_conv_id=item["conv_id"]):
                log.info(f"[{SERVICE_KEY}] Skipping '{folder_name}' — already uploaded to Jordex under a different email")
                tracker.update_status(CAT, item["conv_id"], "uploaded")
                uploaded_folders.add(folder_name)
                continue

            query = normalize_oi_reference(query)
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

            row_index = 0
            uploaded  = False
            carrier_fields_empty = False

            # If this shipment has at least one HBL AND at least one MBL among
            # its docs, verify after upload that Jordex's Carrier tab actually
            # shows both B/L numbers — catches cases where the docs attached
            # fine but the shipment record itself wasn't populated correctly.
            hbl_count, mbl_count = self._count_bl_types(item["folder_path"])
            check_carrier_tab = hbl_count >= 1 and mbl_count >= 1

            # Only iterate multiple rows if the search reference is a reliable
            # OI number or SCAC-prefixed BL (e.g. "OI2617257", "HLCU...").
            # If the search fell back to a short/partial secondary_ref
            # (e.g. "1406"), uploading to all rows would broadcast the document
            # to random shipments that don't belong to this email.
            import re as _re
            _ref_is_reliable = bool(
                _re.match(r'^OI\d{4,}', used_ref, _re.IGNORECASE) or
                (_re.match(r'^[A-Z]{4}', used_ref) and len(used_ref) >= 10)
            )
            _max_rows = 10 if _ref_is_reliable else 1
            if not _ref_is_reliable:
                log.info(
                    f"[{SERVICE_KEY}] Ref '{used_ref}' is not a reliable OI/BL — "
                    f"uploading to row 0 only (preventing broadcast to unrelated shipments)"
                )

            try:
                while row_index < _max_rows:
                    success, rows_found = search_and_open(jordex_page, used_ref, row_index=row_index)
                    if not success: break
                    cust_file_map = build_customer_docs_file_map(item["folder_path"])
                    row_ok = upload_attachments(
                        jordex_page, item["folder_path"], doc_type, display_name,
                        file_map=cust_file_map,
                    )
                    if row_ok and check_carrier_tab:
                        if self._check_carrier_bl_fields(jordex_page):
                            carrier_fields_empty = True
                            log.warning(
                                f"[{SERVICE_KEY}] Carrier tab BL field(s) empty for "
                                f"'{folder_name}' (row {row_index}) despite "
                                f"{hbl_count} HBL + {mbl_count} MBL doc(s) uploaded"
                            )
                    go_back(jordex_page)
                    if row_ok:
                        uploaded = True
                        self._uploaded += 1
                    else:
                        log.warning(
                            f"[{SERVICE_KEY}] Upload not confirmed for row {row_index} "
                            f"(query={query}) — will retry next run"
                        )
                    row_index += 1
                    if rows_found <= row_index: break
            except Exception as e:
                log.error(f"[{SERVICE_KEY}] Error during upload loop for {query}: {e}", exc_info=True)
            finally:
                if uploaded and carrier_fields_empty:
                    # Docs are attached (don't re-upload next run), but the
                    # shipment's Carrier tab wasn't populated — needs a human.
                    tracker.update_status(CAT, item["conv_id"], "carrier_fields_empty")
                    uploaded_folders.add(folder_name)
                    try:
                        mark_as_unread(outlook_page, item["conv_id"])
                        log.info(
                            f"[{SERVICE_KEY}] Marked '{query}' unread — Carrier tab "
                            f"BL field(s) empty, needs manual review"
                        )
                    except Exception as e:
                        log.warning(f"[{SERVICE_KEY}] Could not mark unread for {query}: {e}")
                elif uploaded:
                    tracker.update_status(CAT, item["conv_id"], "uploaded")
                    uploaded_folders.add(folder_name)
                else:
                    log.warning(f"[{SERVICE_KEY}] Could not open/upload shipment for {query}")
                    try:
                        mark_as_unread(outlook_page, item["conv_id"])
                        log.info(f"[{SERVICE_KEY}] Marked '{query}' unread for retry next run")
                    except Exception as e:
                        log.warning(f"[{SERVICE_KEY}] Could not mark unread for {query}: {e}")