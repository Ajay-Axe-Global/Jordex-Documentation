"""
services/booking/booking.py — Booking Confirmation Service
===========================================================
Handles "06.Booking confirmation" Outlook label.
Owns its own Playwright instances with isolated profiles.
Booking confirmation emails are download-only (no Gemini extraction needed).
"""
import os, logging, threading, time
from datetime import datetime
from playwright.sync_api import sync_playwright

from config import OUTPUT_DIR, JORDEX_MAPPING, ROUND_ROBIN_BATCH
from extractor import save_result
from shared.tracker import Tracker
from shared.helpers import (
    navigate_to_folder, collect_unread, click_row, get_subject,
    download_attachments_to_temp, move_file_to_folder, cleanup_temp,
    subject_folder_fallback,
)
from outlook.session import OutlookSession
from jordex.login import JordexSession
from jordex.browser import normalize_dashboard_filters, search_and_open, go_back
from jordex.documents import upload_attachments

log = logging.getLogger("service.booking")

SERVICE_KEY   = "booking"
OUTLOOK_LABEL = "06.Booking confirmation"
CAT           = "Booking_Confirmation"


class BookingService:
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

            # Booking: folder name from subject (no Gemini extraction needed)
            folder_name = subject_folder_fallback(subject)
            final_dir   = os.path.join(base, folder_name)
            os.makedirs(final_dir, exist_ok=True)

            saved_files = []
            for tmp in temp_files:
                saved = move_file_to_folder(tmp, final_dir)
                if saved: saved_files.append(saved)

            extraction = {
                "doc_type":     "booking",
                "subject":      subject,
                "folder_name":  folder_name,
                "extracted_at": datetime.now().isoformat(),
                "source_files": saved_files,
            }
            save_result(extraction, final_dir)
            cleanup_temp(temp_files)

            tracker.mark(CAT, cid, subject, folder_name, saved_files, "downloaded")
            self._processed += 1
            processed_items.append({
                "conv_id":     cid,
                "cat":         CAT,
                "folder_path": final_dir,
                "folder_name": folder_name,
                "mbl":         None,
            })

        return processed_items

    def _upload_to_jordex(self, jordex_page, tracker: Tracker, items: list):
        doc_type, display_name = JORDEX_MAPPING[CAT]
        normalize_dashboard_filters(jordex_page)

        for item in items:
            if self._stop_evt.is_set(): break
            query = item.get("folder_name")
            if not query: continue

            row_index = 0
            uploaded  = False
            while row_index < 10:
                success, rows_found = search_and_open(jordex_page, query, row_index=row_index)
                if not success: break
                upload_attachments(jordex_page, item["folder_path"], doc_type, display_name)
                go_back(jordex_page)
                uploaded = True
                self._uploaded += 1
                row_index += 1
                if rows_found <= row_index: break

            if uploaded:
                tracker.update_status(CAT, item["conv_id"], "uploaded")
