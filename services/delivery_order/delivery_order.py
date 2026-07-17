# """
# services/delivery_order/delivery_order.py — Delivery Order Service
# ===================================================================
# Handles "02.Delivery Order" Outlook label.
# Owns its own Playwright instances with isolated profiles.
# Multi-PDF support: each PDF may have its own MBL → its own folder.
# """
# import os, json, logging, threading, time
# from datetime import datetime
# from playwright.sync_api import sync_playwright

# from config import OUTPUT_DIR, JORDEX_MAPPING, ROUND_ROBIN_BATCH
# from extractor import gemini_model, save_result
# from shared.tracker import Tracker
# from shared.helpers import (
#     navigate_to_folder, collect_unread, click_row, get_subject,
#     download_attachments_to_temp, move_file_to_folder, cleanup_temp,
#     subject_folder_fallback,
# )
# from outlook.session import OutlookSession
# from jordex.login import JordexSession
# from jordex.browser import normalize_dashboard_filters, search_and_open, go_back
# from jordex.documents import upload_attachments
# from services.delivery_order.extractor import extract_delivery_order

# log = logging.getLogger("service.delivery_order")

# SERVICE_KEY   = "delivery_order"
# OUTLOOK_LABEL = "02.Delivery Order"
# CAT           = "Delivery_Order"


# class DeliveryOrderService:
#     def __init__(self):
#         self.status     = "idle"
#         self.error      = None
#         self._thread    = None
#         self._stop_evt  = threading.Event()
#         self._processed = 0
#         self._uploaded  = 0
#         self.last_run   = None

#     def start(self):
#         if self.status == "running":
#             return {"ok": False, "message": "Already running"}
#         self._stop_evt.clear()
#         self._thread = threading.Thread(target=self._run, daemon=True, name=f"svc-{SERVICE_KEY}")
#         self._thread.start()
#         self.status = "running"
#         return {"ok": True, "message": "Started"}

#     def stop(self):
#         if self.status != "running":
#             return {"ok": False, "message": "Not running"}
#         self._stop_evt.set()
#         self.status = "stopping"
#         return {"ok": True, "message": "Stop signal sent"}

#     def get_status(self) -> dict:
#         tracker = Tracker()
#         stats   = tracker.stats(CAT)
#         return {
#             "service":   SERVICE_KEY,
#             "label":     OUTLOOK_LABEL,
#             "status":    self.status,
#             "error":     self.error,
#             "processed": stats.get("total", 0),      # from tracking.json
#             "uploaded":  stats.get("uploaded", 0),   # from tracking.json
#             "last_run":  self.last_run,
#             "stats":     stats,
#         }

#     def _run(self):
#         pw = outlook_session = jordex_session = None
#         try:
#             pw              = sync_playwright().start()
#             outlook_session = OutlookSession(service_key=SERVICE_KEY, pw=pw)
#             jordex_session  = JordexSession(service_key=SERVICE_KEY, pw=pw)
#             outlook_page    = outlook_session.start()
#             jordex_page     = jordex_session.start()
#             tracker         = Tracker()

#             while not self._stop_evt.is_set():
#                 self.last_run = datetime.now().isoformat()
#                 items = self._process_batch(outlook_page, tracker)
#                 if items:
#                     self._upload_to_jordex(jordex_page, tracker, items)
#                 for _ in range(ROUND_ROBIN_BATCH * 2):
#                     if self._stop_evt.is_set(): break
#                     time.sleep(1)

#         except Exception as e:
#             log.error(f"[{SERVICE_KEY}] Fatal: {e}", exc_info=True)
#             self.error  = str(e)
#             self.status = "error"
#         finally:
#             for s in [outlook_session, jordex_session]:
#                 if s:
#                     try: s.close()
#                     except Exception: pass
#             if pw:
#                 try: pw.stop()
#                 except Exception: pass
#             if self.status != "error":
#                 self.status = "idle"

