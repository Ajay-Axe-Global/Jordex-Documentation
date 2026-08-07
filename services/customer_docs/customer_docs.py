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
from jordex.documents import upload_attachments, build_customer_docs_file_map, get_container_no_map
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
                # Outlook page is genuinely frozen (not a per-email glitch) —
                # stop downloading more emails this batch. Whatever WAS
                # already downloaded for this email still gets processed and
                # uploaded normally below; _run() hard-restarts the Outlook
                # browser afterward instead of hammering a dead page.
                self._outlook_stuck = True
                log.warning(f"[{SERVICE_KEY}] Outlook page stuck — finishing this email, then stopping batch early")

            if not temp_files:
                tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], "no_attachment")
                if page_stuck:
                    break
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

            # ── Detect multi-MBL emails (one email, N PDFs, each has its own MBL) ──
            # When every classified PDF has a DIFFERENT reference_number we must
            # do one Jordex search PER PDF, not one search for the whole folder.
            # Build a per_file_upload list: [{file, mbl, container_no}, ...]
            per_file_upload = None
            if cust_results and len(cust_results) > 1:
                unique_refs = set(
                    r.get("reference_number") for r in cust_results
                    if r.get("reference_number")
                )
                if len(unique_refs) > 1:
                    # Multi-MBL: group files by their individual reference_number
                    per_file_upload = []
                    for r in cust_results:
                        ref = r.get("reference_number")
                        src = r.get("source_file")
                        cnt = r.get("container_no")
                        if ref and src:
                            per_file_upload.append({
                                "file":        src,
                                "mbl":         ref,
                                "container_no": cnt,
                            })
                    log.info(
                        f"[{SERVICE_KEY}] Multi-MBL email: {len(per_file_upload)} PDFs with "
                        f"{len(unique_refs)} unique MBLs — will search Jordex individually"
                    )

            # secondary_ref: whichever of container_no / reference_number was NOT
            # already used as the primary (folder_name), so the fallback search
            # actually tries a different value instead of repeating the primary.
            sec_ref = None
            if cust_results and not per_file_upload:
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
                "conv_id":        cid,
                "cat":            CAT,
                "folder_path":    final_dir,
                "folder_name":    folder_name,
                "mbl":            None,
                "secondary_ref":  sec_ref,
                "saved_files":    saved_files,
                "per_file_upload": per_file_upload,  # None = normal, list = multi-MBL
            })

            if page_stuck:
                break

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

    @staticmethod
    def _merge_by_folder(items: list) -> list:
        """
        Collapse multiple email-level items into one item per folder_name
        (MBL). One MBL can receive many separate emails over time — each
        with a unique container — so all conv_ids and their saved_files
        are gathered here, keyed back to the conv_id that produced them,
        so upload success/failure can be attributed to the right email.

        Multi-MBL items (per_file_upload is set) are passed through as-is
        because each PDF already has its own MBL — they must NOT be merged
        with other items by folder_name.
        """
        grouped: dict[str, dict] = {}
        passthrough: list = []
        for item in items:
            # Multi-MBL items skip grouping — upload logic handles them file by file
            if item.get("per_file_upload"):
                passthrough.append({
                    "conv_ids":        [item["conv_id"]],
                    "cat":             item["cat"],
                    "folder_path":     item["folder_path"],
                    "folder_name":     item["folder_name"],
                    "mbl":             item.get("mbl"),
                    "secondary_ref":   item.get("secondary_ref"),
                    "file_conv_map":   {f: item["conv_id"] for f in item.get("saved_files") or []},
                    "per_file_upload": item["per_file_upload"],
                })
                continue
            key = item["folder_name"]
            if key not in grouped:
                grouped[key] = {
                    "conv_ids":       [item["conv_id"]],
                    "cat":            item["cat"],
                    "folder_path":    item["folder_path"],
                    "folder_name":    key,
                    "mbl":            item.get("mbl"),
                    "secondary_ref":  item.get("secondary_ref"),
                    "file_conv_map":  {f: item["conv_id"] for f in item.get("saved_files") or []},
                }
            else:
                grouped[key]["conv_ids"].append(item["conv_id"])
                if item.get("secondary_ref"):
                    grouped[key]["secondary_ref"] = item["secondary_ref"]
                for f in item.get("saved_files") or []:
                    grouped[key]["file_conv_map"][f] = item["conv_id"]
        return passthrough + list(grouped.values())

    def _upload_to_jordex(self, jordex_page, outlook_page, tracker: Tracker, items: list, jordex_session=None):
        """Returns the current jordex_page, which may be a fresh Page if
        search_and_open had to hard-restart the browser mid-batch.

        Each item is one MBL/folder group (merged across every email that
        shares it). If the MBL's Jordex search shows only one shipment row,
        every new file for it goes to that row (old behavior). If it shows
        MULTIPLE rows (one per container under that MBL), files are
        uploaded PER CONTAINER — each file's own container_no is searched
        individually so it lands on the one row it actually belongs to,
        instead of being broadcast to every row.

        MULTI-MBL emails: if the item has a 'per_file_upload' list, each
        PDF is searched + uploaded against its OWN unique MBL number — one
        Jordex search per file, not one search that receives all files.
        """
        doc_type, display_name = JORDEX_MAPPING[CAT]

        for item in items:
            if self._stop_evt.is_set(): break

            # ── Multi-MBL path: each file has its own unique MBL number ─────
            per_file_upload = item.get("per_file_upload")
            if per_file_upload:
                conv_ids = item["conv_ids"]
                full_file_map = build_customer_docs_file_map(item["folder_path"])
                uploaded_files: set[str] = set()

                for entry in per_file_upload:
                    if self._stop_evt.is_set():
                        break
                    filename    = entry["file"]
                    mbl_ref     = normalize_oi_reference(entry["mbl"])
                    container_no = entry.get("container_no")

                    if filename not in full_file_map:
                        log.warning(f"[{SERVICE_KEY}] Multi-MBL: no file_map entry for '{filename}' — skipping")
                        continue

                    # Search Jordex using this file's own MBL (or container fallback)
                    success, used_ref, rows_found, jordex_page = search_jordex_with_fallback(
                        jordex_page=jordex_page,
                        outlook_page=outlook_page,
                        primary_ref=mbl_ref,
                        secondary_ref=container_no,
                        conv_id=conv_ids[0],
                        tracker=tracker,
                        cat=CAT,
                        service_key=SERVICE_KEY,
                        search_fn=search_and_open,
                        jordex_session=jordex_session,
                    )
                    if not success:
                        log.warning(f"[{SERVICE_KEY}] Multi-MBL: could not find '{mbl_ref}' in Jordex — skipping '{filename}'")
                        continue

                    success, _, jordex_page = search_and_open(
                        jordex_page, used_ref, row_index=0, session=jordex_session
                    )
                    if not success:
                        continue

                    single_map = {filename: full_file_map[filename]}
                    row_ok = upload_attachments(
                        jordex_page, item["folder_path"], doc_type, display_name, file_map=single_map,
                    )
                    go_back(jordex_page)
                    if row_ok:
                        uploaded_files.add(filename)
                        self._uploaded += 1
                        log.info(f"[{SERVICE_KEY}] Multi-MBL: uploaded '{filename}' → '{mbl_ref}'")
                    else:
                        log.warning(f"[{SERVICE_KEY}] Multi-MBL: upload not confirmed for '{filename}' ({mbl_ref})")

                # Mark the whole email done if all files succeeded
                if uploaded_files:
                    for cid in conv_ids:
                        tracker.update_status(CAT, cid, "uploaded")
                else:
                    for cid in conv_ids:
                        try:
                            mark_as_unread(outlook_page, cid)
                        except Exception:
                            pass
                continue  # done with this item — skip normal single-MBL logic below

            # ── Normal path (single MBL per email / same MBL across files) ───
            query = item.get("mbl") or item.get("folder_name")
            if not query: continue

            folder_name    = item.get("folder_name") or query
            conv_ids       = item["conv_ids"]
            file_conv_map  = item.get("file_conv_map") or {}

            # Only touch files this MBL hasn't already gotten to Jordex —
            # driven by tracker.json (persists across batches/days), not a
            # one-shot "already uploaded once" flag, so a folder that keeps
            # receiving new containers over time is never wholesale skipped.
            local_files = set(file_conv_map.keys()) or {
                os.path.basename(p) for p in glob.glob(os.path.join(item["folder_path"], "*"))
            }
            already_uploaded = tracker.get_uploaded_files(CAT, folder_name)
            new_files = local_files - already_uploaded

            if not new_files:
                log.info(f"[{SERVICE_KEY}] '{folder_name}' — no new files, all already uploaded")
                for cid in conv_ids:
                    tracker.update_status(CAT, cid, "uploaded")
                continue

            query = normalize_oi_reference(query)
            success, used_ref, rows_found, jordex_page = search_jordex_with_fallback(
                jordex_page=jordex_page,
                outlook_page=outlook_page,
                primary_ref=query,
                secondary_ref=item.get("secondary_ref"),
                conv_id=conv_ids[0],
                tracker=tracker,
                cat=CAT,
                service_key=SERVICE_KEY,
                search_fn=search_and_open,
                jordex_session=jordex_session,
            )
            if not success:
                continue

            full_file_map = build_customer_docs_file_map(item["folder_path"])
            file_map = {f: v for f, v in full_file_map.items() if f in new_files}
            if not file_map:
                for cid in conv_ids:
                    tracker.update_status(CAT, cid, "uploaded")
                continue

            log.info(f"[{SERVICE_KEY}] '{folder_name}': Jordex shows {rows_found} row(s) for this MBL — "
                     f"{len(file_map)} new file(s) to place")

            uploaded_files: set[str] = set()
            try:
                if rows_found <= 1:
                    # Single shipment row for this MBL — all new files go here.
                    hbl_count, mbl_count = self._count_bl_types(item["folder_path"])
                    check_carrier_tab = hbl_count >= 1 and mbl_count >= 1

                    success, _, jordex_page = search_and_open(
                        jordex_page, used_ref, row_index=0, session=jordex_session
                    )
                    if success:
                        row_ok = upload_attachments(
                            jordex_page, item["folder_path"], doc_type, display_name, file_map=file_map,
                        )
                        if row_ok and check_carrier_tab and self._check_carrier_bl_fields(jordex_page):
                            log.warning(
                                f"[{SERVICE_KEY}] Carrier tab BL field(s) empty for '{folder_name}' "
                                f"despite {hbl_count} HBL + {mbl_count} MBL doc(s) uploaded"
                            )
                        go_back(jordex_page)
                        if row_ok:
                            uploaded_files = set(file_map.keys())
                            self._uploaded += 1
                        else:
                            log.warning(f"[{SERVICE_KEY}] Upload not confirmed for '{folder_name}' — will retry next run")
                else:
                    # Multiple containers under one MBL — search + upload
                    # each file against ITS OWN container number.
                    container_map = get_container_no_map(item["folder_path"])
                    uploaded_files, jordex_page = self._upload_per_container(
                        jordex_page, item, used_ref, file_map, container_map, jordex_session
                    )
            except Exception as e:
                log.error(f"[{SERVICE_KEY}] Error during upload for {query}: {e}", exc_info=True)

            failed_files = set(file_map.keys()) - uploaded_files
            done_conv_ids   = {file_conv_map[f] for f in uploaded_files if f in file_conv_map}
            failed_conv_ids = {file_conv_map[f] for f in failed_files if f in file_conv_map} - done_conv_ids
            # conv_ids with no file mapping (e.g. legacy items) fall back to
            # the group-level outcome, matching prior single-item behavior.
            unmapped_conv_ids = set(conv_ids) - done_conv_ids - failed_conv_ids
            if unmapped_conv_ids:
                (done_conv_ids if uploaded_files else failed_conv_ids).update(unmapped_conv_ids)

            for cid in done_conv_ids:
                tracker.update_status(CAT, cid, "uploaded")
            for cid in failed_conv_ids:
                log.warning(f"[{SERVICE_KEY}] Could not upload file(s) for conv_id={cid[:20]}… ({folder_name})")
                try:
                    mark_as_unread(outlook_page, cid)
                except Exception as e:
                    log.warning(f"[{SERVICE_KEY}] Could not mark unread for {cid}: {e}")

        return jordex_page

    def _upload_per_container(self, jordex_page, item, mbl_ref, file_map, container_map, jordex_session):
        """
        Upload each file individually, searching Jordex by ITS OWN container
        number so it lands on the specific row that container belongs to —
        instead of broadcasting every file to every row under the MBL.
        Files with no extractable container_no fall back to a single upload
        against the primary MBL's first row (not broadcast to every row).
        Returns (uploaded_filenames: set[str], jordex_page).
        """
        doc_type, display_name = JORDEX_MAPPING[CAT]
        uploaded_files: set[str] = set()

        for filename in file_map:
            container_no = container_map.get(filename)
            if not container_no:
                continue
            success, rows_found, jordex_page = search_and_open(
                jordex_page, container_no, row_index=0, session=jordex_session
            )
            if not success:
                log.warning(
                    f"[{SERVICE_KEY}] Container '{container_no}' (file '{filename}') "
                    f"not found in Jordex — skipping this file, will retry next run"
                )
                continue
            single_map = {filename: file_map[filename]}
            row_ok = upload_attachments(
                jordex_page, item["folder_path"], doc_type, display_name, file_map=single_map,
            )
            go_back(jordex_page)
            if row_ok:
                uploaded_files.add(filename)
                self._uploaded += 1
            else:
                log.warning(f"[{SERVICE_KEY}] Upload not confirmed for '{filename}' (container {container_no})")

        # Files with no container_no (e.g. debit notes, invoices) apply to
        # the shipment as a whole — upload once against the primary MBL's
        # first row rather than broadcasting to every container row.
        leftover = {f: v for f, v in file_map.items() if f not in uploaded_files and not container_map.get(f)}
        if leftover:
            success, _, jordex_page = search_and_open(
                jordex_page, mbl_ref, row_index=0, session=jordex_session
            )
            if success:
                row_ok = upload_attachments(
                    jordex_page, item["folder_path"], doc_type, display_name, file_map=leftover,
                )
                go_back(jordex_page)
                if row_ok:
                    uploaded_files.update(leftover.keys())
                else:
                    log.warning(f"[{SERVICE_KEY}] Upload not confirmed for {list(leftover.keys())} (no container_no)")

        return uploaded_files, jordex_page