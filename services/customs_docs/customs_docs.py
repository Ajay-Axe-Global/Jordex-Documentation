"""
services/customs_docs/customs_docs.py — Customs Docs Service
=============================================================
Handles "01.Customs docs" Outlook label.
Owns its own Playwright instances with isolated profiles.
"""
import os, json, logging, threading, time, shutil
from datetime import datetime
from playwright.sync_api import sync_playwright

from config import OUTPUT_DIR, JORDEX_MAPPING, ROUND_ROBIN_BATCH
from extractor import gemini_model, save_result, extract_oi_from_subject
from shared.tracker import Tracker
from shared.helpers import (
    navigate_to_folder, collect_unread, click_row, get_subject,
    download_attachments_to_temp, move_file_to_folder, cleanup_temp,
    subject_folder_fallback,
)
from outlook.session import OutlookSession
from jordex.login import JordexSession
from jordex.browser import normalize_dashboard_filters, search_and_open, go_back
from jordex.documents import upload_attachments, CUSTOMS_DOCS_FILE_MAP
from services.customs_docs.extractor import classify_customs_doc

log = logging.getLogger("service.customs_docs")

SERVICE_KEY   = "customs_docs"
OUTLOOK_LABEL = "01.Customs docs"
CAT           = "Customs_Docs"


class CustomsDocsService:
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
                    self._upload_to_jordex(jordex_page, tracker, items)
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

            # Customs Docs: folder name = OI from subject
            oi          = extract_oi_from_subject(subject)
            folder_name = oi if oi else subject_folder_fallback(subject)
            extraction  = {
                "oi_number": oi, "subject": subject,
                "source_file": os.path.basename(temp_files[0]),
                "extracted_at": datetime.now().isoformat(),
            }

            final_dir = os.path.join(base, folder_name)
            os.makedirs(final_dir, exist_ok=True)
            saved_files = []
            for tmp in temp_files:
                saved = move_file_to_folder(tmp, final_dir)
                if saved: saved_files.append(saved)

            # Classify and rename to dms_tax / dms_imp_ttw
            if saved_files:
                saved_files, tax_info = self._classify_and_rename(final_dir, saved_files)
                if tax_info:
                    extraction.update(tax_info)

            save_result(extraction, final_dir, "customs_result.json")
            cleanup_temp(temp_files)

            tracker.mark(CAT, cid, subject, folder_name, saved_files, "downloaded")
            self._processed += 1
            processed_items.append({
                "conv_id":     cid,
                "cat":         CAT,
                "folder_path": final_dir,
                "folder_name": folder_name,
                "oi_number":   oi,
                "mbl":         None,
            })

        return processed_items

    def _classify_and_rename(self, folder_path: str, saved_filenames: list) -> tuple:
        """Rename PDFs to dms_tax.pdf / dms_imp_ttw.pdf based on content."""
        pdf_files   = [f for f in saved_filenames if f.lower().endswith(".pdf")]
        tax_info    = {}
        rename_map  = {}
        assigned    = {"dms_tax": False, "dms_imp_ttw": False}

        for fname in pdf_files:
            full_path = os.path.join(folder_path, fname)
            doc_info  = classify_customs_doc(full_path, gemini_model=gemini_model)
            doc_type  = doc_info.get("doc_type", "unknown")

            if doc_type == "dms_tax" and not assigned["dms_tax"]:
                rename_map[fname] = "dms_tax.pdf"
                assigned["dms_tax"] = True
                tax_info["status"] = doc_info.get("status")
                tax_info["amount_verschuldigd"] = doc_info.get("amount_verschuldigd")
            elif doc_type == "dms_imp_ttw" and not assigned["dms_imp_ttw"]:
                rename_map[fname] = "dms_imp_ttw.pdf"
                assigned["dms_imp_ttw"] = True
            else:
                rename_map[fname] = None

        for fname, target in rename_map.items():
            if target is None:
                if not assigned["dms_tax"]:
                    rename_map[fname] = "dms_tax.pdf"
                    assigned["dms_tax"] = True
                elif not assigned["dms_imp_ttw"]:
                    rename_map[fname] = "dms_imp_ttw.pdf"
                    assigned["dms_imp_ttw"] = True
                else:
                    rename_map[fname] = fname

        updated = list(saved_filenames)
        for orig, new in rename_map.items():
            if not new or new == orig: continue
            src = os.path.join(folder_path, orig)
            dst = os.path.join(folder_path, new)
            try:
                if os.path.exists(src):
                    os.rename(src, dst)
                    idx = updated.index(orig)
                    updated[idx] = new
            except Exception as e:
                log.warning(f"  Rename failed '{orig}': {e}")

        return updated, tax_info

    def _upload_to_jordex(self, jordex_page, tracker: Tracker, items: list):
        doc_type, _ = JORDEX_MAPPING[CAT]
        normalize_dashboard_filters(jordex_page)

        for item in items:
            if self._stop_evt.is_set(): break
            query = item.get("oi_number") or item.get("folder_name")
            if not query: continue

            row_index = 0
            uploaded  = False
            while row_index < 10:
                success, rows_found = search_and_open(jordex_page, query, row_index=row_index)
                if not success: break
                upload_attachments(
                    jordex_page, item["folder_path"], doc_type, None,
                    file_map=CUSTOMS_DOCS_FILE_MAP,
                )
                go_back(jordex_page)
                uploaded = True
                self._uploaded += 1
                row_index += 1
                if rows_found <= row_index: break

            if uploaded:
                tracker.update_status(CAT, item["conv_id"], "uploaded")