#     def _process_batch(self, page, tracker: Tracker) -> list:
#         navigate_to_folder(page, OUTLOOK_LABEL)
#         msgs = collect_unread(page, tracker, CAT, limit=ROUND_ROBIN_BATCH)
#         if not msgs:
#             return []

#         base            = os.path.join(OUTPUT_DIR, CAT)
#         processed_items = []

#         for msg in msgs:
#             if self._stop_evt.is_set(): break
#             cid = msg["conv_id"]

#             if not click_row(page, cid):
#                 tracker.mark(CAT, cid, "", "", [], "failed")
#                 continue

#             subject    = get_subject(page) or cid[:40]
#             temp_files = download_attachments_to_temp(page)

#             if not temp_files:
#                 tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], "no_attachment")
#                 continue

#             pdf_files = [f for f in temp_files if f.lower().endswith(".pdf")]

#             if gemini_model and pdf_files:
#                 # Multi-PDF: each PDF may have a different MBL
#                 multi_items = self._process_multi_pdfs(pdf_files, temp_files, base, subject, cid)
#                 cleanup_temp(temp_files)

#                 if multi_items:
#                     all_mbls  = [it["mbl"] for it in multi_items if it.get("mbl")]
#                     all_files = [f for it in multi_items for f in it.get("files", [])]
#                     tracker.mark(
#                         CAT, cid, subject,
#                         multi_items[0]["folder_name"], all_files, "downloaded",
#                         mbl=all_mbls[0] if all_mbls else None,
#                     )
#                     for it in multi_items:
#                         it["conv_id"] = cid
#                         self._processed += 1
#                         processed_items.append(it)
#                 else:
#                     tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], "failed")
#             else:
#                 cleanup_temp(temp_files)
#                 tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], "failed")

#         return processed_items

#     def _process_multi_pdfs(self, pdf_files, all_temp_files, base, subject, cid) -> list:
#         pdf_to_ext  = {}
#         for pdf_path in pdf_files:
#             ext         = extract_delivery_order(pdf_path, gemini_model=gemini_model)
#             folder_name = ext.get("folder_name") or subject_folder_fallback(subject)
#             pdf_to_ext[pdf_path] = (ext, folder_name)

#         folder_groups: dict[str, dict] = {}
#         for pdf_path, (ext, folder_name) in pdf_to_ext.items():
#             if folder_name not in folder_groups:
#                 folder_groups[folder_name] = {"extraction": ext, "pdfs": []}
#             folder_groups[folder_name]["pdfs"].append(pdf_path)

#         # Non-PDF temps go to first folder
#         non_pdfs    = [f for f in all_temp_files if not f.lower().endswith(".pdf")]
#         first_folder = next(iter(folder_groups)) if folder_groups else None
#         if first_folder and non_pdfs:
#             folder_groups[first_folder]["pdfs"] = non_pdfs + folder_groups[first_folder]["pdfs"]

#         processed = []
#         for folder_name, group in folder_groups.items():
#             final_dir = os.path.join(base, folder_name)
#             os.makedirs(final_dir, exist_ok=True)
#             saved     = []
#             for tmp in group["pdfs"]:
#                 if not os.path.exists(tmp): continue
#                 s = move_file_to_folder(tmp, final_dir)
#                 if s: saved.append(s)

#             ext = group["extraction"]
#             ext["subject"] = subject
#             save_result(ext, final_dir)

#             mbl_val = ext.get("mbl") or ext.get("reference")
#             processed.append({
#                 "cat":         CAT,
#                 "folder_path": final_dir,
#                 "folder_name": folder_name,
#                 "mbl":         mbl_val,
#                 "files":       saved,
#                 "oi_number":   None,
#             })

#         return processed

#     def _upload_to_jordex(self, jordex_page, tracker: Tracker, items: list):
#         doc_type, display_name = JORDEX_MAPPING[CAT]
#         normalize_dashboard_filters(jordex_page)

#         for item in items:
#             if self._stop_evt.is_set(): break
#             query = item.get("mbl") or item.get("folder_name")
#             if not query: continue

