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
    extract_subject_mbl_ref, sanitize_reference_for_path,
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
        self._outlook_stuck = False

    def start(self):
        if self.status == "running":
            return {"ok": False, "message": "Already running"}
        self.error = None
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
                # NEW
                items = self._process_batch(outlook_page, tracker)
                if items:
                    merged = self._merge_by_folder(items)
                    jordex_page = self._upload_to_jordex(jordex_page, outlook_page, tracker, merged, jordex_session)

                if self._outlook_stuck:
                    log.warning(f"[{SERVICE_KEY}] Outlook page was stuck — hard-restarting Outlook browser")
                    try:
                        outlook_page = outlook_session.hard_restart()
                        log.info(f"[{SERVICE_KEY}] Outlook hard restart SUCCEEDED")
                    except Exception as e:
                        log.error(f"[{SERVICE_KEY}] Outlook hard restart FAILED: {e}", exc_info=True)
                        self.error  = f"Outlook hard restart failed: {e}"
                        self.status = "error"
                        break
                    self._outlook_stuck = False

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
            temp_files, page_stuck = download_attachments_to_temp(page)
            if page_stuck:
                # Outlook page is genuinely frozen — finish this email (if
                # anything was already downloaded), then stop the batch.
                # _run() hard-restarts the Outlook browser afterward.
                self._outlook_stuck = True
                log.warning(f"[{SERVICE_KEY}] Outlook page stuck — finishing this email, then stopping batch early")

            if not temp_files:
                tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], "no_attachment")
                if page_stuck:
                    break
                continue

            pdf_files   = [f for f in temp_files if f.lower().endswith(".pdf")]
            folder_groups: dict[str, list] = {}
            duplicate_folders = []
            requeued_any = False

            subject_ref = extract_subject_mbl_ref(subject)

            for pdf_path in pdf_files:
                extraction = extract_invoice_carrier(pdf_path, gemini_model=gemini_model)

                # Normalize OI references (fix 0/O confusion from LLM), one PDF
                # can name MULTIPLE shipment references (e.g. a Hapag-Lloyd
                # invoice covering 2-3 MTD/SWB-NO shipments) — process every one.
                # normalize_oi_reference() is a safe no-op for non-OI refs
                # (e.g. MBL numbers) — applied unconditionally, same as before.
                references = [
                    normalize_oi_reference(r) for r in (extraction.get("references") or [])
                ]
                extraction["references"] = references
                extraction["reference"]  = references[0] if references else None
                extraction["subject_ref"] = subject_ref

                # Audit-only cross-check: the document stays the source of
                # truth (including when it has more refs than the subject
                # shows), this only flags disagreement for manual review.
                if references and subject_ref and subject_ref not in references:
                    extraction["flag"] = extraction.get("flag") or "reference_mismatch"
                    log.warning(
                        f"[{SERVICE_KEY}] Reference mismatch: doc={references} subject='{subject_ref}'"
                    )

                if not references:
                    oi = extract_oi_from_subject(subject)
                    if oi:
                        references = [normalize_oi_reference(oi)]
                    elif subject_ref:
                        references = [subject_ref]
                    else:
                        references = [subject_folder_fallback(subject)]

                # Sanitize every candidate folder name — a reference that
                # slips through with a path separator (e.g. a garbled
                # "624W/683632") must never create a nested subdirectory.
                references = [sanitize_reference_for_path(r) for r in references if r]

                inv_no = extraction.get("invoice_no")

                for folder_name in references:
                    # Has this exact invoice_no already been downloaded before for this folder?
                    res_path = os.path.join(base, folder_name, "result.json")
                    already_downloaded = False
                    if os.path.exists(res_path) and inv_no:
                        try:
                            with open(res_path) as f:
                                old = json.load(f)
                            existing = [d.get("invoice_no") for d in (old if isinstance(old, list) else [old])]
                            already_downloaded = inv_no in existing
                        except Exception:
                            pass

                    if already_downloaded:
                        # Only a TRUE duplicate if it actually reached Jordex. Checking local
                        # disk alone can't tell "already uploaded" apart from "downloaded but
                        # the upload step never ran" (crash, browser restart, etc.) — the
                        # latter must still be pushed to Jordex, not skipped forever.
                        if tracker.is_uploaded_elsewhere(CAT, folder_name=folder_name):
                            log.info(f"[{SERVICE_KEY}] Duplicate invoice {inv_no} for '{folder_name}' — already uploaded, skipping")
                            duplicate_folders.append(folder_name)
                        else:
                            log.warning(
                                f"[{SERVICE_KEY}] Invoice {inv_no} for '{folder_name}' was downloaded "
                                f"before but never reached Jordex — re-queuing for upload (no re-download)"
                            )
                            final_dir = os.path.join(base, folder_name)
                            existing_files = sorted(
                                f for f in os.listdir(final_dir) if f.lower().endswith(".pdf")
                            ) if os.path.isdir(final_dir) else []
                            tracker.mark(CAT, cid, subject, folder_name, existing_files, "downloaded",
                                         mbl=folder_name, secondary_ref=extraction.get("secondary_ref"),
                                         subject_ref=subject_ref)
                            self._processed += 1
                            processed_items.append({
                                "conv_id":       cid,
                                "cat":           CAT,
                                "folder_path":   final_dir,
                                "folder_name":   folder_name,
                                "mbl":           folder_name,
                                "secondary_ref": extraction.get("secondary_ref"),
                                "subject_ref":   subject_ref,
                            })
                            requeued_any = True
                        continue

                    folder_groups.setdefault(folder_name, []).append({"extraction": extraction, "pdf_path": pdf_path})

            for folder_name, items in folder_groups.items():
                final_dir   = os.path.join(base, folder_name)
                os.makedirs(final_dir, exist_ok=True)
                saved_files = []
                extractions = []

                for item in items:
                    # copy=True: the same source PDF may need to be filed into
                    # several different reference folders when one invoice
                    # covers multiple shipments — the temp original is cleaned
                    # up afterward by cleanup_temp() regardless.
                    saved = move_file_to_folder(item["pdf_path"], final_dir, copy=True)
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
                    tracker.mark(CAT, cid, subject, folder_name, saved_files, "downloaded", mbl=mbl_val,
                                 secondary_ref=sec_ref, subject_ref=subject_ref)
                    self._processed += 1
                    processed_items.append({
                        "conv_id":       cid,
                        "cat":           CAT,
                        "folder_path":   final_dir,
                        "folder_name":   folder_name,
                        "mbl":           mbl_val,
                        "secondary_ref": sec_ref,
                        "subject_ref":   subject_ref,
                    })

            if not folder_groups and duplicate_folders and not requeued_any:
                tracker.mark(CAT, cid, subject, duplicate_folders[0], [], "skipped_duplicate",
                             all_duplicate_folders=duplicate_folders)

            cleanup_temp(temp_files)

            if page_stuck:
                break

        return processed_items


    # NEW
    @staticmethod
    def _merge_by_folder(items: list) -> list:
        """
        Collapse multiple email-level items into one item per folder_name.
        All conv_ids are gathered so every email gets marked after upload.
        """
        grouped: dict[str, dict] = {}
        for item in items:
            key = item["folder_name"]
            if key not in grouped:
                grouped[key] = {
                    "conv_ids":      [item["conv_id"]],
                    "cat":           item["cat"],
                    "folder_path":   item["folder_path"],
                    "folder_name":   key,
                    "mbl":           item.get("mbl"),
                    "secondary_ref": item.get("secondary_ref"),
                    "subject_ref":   item.get("subject_ref"),
                }
            else:
                grouped[key]["conv_ids"].append(item["conv_id"])
                if item.get("secondary_ref"):
                    grouped[key]["secondary_ref"] = item["secondary_ref"]
                if item.get("subject_ref"):
                    grouped[key]["subject_ref"] = item["subject_ref"]
        return list(grouped.values())

   
        
    # NEW
    def _upload_to_jordex(self, jordex_page, outlook_page, tracker: Tracker, items: list, jordex_session=None):
        """Returns the current jordex_page, which may be a fresh Page if
        search_and_open had to hard-restart the browser mid-batch."""
        doc_type, display_name = JORDEX_MAPPING[CAT]

        for item in items:
            if self._stop_evt.is_set():
                break

            query = item.get("mbl") or item.get("folder_name")
            if not query:
                continue

            folder_name = item["folder_name"]
            conv_ids    = item["conv_ids"]

            if query.startswith("OE"):
                query = normalize_oi_reference(query)
                for cid in conv_ids:
                    tracker.update_status(CAT, cid, "Skipped")
                continue

            # Cross-batch dedup: skip only if ALL conv_ids are already covered
            already_uploaded = all(
                tracker.is_uploaded_elsewhere(
                    CAT, folder_name=folder_name,
                    mbl=item.get("mbl"), exclude_conv_id=cid,
                )
                for cid in conv_ids
            )
            if already_uploaded:
                # ★ FIX: Check if NEW files exist that weren't uploaded before.
                local_files = set(
                    f for f in os.listdir(item["folder_path"])
                    if f.lower().endswith(".pdf")
                )
                previously_uploaded = tracker.get_uploaded_files(CAT, folder_name)
                new_files = local_files - previously_uploaded

                if not new_files:
                    log.info(
                        f"[{SERVICE_KEY}] Skipping '{folder_name}' — "
                        f"already uploaded to Jordex and no new files"
                    )
                    for cid in conv_ids:
                        tracker.update_status(CAT, cid, "uploaded")
                    continue
                else:
                    log.info(
                        f"[{SERVICE_KEY}] Re-uploading '{folder_name}' — "
                        f"{len(new_files)} new file(s) since last upload: {new_files}"
                    )
                    # Fall through to Jordex search + upload below

            success, used_ref, rows_found, jordex_page = search_jordex_with_fallback(
                jordex_page=jordex_page,
                outlook_page=outlook_page,
                primary_ref=query,
                secondary_ref=item.get("secondary_ref"),
                subject_ref=item.get("subject_ref"),
                conv_id=conv_ids[0],
                tracker=tracker,
                cat=CAT,
                service_key=SERVICE_KEY,
                search_fn=search_and_open,
                jordex_session=jordex_session,
            )
            if not success:
                continue

            row_index = 0
            uploaded  = False
            try:
                while row_index < 10:
                    success, rows_found, jordex_page = search_and_open(
                        jordex_page, used_ref, row_index=row_index, session=jordex_session
                    )
                    if not success:
                        break
                    inv_file_map = build_invoice_carrier_file_map(item["folder_path"])
                    upload_ok = upload_attachments(jordex_page, item["folder_path"], doc_type, display_name, file_map=inv_file_map)
                    if not upload_ok:
                        log.warning(f"[{SERVICE_KEY}] upload_attachments returned False for {query} row {row_index}")
                    # Give Jordex server time to persist before navigating away
                    jordex_page.wait_for_timeout(2000)
                    go_back(jordex_page)
                    if upload_ok:
                        uploaded = True
                        self._uploaded += 1
                    row_index += 1
                    if rows_found <= row_index:
                        break
            except Exception as e:
                log.error(f"[{SERVICE_KEY}] Error during upload loop for {query}: {e}", exc_info=True)
            finally:
                if uploaded:
                    for cid in conv_ids:
                        tracker.update_status(CAT, cid, "uploaded")
                else:
                    log.warning(f"[{SERVICE_KEY}] Could not open/upload shipment for {query}")

        return jordex_page