"""
services/invoice_carrier/invoice_carrier.py — Invoice Carrier Service
======================================================================
Owns its own Playwright instances (Outlook + Jordex) with isolated profiles.
Processes the "05.Invoice Carrier" Outlook label independently.
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
    mark_as_unread, search_jordex_with_fallback,
)
from extractor import extract_oi_from_subject
from outlook.session import OutlookSession
from jordex.login import JordexSession
from jordex.browser import normalize_dashboard_filters, search_and_open, go_back
from jordex.documents import upload_attachments, build_invoice_carrier_file_map
from services.invoice_carrier.extractor import extract_invoice_carrier

log = logging.getLogger("service.invoice_carrier")

SERVICE_KEY   = "invoice_carrier"
OUTLOOK_LABEL = "05.Invoice Carrier"
CAT           = "Invoice_Carrier"


class InvoiceCarrierService:
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

            pdf_files   = [f for f in temp_files if f.lower().endswith(".pdf")]
            folder_groups: dict[str, list] = {}

            for pdf_path in pdf_files:
                extraction  = extract_invoice_carrier(pdf_path, gemini_model=gemini_model)
                folder_name = extraction.get("reference")

                # Normalize OI reference (fix 0/O confusion from LLM)
                if folder_name:
                    folder_name = normalize_oi_reference(folder_name)
                    extraction["reference"] = folder_name

                if not folder_name:
                    oi = extract_oi_from_subject(subject)
                    folder_name = normalize_oi_reference(oi) if oi else subject_folder_fallback(subject)

                inv_no = extraction.get("invoice_no")

                # Duplicate check
                res_path = os.path.join(base, folder_name, "result.json")
                if os.path.exists(res_path) and inv_no:
                    try:
                        with open(res_path) as f:
                            old = json.load(f)
                        existing = [d.get("invoice_no") for d in (old if isinstance(old, list) else [old])]
                        if inv_no in existing:
                            log.info(f"[{SERVICE_KEY}] Duplicate invoice {inv_no}, skipping")
                            continue
                    except Exception: pass

                if folder_name not in folder_groups:
                    folder_groups[folder_name] = []
                folder_groups[folder_name].append({"extraction": extraction, "pdf_path": pdf_path})

            for folder_name, items in folder_groups.items():
                final_dir   = os.path.join(base, folder_name)
                os.makedirs(final_dir, exist_ok=True)
                saved_files = []
                extractions = []

                for item in items:
                    saved = move_file_to_folder(item["pdf_path"], final_dir)
                    if saved: saved_files.append(saved)
                    ext = item["extraction"]
                    if ext:
                        ext["subject"] = subject
                        extractions.append(ext)

                if extractions:
                    res_path = os.path.join(final_dir, "result.json")
                    if os.path.exists(res_path):
                        try:
                            with open(res_path) as f: old = json.load(f)
                            if not isinstance(old, list): old = [old]
                            old.extend(extractions)
                            with open(res_path, "w") as f: json.dump(old, f, indent=2, default=str)
                        except Exception:
                            save_result(extractions[-1], final_dir)
                    else:
                        with open(res_path, "w") as f:
                            json.dump(extractions, f, indent=2, default=str)

                if saved_files:
                    mbl_val = folder_name
                    last_ext = extractions[-1] if extractions else {}
                    sec_ref = last_ext.get("secondary_ref")
                    tracker.mark(CAT, cid, subject, folder_name, saved_files, "downloaded", mbl=mbl_val, secondary_ref=sec_ref)
                    self._processed += 1
                    processed_items.append({
                        "conv_id":       cid,
                        "cat":           CAT,
                        "folder_path":   final_dir,
                        "folder_name":   folder_name,
                        "mbl":           mbl_val,
                        "secondary_ref": sec_ref,
                    })

            cleanup_temp(temp_files)

        return processed_items

    def _upload_to_jordex(self, jordex_page, outlook_page, tracker: Tracker, items: list):
        doc_type, display_name = JORDEX_MAPPING[CAT]


        # Deduplicate: track folder_names already uploaded this batch
        uploaded_folders: set[str] = set()

        for item in items:
            if self._stop_evt.is_set(): break
            query = item.get("mbl") or item.get("folder_name")
            if not query: continue
            if query.startswith("OE"):
                query = normalize_oi_reference(query)
                tracker.update_status(CAT, item["conv_id"], "Skipped")
                continue

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
            try:
                while row_index < 10:
                    success, rows_found = search_and_open(jordex_page, used_ref, row_index=row_index)
                    if not success: break
                    inv_file_map = build_invoice_carrier_file_map(item["folder_path"])
                    upload_attachments(jordex_page, item["folder_path"], doc_type, display_name, file_map=inv_file_map)
                    go_back(jordex_page)
                    uploaded = True
                    self._uploaded += 1
                    row_index += 1
                    if rows_found <= row_index: break
            except Exception as e:
                log.error(f"[{SERVICE_KEY}] Error during upload loop for {query}: {e}", exc_info=True)
            finally:
                if uploaded:
                    tracker.update_status(CAT, item["conv_id"], "uploaded")
                    uploaded_folders.add(folder_name)
                else:
                    log.warning(f"[{SERVICE_KEY}] Could not open/upload shipment for {query}")