#             row_index = 0
#             uploaded  = False
#             while row_index < 10:
#                 success, rows_found = search_and_open(jordex_page, query, row_index=row_index)
#                 if not success: break
#                 upload_attachments(jordex_page, item["folder_path"], doc_type, display_name)
#                 go_back(jordex_page)
#                 uploaded = True
#                 self._uploaded += 1
#                 row_index += 1
#                 if rows_found <= row_index: break

#             if uploaded:
#                 tracker.update_status(CAT, item.get("conv_id"), "uploaded")

"""
services/delivery_order/delivery_order.py — Delivery Order Service
===================================================================
Handles "02.Delivery Order" Outlook label.

Flow per shipment:
  1. Download email attachments, extract DO data (MBL, containers, pickup, return)
  2. Search Jordex by container number, open shipment
  3. NEW: View Routing → Destination → fill Pick-up Terminal + Return Terminal per container
  4. Back to shipment detail → Upload document as "Container release" / "DO"
  5. Back → next item
"""
import os, json, re, logging, threading, time
from datetime import datetime
from playwright.sync_api import sync_playwright, Page

from config import OUTPUT_DIR, JORDEX_MAPPING, ROUND_ROBIN_BATCH
from extractor import gemini_model, save_result
from shared.tracker import Tracker
from shared.helpers import (
    navigate_to_folder, collect_unread, click_row, get_subject,
    download_attachments_to_temp, move_file_to_folder, cleanup_temp,
    subject_folder_fallback, normalize_oi_reference,
)
from outlook.session import OutlookSession
from jordex.login import JordexSession
from jordex.browser import normalize_dashboard_filters, search_and_open, go_back, apply_zoom
from jordex.documents import upload_attachments
from services.delivery_order.extractor import extract_delivery_order

log = logging.getLogger("service.delivery_order")

SERVICE_KEY   = "delivery_order"
OUTLOOK_LABEL = "02.Delivery Order"
CAT           = "Delivery_Order"


