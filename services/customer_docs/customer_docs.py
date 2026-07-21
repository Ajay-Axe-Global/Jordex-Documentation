"""
services/customer_docs/customer_docs.py — Customer Docs Service
================================================================
Handles "04.Customer Docs" Outlook label.
Owns its own Playwright instances with isolated profiles.
Classifies every PDF individually using Gemini.
"""
import os, json, logging, threading, time
from datetime import datetime
from playwright.sync_api import sync_playwright

from config import OUTPUT_DIR, JORDEX_MAPPING, ROUND_ROBIN_BATCH
from extractor import gemini_model, save_result, extract_oi_from_subject
from shared.tracker import Tracker
from shared.helpers import (
    navigate_to_folder, collect_unread, click_row, get_subject,
    download_attachments_to_temp, move_file_to_folder, cleanup_temp,
    subject_folder_fallback,
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

            pdf_files = [f for f in temp_files if f.lower().endswith(".pdf")]

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
                m = re.search(r'(OI\d{4,})', subject, re.IGNORECASE)
                oi_fallback = m.group(1).upper() if m else None
                folder_name = oi_fallback if oi_fallback else subject_folder_fallback(subject)

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

            # secondary_ref: try reference_number from classification, then container_no
            sec_ref = None
            if cust_results:
                for r in cust_results:
                    sec_ref = r.get("reference_number") or r.get("container_no")
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

    def _upload_to_jordex(self, jordex_page, outlook_page, tracker: Tracker, items: list):
        doc_type, display_name = JORDEX_MAPPING[CAT]
        normalize_dashboard_filters(jordex_page)

        for item in items:
            if self._stop_evt.is_set(): break
            query = item.get("mbl") or item.get("folder_name")
            if not query: continue

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
            while row_index < 10:
                success, rows_found = search_and_open(jordex_page, used_ref, row_index=row_index)
                if not success: break
                cust_file_map = build_customer_docs_file_map(item["folder_path"])
                upload_attachments(
                    jordex_page, item["folder_path"], doc_type, display_name,
                    file_map=cust_file_map,
                )
                go_back(jordex_page)
                uploaded = True
                self._uploaded += 1
                row_index += 1
                if rows_found <= row_index: break

            if uploaded:
                tracker.update_status(CAT, item["conv_id"], "uploaded")