class DeliveryOrderService:
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
            "processed": stats.get("total", 0),
            "uploaded":  stats.get("uploaded", 0),
            "last_run":  self.last_run,
            "stats":     stats,
        }

    # ══════════════════════════════════════════════════════════════════
    #  MAIN LOOP
    # ══════════════════════════════════════════════════════════════════

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
                    except: pass
            if pw:
                try: pw.stop()
                except: pass
            if self.status != "error":
                self.status = "idle"

    # ══════════════════════════════════════════════════════════════════
    #  EMAIL PROCESSING (download + extract)
    # ══════════════════════════════════════════════════════════════════

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

            if gemini_model and pdf_files:
                multi_items = self._process_multi_pdfs(pdf_files, temp_files, base, subject, cid)
                cleanup_temp(temp_files)

                if multi_items:
                    all_mbls  = [it["mbl"] for it in multi_items if it.get("mbl")]
                    all_files = [f for it in multi_items for f in it.get("files", [])]
                    tracker.mark(
                        CAT, cid, subject,
                        multi_items[0]["folder_name"], all_files, "downloaded",
                        mbl=all_mbls[0] if all_mbls else None,
                    )
                    for it in multi_items:
                        it["conv_id"] = cid
                        self._processed += 1
                        processed_items.append(it)
                else:
                    tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], "failed")
            else:
                cleanup_temp(temp_files)
                tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], "failed")

        return processed_items

    def _process_multi_pdfs(self, pdf_files, all_temp_files, base, subject, cid) -> list:
        pdf_to_ext = {}
        for pdf_path in pdf_files:
            ext         = extract_delivery_order(pdf_path, gemini_model=gemini_model)
            folder_name = ext.get("folder_name") or subject_folder_fallback(subject)
            pdf_to_ext[pdf_path] = (ext, folder_name)

        folder_groups: dict[str, dict] = {}
        for pdf_path, (ext, folder_name) in pdf_to_ext.items():
            if folder_name not in folder_groups:
                folder_groups[folder_name] = {"extraction": ext, "pdfs": []}
            folder_groups[folder_name]["pdfs"].append(pdf_path)

        non_pdfs     = [f for f in all_temp_files if not f.lower().endswith(".pdf")]
        first_folder = next(iter(folder_groups)) if folder_groups else None
        if first_folder and non_pdfs:
            folder_groups[first_folder]["pdfs"] = non_pdfs + folder_groups[first_folder]["pdfs"]

        processed = []
        for folder_name, group in folder_groups.items():
            final_dir = os.path.join(base, folder_name)
            os.makedirs(final_dir, exist_ok=True)
            saved = []
            for tmp in group["pdfs"]:
                if not os.path.exists(tmp): continue
                s = move_file_to_folder(tmp, final_dir)
                if s: saved.append(s)

            ext = group["extraction"]
            ext["subject"] = subject
            save_result(ext, final_dir)

            mbl_val = ext.get("mbl") or ext.get("reference")
            processed.append({
                "cat":         CAT,
                "folder_path": final_dir,
                "folder_name": folder_name,
                "mbl":         mbl_val,
                "files":       saved,
                "oi_number":   None,
                "extraction":  ext,  # ← pass extraction for destination fill
            })

        return processed

    # ══════════════════════════════════════════════════════════════════
    #  JORDEX UPLOAD (with destination fill BEFORE upload)
    # ══════════════════════════════════════════════════════════════════

    def _upload_to_jordex(self, jordex_page, tracker: Tracker, items: list):
        doc_type, display_name = JORDEX_MAPPING[CAT]
        normalize_dashboard_filters(jordex_page)

        for item in items:
            if self._stop_evt.is_set(): break
            query = item.get("mbl") or item.get("folder_name")
            if not query: continue

            query = normalize_oi_reference(query)

            row_index = 0
            uploaded  = False
            while row_index < 10:
                success, rows_found = search_and_open(jordex_page, query, row_index=row_index)
                if not success: break

                # ── NEW: Fill destination BEFORE upload ──────────────
                extraction = item.get("extraction")
                if extraction and (extraction.get("pickup") or extraction.get("return")):
                    try:
                        self._fill_destination(jordex_page, extraction)
                    except Exception as e:
                        log.error(f"[{SERVICE_KEY}] Destination fill failed: {e}")
                # ─────────────────────────────────────────────────────

                upload_attachments(jordex_page, item["folder_path"], doc_type, display_name)
                go_back(jordex_page)
                uploaded = True
                self._uploaded += 1
                row_index += 1
                if rows_found <= row_index: break

            if uploaded:
                tracker.update_status(CAT, item.get("conv_id"), "uploaded")

    # ══════════════════════════════════════════════════════════════════
    #  DESTINATION FILL — View Routing → 3. Destination
    # ══════════════════════════════════════════════════════════════════

    def _fill_destination(self, page: Page, extraction: dict):
        """
        Open View Routing → Destination tab → for each container:
          3.1 Pick-up Terminal: fill address + reference
          3.3 Return Terminal: fill address + reference
        Then save and go back to shipment detail.

        Data comes from extraction JSON:
          pickup.address, pickup.reference, pickup.reference_mode
          return.references[] (per-container), return.address, return.reference
        """
        pickup  = extraction.get("pickup", {})
        returns = extraction.get("return", {})
        containers = extraction.get("containers", [])

        pickup_address = (pickup.get("address") or "").strip()
        pickup_ref     = (pickup.get("reference") or "").strip()
        ref_mode       = (returns.get("reference_mode") or "single").lower()

        # Build per-container return lookup
        return_lookup = {}  # container_no → {address, reference}
        for r in returns.get("references", []):
            cno = (r.get("container_no") or "").strip().upper()
            if cno:
                addr_lines = (r.get("address") or "").strip().split("\n")
                return_lookup[cno] = {
                    "address":   addr_lines[0].strip() if addr_lines else "",
                    "reference": (r.get("reference") or "").strip(),
                }

        # Fallback for single-mode return
        default_return_addr = ""
        default_return_ref  = ""
        if ref_mode == "single" or not return_lookup:
            addr_lines = (returns.get("address") or "").strip().split("\n")
            default_return_addr = addr_lines[0].strip() if addr_lines else ""
            default_return_ref  = (returns.get("reference") or "").strip()

        if not pickup_address and not default_return_addr and not return_lookup:
            log.info(f"[{SERVICE_KEY}] No destination data to fill — skipping")
            return

        log.info(f"[{SERVICE_KEY}] Filling destination: pickup='{pickup_address}' "
                 f"ref='{pickup_ref}' return_mode='{ref_mode}' containers={len(containers)}")

        # ── Open View Routing ────────────────────────────────────────
        if not self._open_view_routing(page):
            return

        # ── Wait for sidebar ────────────────────────────────────────
        try:
            page.locator(".cargo-tab__block").first.wait_for(state="visible", timeout=8000)
        except Exception:
            pass

        sidebar_count = page.evaluate("""() => {
            const c = document.querySelector('.cargo-tab__content');
            return c ? c.querySelectorAll('.cargo-tab__block').length
                     : document.querySelectorAll('.cargo-tab__block').length;
        }""") or 0

        log.info(f"[{SERVICE_KEY}] Sidebar has {sidebar_count} container block(s)")

        # ── Process each container ──────────────────────────────────
        for idx in range(sidebar_count):
            if self._stop_evt.is_set(): break

            # Click sidebar block
            if idx > 0:
                page.evaluate(f"""() => {{
                    const c = document.querySelector('.cargo-tab__content');
                    const blocks = c ? c.querySelectorAll('.cargo-tab__block')
                                     : document.querySelectorAll('.cargo-tab__block');
                    if (blocks[{idx}]) blocks[{idx}].click();
                }}""")
                page.wait_for_timeout(2000)

            # Read container number from sidebar text
            sidebar_cno = self._read_sidebar_container_no(page, idx)
            log.info(f"[{SERVICE_KEY}] Container [{idx+1}/{sidebar_count}]: {sidebar_cno}")

            # Resolve return data for this container
            if sidebar_cno and sidebar_cno in return_lookup:
                ret = return_lookup[sidebar_cno]
                return_addr = ret["address"]
                return_ref  = ret["reference"]
            else:
                return_addr = default_return_addr
                return_ref  = default_return_ref

            # ── Click Destination tab ────────────────────────────────
            try:
                dest_tab = page.locator("#tab-destination, .el-tabs__item:has-text('Destination')").first
                if dest_tab.is_visible(timeout=3000):
                    dest_tab.click()
                    page.wait_for_timeout(1500)
                else:
                    log.warning(f"[{SERVICE_KEY}] Destination tab not visible")
                    continue
            except Exception as e:
                log.warning(f"[{SERVICE_KEY}] Destination tab click failed: {e}")
                continue

            # ── 3.1 PICK-UP TERMINAL ─────────────────────────────────
            if pickup_address:
                self._fill_section_terminal(
                    page, section_nth=2, terminal_name=pickup_address,
                    label="Pick-up"
                )
            if pickup_ref:
                self._fill_section_reference(
                    page, section_nth=2, ref_value=pickup_ref,
                    label="Pick-up"
                )

            # ── 3.3 RETURN TERMINAL ──────────────────────────────────
            if return_addr:
                self._fill_section_terminal(
                    page, section_nth=4, terminal_name=return_addr,
                    label="Return"
                )
            if return_ref:
                self._fill_section_reference(
                    page, section_nth=4, ref_value=return_ref,
                    label="Return"
                )

        # ── Save ────────────────────────────────────────────────────
        self._save_routing(page)

        # ── Go back to shipment detail ──────────────────────────────
        self._go_back_from_routing(page)
        log.info(f"[{SERVICE_KEY}] Destination fill complete")

    # ══════════════════════════════════════════════════════════════════
    #  DESTINATION HELPERS
    # ══════════════════════════════════════════════════════════════════

    def _open_view_routing(self, page: Page) -> bool:
        """Click View Routing, set zoom to 1.0, wait for sidebar."""
        try:
            # Wait up to 15s for the View routing button to appear (SPA load)
            clicked = False
            for _ in range(15):
                clicked = page.evaluate("""() => {
                    const el = document.querySelector('.routing-sidebar__routing-label');
                    if (el) { el.click(); return true; }
                    const all = [...document.querySelectorAll('*')];
                    const vr = all.find(e => e.childElementCount === 0
                        && e.innerText?.trim() === 'View routing');
                    if (vr) { vr.click(); return true; }
                    return false;
                }""")
                if clicked:
                    break
                page.wait_for_timeout(1000)
                
            if not clicked:
                log.warning(f"[{SERVICE_KEY}] View Routing button not found")
                return False

            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(3000)

            # Set zoom to 1.0 for stable selectors
            page.evaluate("""() => {
                let style = document.getElementById('jordex-zoom-style');
                if (!style) {
                    style = document.createElement('style');
                    style.id = 'jordex-zoom-style';
                    document.head.appendChild(style);
                }
                style.innerHTML = 'body { zoom: 1.0 !important; }';
            }""")
            page.wait_for_timeout(1000)
            return True

        except Exception as e:
            log.error(f"[{SERVICE_KEY}] Failed to open View Routing: {e}")
            return False

    def _read_sidebar_container_no(self, page: Page, idx: int) -> str:
        """Read container number from sidebar block text."""
        block_text = page.evaluate(f"""() => {{
            const c = document.querySelector('.cargo-tab__content');
            const blocks = c ? c.querySelectorAll('.cargo-tab__block')
                             : document.querySelectorAll('.cargo-tab__block');
            return blocks[{idx}] ? blocks[{idx}].innerText.trim() : '';
        }}""") or ""

        for line in block_text.split("\n"):
            line = line.strip()
            if re.match(r'^[A-Z]{4}\d{7}$', line):
                return line
        return ""

    def _fill_section_terminal(self, page: Page, section_nth: int,
                                terminal_name: str, label: str):
        """
        Fill a terminal address in Destination section via address book search.

        section_nth: 2 for Pick-up (3.1), 4 for Return (3.3)
        Maps to: #pane-destination .routing-tab-panel__body > div:nth-child(N)
        """
        log.info(f"[{SERVICE_KEY}]   {label} Terminal: searching '{terminal_name}'")

        # Check if already filled
        existing = self._read_existing_terminal(page, section_nth)
        if existing:
            # Compare: if same terminal, skip
            if terminal_name.upper()[:20] in existing.upper():
                log.info(f"[{SERVICE_KEY}]   {label} Terminal already correct: '{existing[:50]}'")
                return
            else:
                log.info(f"[{SERVICE_KEY}]   {label} Terminal different: existing='{existing[:40]}' "
                         f"new='{terminal_name[:40]}' — overwriting")
                # Delete existing terminal first
                self._delete_existing_terminal(page, section_nth)

        # Click address book button
        addr_clicked = False
        try:
            book_btn = page.locator(
                f"#pane-destination .routing-tab-panel__body > div:nth-child({section_nth}) "
                f".address-select-toolbar > button"
            ).first
            if book_btn.is_visible(timeout=3000):
                book_btn.click(timeout=3000)
                addr_clicked = True
                log.info(f"[{SERVICE_KEY}]   {label} Address book clicked")
        except Exception as e:
            log.warning(f"[{SERVICE_KEY}]   {label} Address book button failed: {e}")

        # Fallback: broader selector
        if not addr_clicked:
            try:
                book_btn = page.locator(
                    f"#pane-destination div:nth-child({section_nth}) "
                    f".address-select__toolbar button"
                ).first
                if book_btn.is_visible(timeout=2000):
                    book_btn.click(timeout=2000)
                    addr_clicked = True
            except Exception:
                pass

        if not addr_clicked:
            log.warning(f"[{SERVICE_KEY}]   {label} Could not click address book — skipping")
            return

        page.wait_for_timeout(2000)

        # Search in dialog
        try:
            dialog = page.get_by_role("dialog", name="Select address")
            dialog.wait_for(state="visible", timeout=5000)

            search_box = dialog.get_by_role("textbox", name="Search")
            search_box.click()
            search_box.fill("")

            # Use first line of terminal name for search
            search_term = terminal_name.split("\n")[0].strip()
            # If very long, use first 3 words
            words = search_term.split()
            if len(words) > 3:
                search_term = " ".join(words[:3])

            search_box.fill(search_term)
            page.wait_for_timeout(2000)

            # Select matching row
            result = page.evaluate("""(target) => {
                const dialogs = [...document.querySelectorAll('.el-dialog, .el-dialog__wrapper')]
                    .filter(d => d.offsetParent !== null || d.style.display !== 'none');
                for (const dialog of dialogs) {
                    const rows = [...dialog.querySelectorAll('table tbody tr, .el-table__body tr')];
                    for (let i = 0; i < rows.length; i++) {
                        const cells = rows[i].querySelectorAll('td');
                        const name = (cells[0]?.innerText || '').trim().toLowerCase();
                        if (name.includes(target) || target.includes(name)) {
                            rows[i].click();
                            return { matched: true, row: i, name: name };
                        }
                    }
                    if (rows.length > 0) {
                        rows[0].click();
                        return { matched: false, row: 0 };
                    }
                }
                return { matched: false, row: -1 };
            }""", search_term.lower().strip()) or {}

            log.info(f"[{SERVICE_KEY}]   {label} Terminal search result: {result}")
            page.wait_for_timeout(1500)

        except Exception as e:
            log.warning(f"[{SERVICE_KEY}]   {label} Terminal search failed: {e}")
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            except Exception:
                pass

    def _fill_section_reference(self, page: Page, section_nth: int,
                                 ref_value: str, label: str):
        """
        Fill the Reference field in a Destination section.

        section_nth: 2 for Pick-up (3.1), 4 for Return (3.3)
        """
        log.info(f"[{SERVICE_KEY}]   {label} Reference: '{ref_value}'")

        try:
            # Strategy 1: Scoped to section via div:nth-child + placeholder
            ref_filled = page.evaluate(f"""(refVal) => {{
                const pane = document.querySelector('#pane-destination');
                if (!pane) return false;
                const body = pane.querySelector('.routing-tab-panel__body');
                if (!body) return false;
                const section = body.children[{section_nth - 1}];
                if (!section) return false;

                const inputs = [...section.querySelectorAll('input[placeholder="Reference"]')]
                    .filter(i => i.offsetParent !== null);
                if (!inputs.length) return false;

                const inp = inputs[0];
                if (inp.readOnly) return false;

                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, '');
                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                setter.call(inp, refVal);
                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                return true;
            }}""", ref_value)

            if ref_filled:
                log.info(f"[{SERVICE_KEY}]   {label} Reference filled OK")
                page.wait_for_timeout(300)
                return

            # Strategy 2: Use nth Reference textbox
            # Pick-up = section 3.1 → first Reference in pane
            # Return = section 3.3 → third Reference (nth(2))
            if section_nth == 2:
                ref_nth = 0
            elif section_nth == 4:
                ref_nth = 2
            else:
                ref_nth = 0

            ref_input = page.get_by_role("textbox", name="Reference").nth(ref_nth)
            if ref_input.is_visible(timeout=2000):
                ref_input.click()
                ref_input.fill("")
                ref_input.fill(ref_value)
                page.wait_for_timeout(300)
                log.info(f"[{SERVICE_KEY}]   {label} Reference filled via nth({ref_nth})")
            else:
                log.warning(f"[{SERVICE_KEY}]   {label} Reference input not visible")

        except Exception as e:
            log.warning(f"[{SERVICE_KEY}]   {label} Reference fill failed: {e}")

    def _read_existing_terminal(self, page: Page, section_nth: int) -> str:
        """Read existing terminal name from a Destination section."""
        try:
            return page.evaluate(f"""() => {{
                const pane = document.querySelector('#pane-destination');
                if (!pane) return '';
                const body = pane.querySelector('.routing-tab-panel__body');
                if (!body) return '';
                const section = body.children[{section_nth - 1}];
                if (!section) return '';
                const fa = section.querySelector('.full-address-name');
                if (fa && fa.textContent.trim()) return fa.textContent.trim();
                return '';
            }}""") or ""
        except Exception:
            return ""

    def _delete_existing_terminal(self, page: Page, section_nth: int):
        """Delete existing terminal in a section (click the delete/close button)."""
        try:
            page.evaluate(f"""() => {{
                const pane = document.querySelector('#pane-destination');
                if (!pane) return;
                const body = pane.querySelector('.routing-tab-panel__body');
                if (!body) return;
                const section = body.children[{section_nth - 1}];
                if (!section) return;
                // Look for delete/close button in address-select area
                const btns = [...section.querySelectorAll(
                    '.address-select__body--select button'
                )].filter(b => b.offsetParent !== null);
                // Delete button is usually the last one (after edit and book)
                if (btns.length > 0) {{
                    btns[btns.length - 1].click();
                }}
            }}""")
            page.wait_for_timeout(1000)
            log.info(f"[{SERVICE_KEY}]   Deleted existing terminal in section {section_nth}")
        except Exception as e:
            log.warning(f"[{SERVICE_KEY}]   Delete terminal failed: {e}")

    def _save_routing(self, page: Page):
        """Click Save on the routing page."""
        try:
            save_btn = page.locator("button:has-text('Save'):visible").first
            if save_btn.is_visible(timeout=5000):
                save_btn.click()
                page.wait_for_timeout(2500)
                try:
                    ok = page.locator("button:has-text('OK'):visible").first
                    if ok.is_visible(timeout=2000):
                        ok.click()
                        page.wait_for_timeout(1000)
                except Exception:
                    pass
                log.info(f"[{SERVICE_KEY}] Routing saved")
            else:
                log.warning(f"[{SERVICE_KEY}] Save button not visible")
        except Exception as e:
            log.warning(f"[{SERVICE_KEY}] Save failed: {e}")

    def _go_back_from_routing(self, page: Page):
        """Navigate back from View Routing to shipment detail."""
        # Restore zoom
        try:
            page.evaluate("""() => {
                let s = document.getElementById('jordex-zoom-style');
                if (s) s.innerHTML = 'body { zoom: 0.75 !important; }';
                else document.documentElement.style.zoom = '0.75';
            }""")
        except Exception:
            pass

        # Click back
        back_ok = False
        try:
            back_ok = page.evaluate("""() => {
                const hdr = document.querySelector('.el-page-header__left');
                if (hdr) { hdr.click(); return true; }
                const icon = document.querySelector('.el-icon-back');
                if (icon) {
                    const btn = icon.closest('button') || icon.closest('[role="button"]');
                    if (btn) { btn.click(); return true; }
                    icon.click(); return true;
                }
                const els = [...document.querySelectorAll('button, span, a')];
                const b = els.find(e => (e.innerText || '').trim() === 'Back');
                if (b) { b.click(); return true; }
                return false;
            }""")
            if back_ok:
                page.wait_for_timeout(3000)
        except Exception:
            pass

        if not back_ok:
            try:
                page.go_back(timeout=10000)
                page.wait_for_timeout(3000)
            except Exception:
                pass

        # Verify we're back on shipment detail
        for v in range(5):
            on_detail = page.evaluate("""() => {
                for (const t of document.querySelectorAll('.el-tabs__item'))
                    if (t.textContent.trim() === 'Parties') return true;
                return false;
            }""")
            if on_detail: break
            page.wait_for_timeout(1500)
            if v == 2:
                try:
                    page.go_back(timeout=5000)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass