

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
    mark_as_unread, search_jordex_with_fallback,
)
from outlook.session import OutlookSession
from jordex.login import JordexSession
from jordex.browser import normalize_dashboard_filters, search_and_open, go_back, apply_zoom
from jordex.documents import upload_attachments
from services.delivery_order.extractor import extract_delivery_order
from services.delivery_order.evergreen_portal import scrape_evergreen_depot

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
                items = self._process_batch(outlook_page, tracker, pw=pw)
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
                    except: pass
            if pw:
                try: pw.stop()
                except: pass
            if self.status != "error":
                self.status = "idle"

    # ══════════════════════════════════════════════════════════════════
    #  EMAIL PROCESSING (download + extract)
    # ══════════════════════════════════════════════════════════════════

    def _process_batch(self, page, tracker: Tracker, pw=None) -> list:
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
                multi_items = self._process_multi_pdfs(pdf_files, temp_files, base, subject, cid, pw=pw)
                cleanup_temp(temp_files)

                if multi_items:
                    all_mbls  = [it["mbl"] for it in multi_items if it.get("mbl")]
                    all_files = [f for it in multi_items for f in it.get("files", [])]
                    all_secs  = [it.get("secondary_ref") for it in multi_items if it.get("secondary_ref")]
                    tracker.mark(
                        CAT, cid, subject,
                        multi_items[0]["folder_name"], all_files, "downloaded",
                        mbl=all_mbls[0] if all_mbls else None,
                        secondary_ref=all_secs[0] if all_secs else None,
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

    def _process_multi_pdfs(self, pdf_files, all_temp_files, base, subject, cid, pw=None) -> list:
        """
        Process multiple PDFs. Each PDF is classified as:
        - delivery_order → full extraction, upload as "Container release" / "DO"
        - acknowledgement → no extraction, upload as "Additional Files"
        - invoice → skip entirely
        """
        pdf_to_ext = {}
        for pdf_path in pdf_files:
            ext = extract_delivery_order(pdf_path, gemini_model=gemini_model)

            # ── Evergreen portal scrape ──────────────────────────────
            if ext.get("scac") == "EGLV" and ext.get("mbl"):
                log.info(f"[{SERVICE_KEY}] Evergreen carrier detected for '{os.path.basename(pdf_path)}' — scraping depot portal")
                try:
                    if pw:
                        ev_data = scrape_evergreen_depot(
                            mbl_full=ext["mbl"],
                            pw=pw,
                            gemini_model=gemini_model,
                        )
                    else:
                        log.warning(f"[{SERVICE_KEY}] No Playwright context available for Evergreen scrape")
                        ev_data = None
                    if ev_data:
                        ext = self._merge_evergreen_data(ext, ev_data)
                        log.info(f"[{SERVICE_KEY}] Evergreen depot data merged for {ext['mbl']}")
                    else:
                        log.warning(f"[{SERVICE_KEY}] Evergreen portal scrape returned nothing — proceeding without depot data")
                except Exception as ev_err:
                    log.error(f"[{SERVICE_KEY}] Evergreen portal error: {ev_err}", exc_info=True)
            # ────────────────────────────────────────────────────────

            folder_name = ext.get("folder_name") or ""

            # ── Acknowledgement MBL fix ──────────────────────────────
            # If no folder_name (MBL missing from doc), try to recover the
            # BL from the subject and prepend the SCAC code so Jordex can
            # find the shipment.
            if not folder_name and ext.get("doc_subtype") in ("acknowledgement", "other"):
                scac = ext.get("scac", "")
                # Try to extract a BL-like number from the subject
                # Pattern: 6–15 digit/alphanum group that appears in the subject
                bl_candidates = re.findall(r'\b([A-Z0-9]{6,15})\b', subject.upper())
                # Filter out known non-BL tokens (request numbers usually mixed-case alpha)
                for cand in bl_candidates:
                    if re.match(r'^\d+$', cand) and len(cand) >= 6:
                        # Pure numeric → likely BL (e.g. 270557106)
                        folder_name = (scac + cand) if scac else cand
                        ext["mbl"] = folder_name
                        ext["folder_name"] = folder_name
                        log.info(f"[{SERVICE_KEY}] Recovered BL from subject: '{folder_name}'")
                        break

            if not folder_name:
                folder_name = subject_folder_fallback(subject)

            pdf_to_ext[pdf_path] = (ext, folder_name)
    
        folder_groups: dict[str, dict] = {}
        for pdf_path, (ext, folder_name) in pdf_to_ext.items():
            if folder_name not in folder_groups:
                folder_groups[folder_name] = {
                    "extraction": None,      # will hold the DO extraction (not ack)
                    "pdfs": [],
                    "ack_pdfs": [],          # NEW: acknowledgement PDFs
                }
    
            doc_subtype = ext.get("doc_subtype", "delivery_order")
    
            if doc_subtype == "acknowledgement":
                # Acknowledgement → no extraction, upload as Additional Files
                folder_groups[folder_name]["ack_pdfs"].append(pdf_path)
                log.info(f"[{SERVICE_KEY}] '{os.path.basename(pdf_path)}' is Acknowledgement → Additional Files")
            elif doc_subtype == "invoice":
                # Invoice → skip entirely
                log.info(f"[{SERVICE_KEY}] '{os.path.basename(pdf_path)}' is Invoice → skipping")
                continue
            else:
                # Actual Delivery Order → full extraction
                folder_groups[folder_name]["pdfs"].append(pdf_path)
                # Use the DO extraction (not acknowledgement) for destination fill
                if folder_groups[folder_name]["extraction"] is None:
                    folder_groups[folder_name]["extraction"] = ext
                log.info(f"[{SERVICE_KEY}] '{os.path.basename(pdf_path)}' is Delivery Order → Container release")
    
        # Non-PDF temps go to first folder
        non_pdfs = [f for f in all_temp_files if not f.lower().endswith(".pdf")]
        first_folder = next(iter(folder_groups)) if folder_groups else None
        if first_folder and non_pdfs:
            folder_groups[first_folder]["pdfs"] = non_pdfs + folder_groups[first_folder]["pdfs"]
    
        processed = []
        for folder_name, group in folder_groups.items():
            final_dir = os.path.join(base, folder_name)
            os.makedirs(final_dir, exist_ok=True)
    
            # Move DO files
            saved_do = []
            for tmp in group["pdfs"]:
                if not os.path.exists(tmp):
                    continue
                s = move_file_to_folder(tmp, final_dir)
                if s:
                    saved_do.append(s)
    
            # Move Acknowledgement files
            saved_ack = []
            for tmp in group["ack_pdfs"]:
                if not os.path.exists(tmp):
                    continue
                s = move_file_to_folder(tmp, final_dir)
                if s:
                    saved_ack.append(s)
    
            ext = group["extraction"]
            if ext:
                ext["subject"] = subject
                save_result(ext, final_dir)
                mbl_val = ext.get("mbl") or ext.get("reference")
            else:
                # Only acknowledgements in this folder, no actual DO
                mbl_val = None
    
            # Grab container_no for secondary fallback search
            sec_ref = None
            if ext:
                containers = ext.get("containers") or []
                if containers and isinstance(containers, list):
                    sec_ref = containers[0].get("container_no") if isinstance(containers[0], dict) else containers[0]

            processed.append({
                "cat":           CAT,
                "folder_path":   final_dir,
                "folder_name":   folder_name,
                "mbl":           mbl_val,
                "files":         saved_do + saved_ack,
                "do_files":      saved_do,
                "ack_files":     saved_ack,
                "oi_number":     None,
                "extraction":    ext,
                "conv_id":       cid,
                "secondary_ref": sec_ref,
            })
    
        return processed

    def _merge_evergreen_data(self, ext: dict, ev_data: dict) -> dict:
        """
        Merge Evergreen portal scrape result into the DO extraction dict.
        Overwrites pickup and return sections with the live-scraped depot data.
        Preserves all other fields (mbl, containers, scac, etc.).
        """
        ext["pickup"] = ev_data["pickup"]
        ext["return"] = ev_data["return"]
        ext["evergreen_bl"] = ev_data.get("evergreen_bl", "")
        # Clear the portal_required flag now that we have real data
        if ext.get("flag") == "evergreen_portal_required":
            ext["flag"] = ""
        return ext

    # ══════════════════════════════════════════════════════════════════
    #  JORDEX UPLOAD (with destination fill BEFORE upload)
    # ══════════════════════════════════════════════════════════════════

    def _build_do_file_map(self, do_files, doc_type, display_name):
        """Build file_map for DO files: type='Container release', name='DO'."""
        file_map = {}
        for filepath in do_files:
            filename = os.path.basename(filepath)
            file_map[filename] = (doc_type, display_name)
        return file_map
     
    def _build_ack_file_map(self, ack_files):
        """Build file_map for Acknowledgement files: type='Additional Files', keep original name."""
        file_map = {}
        for filepath in ack_files:
            filename = os.path.basename(filepath)
            # Use original filename (without .pdf extension) as display name
            name_without_ext = os.path.splitext(filename)[0]
            file_map[filename] = ("Additional Files", name_without_ext)
        return file_map
    
    def _upload_to_jordex(self, jordex_page, outlook_page, tracker, items):
        """
        Upload to Jordex with doc-subtype awareness:
          - DO files → "Container release" / "DO" (existing behavior)
          - Acknowledgement files → "Additional Files" / original filename
          - Destination fill → ONLY if extraction exists (i.e. actual DO)
        """
        doc_type, display_name = JORDEX_MAPPING[CAT]

 
        # Deduplicate: track folder_names already uploaded this batch
        uploaded_folders: set[str] = set()

        for item in items:
            if self._stop_evt.is_set():
                break
            query = item.get("mbl") or item.get("folder_name")
            if not query:
                continue
 
            folder_name = item.get("folder_name") or query
            if folder_name in uploaded_folders:
                log.info(
                    f"[{SERVICE_KEY}] Skipping duplicate folder '{folder_name}' "
                    f"(conv_id={item.get('conv_id', '')[:20]}…) — already uploaded this batch"
                )
                tracker.update_status(CAT, item.get("conv_id"), "uploaded")
                continue

            query = normalize_oi_reference(query)
 
            success, used_ref, rows_found = search_jordex_with_fallback(
                jordex_page=jordex_page,
                outlook_page=outlook_page,
                primary_ref=query,
                secondary_ref=item.get("secondary_ref"),
                conv_id=item.get("conv_id"),
                tracker=tracker,
                cat=CAT,
                service_key=SERVICE_KEY,
                search_fn=search_and_open,
            )
            if not success:
                continue

            row_index = 0
            uploaded = False
            while row_index < 10:
                success, rows_found = search_and_open(jordex_page, used_ref, row_index=row_index)
                if not success:
                    break
 
                # ── Destination fill: ONLY for actual delivery orders ────
                extraction = item.get("extraction")
                if extraction and (extraction.get("pickup") or extraction.get("return")):
                    # Only fill if this is a real DO extraction (not acknowledgement)
                    if extraction.get("doc_subtype") != "acknowledgement":
                        try:
                            self._fill_destination(jordex_page, extraction)
                        except Exception as e:
                            log.error(f"[{SERVICE_KEY}] Destination fill failed: {e}")
 
                # ── Upload DO files as "Container release" / "DO" ────────
                do_files = item.get("do_files", [])
                if do_files:
                    # Build a file_map so only DO files get uploaded with correct type
                    # upload_attachments will pick up PDFs from folder_path
                    upload_attachments(
                        jordex_page, item["folder_path"],
                        doc_type, display_name,
                        file_map=self._build_do_file_map(do_files, doc_type, display_name),
                    )
 
                # ── Upload Acknowledgement files as "Additional Files" ───
                ack_files = item.get("ack_files", [])
                if ack_files:
                    upload_attachments(
                        jordex_page, item["folder_path"],
                        "Additional Files", None,  # keep original filename
                        file_map=self._build_ack_file_map(ack_files),
                    )
 
                go_back(jordex_page)
                uploaded = True
                self._uploaded += 1
                row_index += 1
                if rows_found <= row_index:
                    break
 
            if uploaded:
                tracker.update_status(CAT, item.get("conv_id"), "uploaded")
                uploaded_folders.add(folder_name)

    # ══════════════════════════════════════════════════════════════════
    #  DESTINATION FILL — View Routing → 3. Destination
    # ══════════════════════════════════════════════════════════════════

    def _fill_destination(self, page: Page, extraction: dict):
        """
        Open View Routing → Destination tab → for each container:
          3.1 Pick-up Terminal: fill address + reference
          3.3 Return Terminal: fill address + reference
        Then save and go back to shipment detail.
        """
        pickup   = extraction.get("pickup", {})
        returns  = extraction.get("return", {})
        containers = extraction.get("containers", [])
 
        pickup_address = (pickup.get("address") or "").strip()
        pickup_ref     = (pickup.get("reference") or "").strip()
        ref_mode       = (returns.get("reference_mode") or "single").lower()
 
        # Build per-container return lookup
        return_lookup = {}
        for r in returns.get("references", []):
            cno = (r.get("container_no") or "").strip().upper()
            if cno:
                addr_lines = (r.get("address") or "").strip().split("\n")
                return_lookup[cno] = {
                    "address":   addr_lines[0].strip() if addr_lines else "",
                    "reference": (r.get("reference") or "").strip(),
                }
 
        default_return_addr = ""
        default_return_ref  = ""
        if ref_mode == "single" or not return_lookup:
            addr_lines = (returns.get("address") or "").strip().split("\n")
            default_return_addr = addr_lines[0].strip() if addr_lines else ""
            default_return_ref  = (returns.get("reference") or "").strip()
 
        if not pickup_address and not default_return_addr and not return_lookup:
            log.info(f"[{SERVICE_KEY}] No destination data to fill — skipping")
            return
 
        log.info(
            f"[{SERVICE_KEY}] Filling destination: pickup='{pickup_address}' "
            f"ref='{pickup_ref}' return_mode='{ref_mode}' containers={len(containers)}"
        )
 
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
            if self._stop_evt.is_set():
                break
 
            # Click sidebar block + wait for content reload
            if idx > 0:
                page.evaluate(f"""() => {{
                    const c = document.querySelector('.cargo-tab__content');
                    const blocks = c ? c.querySelectorAll('.cargo-tab__block')
                                     : document.querySelectorAll('.cargo-tab__block');
                    if (blocks[{idx}]) blocks[{idx}].click();
                }}""")
                page.wait_for_timeout(2500)
                # Wait for any loading spinner to disappear
                try:
                    page.locator(".el-loading-mask").wait_for(state="hidden", timeout=5000)
                except Exception:
                    pass
 
            sidebar_cno = self._read_sidebar_container_no(page, idx)
            log.info(f"[{SERVICE_KEY}] Container [{idx + 1}/{sidebar_count}]: {sidebar_cno}")
 
            # Resolve return data for this container
            if sidebar_cno and sidebar_cno in return_lookup:
                ret = return_lookup[sidebar_cno]
                return_addr = ret["address"]
                return_ref  = ret["reference"]
            else:
                return_addr = default_return_addr
                return_ref  = default_return_ref
 
            # ── ALWAYS click Destination tab (it resets after sidebar click) ──
            if not self._ensure_destination_tab(page):
                log.warning(f"[{SERVICE_KEY}] Cannot activate Destination tab for container {idx + 1}")
                continue
 
            # ── 3.1 PICK-UP TERMINAL ─────────────────────────────────
            pickup_addr_for_container = pickup_address
            pickup_ref_for_container  = pickup_ref
 
            if sidebar_cno and pickup.get("references"):
                for pr in pickup.get("references", []):
                    if (pr.get("container_no") or "").strip().upper() == sidebar_cno:
                        if pr.get("address"):
                            pickup_addr_for_container = pr["address"].split("\n")[0].strip()
                        if pr.get("reference"):
                            pickup_ref_for_container = pr["reference"]
                        break
 
            if pickup_addr_for_container:
                self._fill_section_terminal(
                    page, section_nth=2, terminal_name=pickup_addr_for_container,
                    label="Pick-up"
                )
            if pickup_ref_for_container:
                self._fill_section_reference(
                    page, section_nth=2, ref_value=pickup_ref_for_container,
                    label="Pick-up"
                )
 
            # ── Scroll Return section into view BEFORE interacting ───
            page.evaluate("""() => {
                const pane = document.querySelector('#pane-destination');
                if (!pane) return;
                const body = pane.querySelector('.routing-tab-panel__body');
                if (!body) return;
                const returnSection = body.children[3]; // div:nth-child(4)
                if (returnSection)
                    returnSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }""")
            page.wait_for_timeout(1000)
 
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
 
            # ── Save after each container ────────────────────────────
            self._save_routing(page)
 
        # ── Go back to shipment detail ──────────────────────────────
        self._go_back_from_routing(page)
        log.info(f"[{SERVICE_KEY}] Destination fill complete")
    
    def _ensure_destination_tab(self, page: Page) -> bool:
        """
        Click the Destination tab and verify #pane-destination is visible.
        Retries up to 3 times. Returns True if active.
        """
        for attempt in range(3):
            try:
                tab = page.locator("#tab-destination")
                if tab.count() > 0 and tab.first.is_visible(timeout=2000):
                    tab.first.click()
                    page.wait_for_timeout(1500)
                else:
                    tab = page.locator(".el-tabs__item:has-text('Destination')")
                    if tab.count() > 0 and tab.first.is_visible(timeout=2000):
                        tab.first.click()
                        page.wait_for_timeout(1500)
 
                pane = page.locator("#pane-destination")
                if pane.count() > 0 and pane.first.is_visible(timeout=2000):
                    return True
            except Exception as e:
                log.warning(f"[{SERVICE_KEY}] Destination tab attempt {attempt + 1}: {e}")
            page.wait_for_timeout(1000 * (attempt + 1))
        return False

    # ══════════════════════════════════════════════════════════════════
    #  DESTINATION HELPERS
    # ══════════════════════════════════════════════════════════════════

    def _open_view_routing(self, page: Page) -> bool:
        """Click View Routing, set zoom to 1.0, wait for sidebar."""
        try:
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
 
            # Set zoom to 1.0
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
 
            # Wait for loading to finish
            try:
                page.locator(".el-loading-mask").wait_for(state="hidden", timeout=8000)
            except Exception:
                pass
 
            return True
 
        except Exception as e:
            log.error(f"[{SERVICE_KEY}] Failed to open View Routing: {e}")
            return False
    def _score_and_click_best_row(self, page: Page, doc_address: str,
                                   label: str, min_score: int = 12) -> bool:
        """
        Score every row in the visible address dialog against the FULL doc
        address blob, using all 5 columns (Name, Street, Postal, City, Country).
 
        Scoring weights:
          Postal code exact match   → +10
          Street name + number      → +8
          Street name only          → +4
          City match                → +3
          Country match             → +2
          Name word matches         → +1 each (max +3)
 
        Returns True if a row was clicked (score ≥ min_score), False otherwise.
        All scoring runs inside a single page.evaluate() — no network calls.
        """
        result = page.evaluate("""(docAddr) => {
            // ── Normalize doc address ───────────────────────────────
            let doc = docAddr.toUpperCase()
                .replace(/[.,;:()\/\\-]/g, ' ')
                .replace(/\\bNETHERLANDS\\b/g, 'NL')
                .replace(/\\bBELGIUM\\b/g, 'BE')
                .replace(/\\bBELGIE\\b/g, 'BE')
                .replace(/\\bGERMANY\\b/g, 'DE')
                .replace(/\\bDEUTSCHLAND\\b/g, 'DE')
                .replace(/\\bFRANCE\\b/g, 'FR')
                .replace(/\\bUNITED KINGDOM\\b/g, 'GB')
                .replace(/\\bGREAT BRITAIN\\b/g, 'GB')
                .replace(/\\bB\\.?V\\.?\\b/g, '')
                .replace(/\\bN\\.?V\\.?\\b/g, '')
                .replace(/\\bHAVENNUMMER\\b/g, '')
                .replace(/\\s+/g, ' ')
                .trim();
 
            // Doc address with ALL spaces removed (for postal matching)
            const docNoSpaces = doc.replace(/\\s/g, '');
 
            // ── Find visible dialog table ───────────────────────────
            const visibleDialog = [...document.querySelectorAll('.el-dialog')]
                .find(d => d.offsetParent !== null);
            if (!visibleDialog) return { clicked: false, reason: 'no-dialog' };
 
            const table = visibleDialog.querySelector('table');
            if (!table) return { clicked: false, reason: 'no-table' };
 
            // ── Read headers ────────────────────────────────────────
            const headers = [...table.querySelectorAll('thead th')]
                .map(h => h.innerText.trim().toUpperCase());
 
            const nameIdx   = headers.findIndex(h => h.includes('NAME'));
            const streetIdx = headers.findIndex(h => h.includes('STREET'));
            const postalIdx = headers.findIndex(h => h.includes('POSTAL'));
            const cityIdx   = headers.findIndex(h => h.includes('CITY'));
            const countryIdx = headers.findIndex(h => h.includes('COUNTRY'));
 
            // ── Score each row ──────────────────────────────────────
            const rows = [...table.querySelectorAll('tbody tr')];
            let bestIdx = -1;
            let bestScore = 0;
            let bestName = '';
            const scores = [];
 
            for (let i = 0; i < rows.length; i++) {
                const cells = rows[i].querySelectorAll('td');
                if (!cells.length) continue;
 
                const cell = (idx) => idx >= 0 && cells[idx]
                    ? cells[idx].innerText.trim() : '';
 
                const name    = cell(nameIdx).toUpperCase();
                const street  = cell(streetIdx).toUpperCase();
                const postal  = cell(postalIdx).toUpperCase();
                const city    = cell(cityIdx).toUpperCase();
                const country = cell(countryIdx).toUpperCase();
 
                let score = 0;
 
                // ── Postal code match (+10) ─────────────────────────
                if (postal.length >= 4) {
                    const postalClean = postal.replace(/[\\s-]/g, '');
                    if (docNoSpaces.includes(postalClean)) {
                        score += 10;
                    }
                }
 
                // ── Street match (+8 full, +4 name only) ────────────
                if (street.length >= 3) {
                    // Extract street name (longest word ≥5 chars) and number
                    const streetClean = street
                        .replace(/[.,;:()\/\\-]/g, ' ')
                        .replace(/\\s+/g, ' ').trim();
                    const streetWords = streetClean.split(' ');
 
                    // Find the main street name word (longest alphabetic word)
                    let streetName = '';
                    let streetNum = '';
                    for (const w of streetWords) {
                        if (/^[A-Z]{5,}/.test(w) && w.length > streetName.length) {
                            streetName = w;
                        }
                        if (!streetNum && /^\\d{1,5}$/.test(w)) {
                            streetNum = w;
                        }
                    }
 
                    if (streetName && doc.includes(streetName)) {
                        if (streetNum && doc.includes(streetNum)) {
                            // Check street name and number are near each other in doc
                            const namePos = doc.indexOf(streetName);
                            const numPos = doc.indexOf(streetNum, Math.max(0, namePos - 5));
                            if (Math.abs(numPos - namePos) < 40) {
                                score += 8;  // Full street match
                            } else {
                                score += 4;  // Street name found but number far away
                            }
                        } else {
                            score += 4;  // Street name only
                        }
                    }
                }
 
                // ── City match (+3) ─────────────────────────────────
                if (city.length >= 3) {
                    const cityClean = city.replace(/[()]/g, '').split(/[\/,]/)[0].trim();
                    if (cityClean && doc.includes(cityClean)) {
                        score += 3;
                    }
                }
 
                // ── Country match (+2) ──────────────────────────────
                if (country.length >= 2) {
                    if (doc.includes(country)) {
                        score += 2;
                    }
                }
 
                // ── Name word match (+1 each, max +3) ───────────────
                if (name) {
                    const nameWords = name
                        .replace(/[.,;:()\/\\-]/g, ' ')
                        .split(/\\s+/)
                        .filter(w => w.length >= 4);
                    // Skip noise words
                    const noise = new Set([
                        'TERMINAL', 'TERMINALS', 'DEPOT', 'CONTAINER',
                        'PORT', 'PORTS', 'GROUP', 'INTERNATIONAL'
                    ]);
                    let nameScore = 0;
                    for (const w of nameWords) {
                        if (noise.has(w)) continue;
                        if (doc.includes(w)) {
                            nameScore++;
                            if (nameScore >= 3) break;
                        }
                    }
                    score += nameScore;
                }
 
                scores.push({ idx: i, score, name: name.substring(0, 50) });
 
                if (score > bestScore) {
                    bestScore = score;
                    bestIdx = i;
                    bestName = name.substring(0, 60);
                }
            }
 
            // ── Click best row if above threshold ───────────────────
            if (bestIdx >= 0 && bestScore >= arguments[1]) {
                const row = rows[bestIdx];
                row.click();
 
                // Also click radio button if present
                const radio = row.querySelector(
                    '.el-radio__input, .el-radio, input[type="radio"], ' +
                    '.el-radio__original, label.el-radio'
                );
                if (radio) radio.click();
 
                // Click first cell as backup trigger
                const firstCell = row.querySelector('td, [role="cell"]');
                if (firstCell) firstCell.click();
 
                return {
                    clicked: true,
                    score: bestScore,
                    index: bestIdx,
                    name: bestName,
                    total_rows: rows.length,
                    top3: scores.sort((a, b) => b.score - a.score).slice(0, 3)
                };
            }
 
            return {
                clicked: false,
                best_score: bestScore,
                best_name: bestName,
                total_rows: rows.length,
                top3: scores.sort((a, b) => b.score - a.score).slice(0, 3)
            };
        }""", doc_address, min_score) or {}
 
        if result.get("clicked"):
            log.info(
                f"[{SERVICE_KEY}]   {label} Scored row {result.get('index')} "
                f"(score={result.get('score')}, name='{result.get('name')}')"
            )
            top3 = result.get("top3", [])
            if len(top3) > 1:
                log.debug(
                    f"[{SERVICE_KEY}]   {label} Top 3 scores: "
                    + ", ".join(f"#{t['idx']}={t['score']}" for t in top3)
                )
            return True
        else:
            log.warning(
                f"[{SERVICE_KEY}]   {label} No confident match "
                f"(best_score={result.get('best_score', 0)}, "
                f"best='{result.get('best_name', '')}')"
            )
            top3 = result.get("top3", [])
            if top3:
                log.info(
                    f"[{SERVICE_KEY}]   {label} Top 3: "
                    + ", ".join(f"#{t['idx']}={t['score']}({t['name'][:25]})" for t in top3)
                )
            return False
    
    def _build_street_search_term(self, doc_address: str) -> str:
        """
        Extract the street name from a doc address blob for fallback search.
 
        Looks for pattern: long word (6+ chars) followed by a number.
        Examples:
          "... BUNSCHOTENWEG 200 ..." → "BUNSCHOTENWEG"
          "... EUROPAWEG 875 ..."     → "EUROPAWEG"
          "... MAASVLAKTEWEG 951 ..." → "MAASVLAKTEWEG"
        """
        # Pattern: word with 6+ alpha chars followed by a number within a few words
        matches = re.findall(
            r'\b([A-Za-z]{6,})\s+(\d{1,5})\b',
            doc_address.upper()
        )
 
        # Filter out noise words that look like streets but aren't
        noise = {
            "NETHERLANDS", "ROTTERDAM", "ANTWERPEN", "AMSTERDAM",
            "BELGIUM", "GERMANY", "HAVENNUMMER", "TERMINAL",
            "TERMINALS", "CONTAINER", "CONTAINERS",
        }
 
        for word, _num in matches:
            if word not in noise:
                return word
 
        # Fallback: just find the longest word that looks like a street
        # (words ending in common Dutch/German street suffixes)
        street_suffixes = ("WEG", "STRAAT", "LAAN", "KADE", "PLEIN", "GRACHT",
                           "SINGEL", "DIJK", "STEEG", "PAD", "BAAN", "DREEF",
                           "ALLEE", "STRASSE", "ROAD", "STREET", "AVENUE")
        words = re.findall(r'[A-Za-z]{5,}', doc_address.upper())
        for w in words:
            for suffix in street_suffixes:
                if w.endswith(suffix):
                    return w
 
        return ""
 
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
        Fill a terminal address via address book search with two-phase matching:
          Phase 1: Search by name → score all rows → click if confident
          Phase 2: Search by street name → score again → click if confident
          Phase 3: Last resort → click first row
        """
        log.info(f"[{SERVICE_KEY}]   {label} Terminal: searching '{terminal_name}'")
 
        # ── Scroll section into view ─────────────────────────────────
        page.evaluate(f"""() => {{
            const pane = document.querySelector('#pane-destination');
            if (!pane) return;
            const body = pane.querySelector('.routing-tab-panel__body');
            if (!body) return;
            const section = body.children[{section_nth - 1}];
            if (section) section.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
        }}""")
        page.wait_for_timeout(800)
 
        # ── Check if already filled correctly ────────────────────────
        existing = self._read_existing_terminal(page, section_nth)
        if existing:
            norm_existing = existing.upper().replace(",", "").strip()[:20]
            norm_new = terminal_name.upper().replace(",", "").strip()[:20]
            if norm_new in norm_existing or norm_existing in norm_new:
                log.info(f"[{SERVICE_KEY}]   {label} Terminal already correct: '{existing[:50]}'")
                return
            else:
                log.info(f"[{SERVICE_KEY}]   {label} Terminal different — overwriting")
                self._delete_existing_terminal(page, section_nth)
                page.wait_for_timeout(1000)
 
        # ── Open address book (with drawer detection + retry) ────────
        if not self._open_address_book_dialog(page, section_nth, label):
            return
 
        # ══════════════════════════════════════════════════════════════
        #  PHASE 1: Search by name, score results
        # ══════════════════════════════════════════════════════════════
        search_term = self._build_search_term(terminal_name)
        log.info(f"[{SERVICE_KEY}]   {label} Phase 1 search: '{search_term}'")
 
        if not self._fill_search_box(page, search_term, label):
            self._close_dialog_properly(page, label)
            return
 
        page.wait_for_timeout(2500)
 
        # Score and click
        matched = self._score_and_click_best_row(page, terminal_name, label, min_score=12)
 
        # ══════════════════════════════════════════════════════════════
        #  PHASE 2: If no confident match, search by street name
        # ══════════════════════════════════════════════════════════════
        if not matched:
            street_term = self._build_street_search_term(terminal_name)
            if street_term and street_term.upper() != search_term.upper():
                log.info(f"[{SERVICE_KEY}]   {label} Phase 2 search: '{street_term}'")
 
                # Clear and re-fill search box
                if self._fill_search_box(page, street_term, label):
                    page.wait_for_timeout(2500)
                    matched = self._score_and_click_best_row(
                        page, terminal_name, label, min_score=8
                    )
 
        # ══════════════════════════════════════════════════════════════
        #  PHASE 3: Last resort — click first row
        # ══════════════════════════════════════════════════════════════
        if not matched:
            log.warning(f"[{SERVICE_KEY}]   {label} No confident match — clicking first row as fallback")
            page.evaluate("""() => {
                const dialog = [...document.querySelectorAll('.el-dialog')]
                    .find(d => d.offsetParent !== null);
                if (!dialog) return;
                const row = dialog.querySelector('table tbody tr');
                if (row) {
                    row.click();
                    const radio = row.querySelector(
                        '.el-radio__input, .el-radio, input[type="radio"]');
                    if (radio) radio.click();
                }
            }""")
            page.wait_for_timeout(1000)
 
        # ── Close dialog properly ────────────────────────────────────
        page.wait_for_timeout(500)
        self._close_dialog_properly(page, label)
        page.wait_for_timeout(1000)
    def _open_address_book_dialog(self, page: Page, section_nth: int,
                                   label: str) -> bool:
        """
        Click address book button + handle drawer-vs-dialog detection.
        Returns True if the dialog Search textbox is visible and ready.
        """
        dialog_ready = False
        for attempt in range(2):
            addr_clicked = self._click_address_book_js(page, section_nth, label)
            if not addr_clicked:
                log.warning(f"[{SERVICE_KEY}]   {label} Could not click address book button")
                break
 
            page.wait_for_timeout(2000)
 
            # Check: did a drawer open instead of the dialog?
            drawer_opened = page.evaluate("""() => {
                const drawers = document.querySelectorAll('.el-drawer__wrapper');
                for (const d of drawers) {
                    if (getComputedStyle(d).display !== 'none') return true;
                }
                return false;
            }""")
 
            if drawer_opened:
                log.warning(
                    f"[{SERVICE_KEY}]   {label} Drawer opened instead of address book "
                    f"(attempt {attempt + 1}) — closing"
                )
                try:
                    close_drawer = page.get_by_role("button", name="close drawer")
                    if close_drawer.is_visible(timeout=2000):
                        close_drawer.click(timeout=3000)
                        page.wait_for_timeout(1500)
                except Exception:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(1000)
                    page.evaluate("""() => {
                        const drawers = document.querySelectorAll('.el-drawer__wrapper');
                        for (const d of drawers) d.style.display = 'none';
                        const masks = document.querySelectorAll('.v-modal');
                        for (const m of masks) m.remove();
                        document.body.classList.remove('el-popup-parent--hidden');
                    }""")
                    page.wait_for_timeout(500)
                continue
 
            # Check: is the Search textbox visible?
            for wait in range(3):
                try:
                    search_box = page.get_by_role("textbox", name="Search")
                    if search_box.is_visible(timeout=2000):
                        dialog_ready = True
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
 
            if dialog_ready:
                break
 
        if not dialog_ready:
            log.warning(f"[{SERVICE_KEY}]   {label} Address dialog did not appear after retries")
            self._cleanup_overlays(page)
 
        return dialog_ready
 
    def _click_address_book_js(self, page: Page, section_nth: int, label: str) -> bool:
        """
        Click the address book button using JS .click() on the exact recorded
        CSS selector. JS .click() bypasses any overlay (mf-drawer, v-modal).
 
        section_nth: 2 = Pick-up, 4 = Return
        """
        exact_selector = (
            f"#pane-destination > div > .el-card > .el-card__body > .el-form > div > "
            f".routing-tab-panel__body > div:nth-child({section_nth}) > div:nth-child(2) > "
            f"div:nth-child(3) > .el-form-item > .el-form-item__content > div > "
            f".address-select__body--select > .address-select__toolbar > "
            f".address-select-toolbar > button"
        )
 
        clicked = page.evaluate(f"""() => {{
            const btn = document.querySelector('{exact_selector}');
            if (btn) {{
                btn.scrollIntoView({{ block: 'center' }});
                btn.click();
                return true;
            }}
            return false;
        }}""")
 
        if clicked:
            log.info(f"[{SERVICE_KEY}]   {label} Address book clicked via JS exact selector")
            return True
 
        # Fallback: broader selector
        clicked = page.evaluate(f"""() => {{
            const pane = document.querySelector('#pane-destination');
            if (!pane) return false;
            const body = pane.querySelector('.routing-tab-panel__body');
            if (!body) return false;
            const section = body.children[{section_nth - 1}];
            if (!section) return false;
 
            // Find the address-select-toolbar button specifically
            const toolbar = section.querySelector('.address-select-toolbar');
            if (toolbar) {{
                const btn = toolbar.querySelector('button');
                if (btn) {{
                    btn.scrollIntoView({{ block: 'center' }});
                    btn.click();
                    return true;
                }}
            }}
 
            // Last resort: any button in address-select__body--select
            const areaBtn = section.querySelector(
                '.address-select__body--select .address-select__toolbar button');
            if (areaBtn) {{
                areaBtn.scrollIntoView({{ block: 'center' }});
                areaBtn.click();
                return true;
            }}
            return false;
        }}""")
 
        if clicked:
            log.info(f"[{SERVICE_KEY}]   {label} Address book clicked via JS fallback")
        else:
            log.warning(f"[{SERVICE_KEY}]   {label} Address book button not found in DOM")
        return bool(clicked)
    def _fill_search_box(self, page: Page, search_term: str, label: str) -> bool:
        """
        Clear and fill the Search textbox in the visible address dialog.
        Returns True if the search term was successfully entered.
        """
        try:
            search_box = page.get_by_role("textbox", name="Search")
            search_box.click(timeout=3000)
            search_box.fill("")
            page.wait_for_timeout(300)
            search_box.fill(search_term)
            page.keyboard.press("Enter")
            return True
        except Exception as e:
            log.warning(f"[{SERVICE_KEY}]   {label} Playwright search fill failed ({e}), trying JS")
 
        # JS fallback
        filled = page.evaluate("""(term) => {
            const dialog = [...document.querySelectorAll('.el-dialog')]
                .find(d => d.offsetParent !== null);
            if (!dialog) return false;
 
            const inputs = dialog.querySelectorAll('input');
            for (const inp of inputs) {
                if (inp.type === 'file' || !inp.offsetParent) continue;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, '');
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                setter.call(inp, term);
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                inp.dispatchEvent(new Event('change', {bubbles: true}));
                inp.dispatchEvent(new KeyboardEvent('keyup',
                    {key: 'Enter', bubbles: true}));
                return true;
            }
            return false;
        }""", search_term)
 
        return bool(filled)
    # ══════════════════════════════════════════════════════════════════
    #  _close_dialog_properly  (NEW — replaces _confirm_dialog + CSS hack)
    # ══════════════════════════════════════════════════════════════════
 
    def _close_dialog_properly(self, page: Page, label: str):
        """
        Close the Select Address dialog through proper UI interactions.
        Preserves Vue/Element UI internal state so subsequent dialogs work.
 
        Strategy order:
          1. Click 'Close' button (from recorded Playwright session)
          2. Click dialog header X button
          3. Escape key
          4. Vue component API (vm.visible = false / vm.handleClose())
          5. LAST RESORT: remove overlay elements from DOM
        After each, verify dialog is actually closed.
        """
        # ── Strategy 1: 'Close' button by role ───────────────────────
        try:
            close_btn = page.get_by_role("button", name="Close")
            if close_btn.is_visible(timeout=1500):
                close_btn.click(timeout=2000)
                page.wait_for_timeout(800)
                if self._is_all_dialogs_closed(page):
                    log.info(f"[{SERVICE_KEY}]   {label} Dialog closed via 'Close' button")
                    return
        except Exception:
            pass
 
        # ── Strategy 2: Header X button ──────────────────────────────
        try:
            x_btn = page.locator(
                ".el-dialog__wrapper:not([style*='display: none']) .el-dialog__headerbtn"
            ).first
            if x_btn.is_visible(timeout=1000):
                x_btn.click(timeout=2000)
                page.wait_for_timeout(800)
                if self._is_all_dialogs_closed(page):
                    log.info(f"[{SERVICE_KEY}]   {label} Dialog closed via header X")
                    return
        except Exception:
            pass
 
        # ── Strategy 3: Escape key ───────────────────────────────────
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
            if self._is_all_dialogs_closed(page):
                log.info(f"[{SERVICE_KEY}]   {label} Dialog closed via Escape")
                return
        except Exception:
            pass
 
        # ── Strategy 4: Vue component close ──────────────────────────
        try:
            page.evaluate("""() => {
                const wrappers = [...document.querySelectorAll('.el-dialog__wrapper')]
                    .filter(d => getComputedStyle(d).display !== 'none');
                for (const wrapper of wrappers) {
                    const dialog = wrapper.querySelector('.el-dialog');
                    // Walk up __vue__ chain to find close/handleClose
                    const el = dialog || wrapper;
                    if (el.__vue__) {
                        let vm = el.__vue__;
                        for (let depth = 0; depth < 10 && vm; depth++) {
                            if (typeof vm.handleClose === 'function') {
                                vm.handleClose();
                                return;
                            }
                            if (typeof vm.close === 'function') {
                                vm.close();
                                return;
                            }
                            if (vm.visible !== undefined) {
                                vm.visible = false;
                                if (vm.$emit) vm.$emit('update:visible', false);
                                return;
                            }
                            vm = vm.$parent;
                        }
                    }
                }
            }""")
            page.wait_for_timeout(1000)
            if self._is_all_dialogs_closed(page):
                log.info(f"[{SERVICE_KEY}]   {label} Dialog closed via Vue API")
                return
        except Exception:
            pass
 
        # ── Strategy 5: LAST RESORT — remove overlays from DOM ───────
        log.warning(f"[{SERVICE_KEY}]   {label} All close methods failed — force cleanup")
        self._cleanup_overlays(page)
    def _is_all_dialogs_closed(self, page: Page) -> bool:
        """Check that no dialog wrappers are visible."""
        return page.evaluate("""() => {
            const wrappers = document.querySelectorAll('.el-dialog__wrapper');
            for (const w of wrappers) {
                if (getComputedStyle(w).display !== 'none') return false;
            }
            return true;
        }""")
 
    def _cleanup_overlays(self, page: Page):
        """
        Nuclear cleanup: remove stale overlays and body locks.
        Only use as last resort after all proper close methods fail.
        """
        page.evaluate("""() => {
            // Force-hide dialog wrappers
            const dialogs = document.querySelectorAll('.el-dialog__wrapper');
            for (const d of dialogs) {
                if (getComputedStyle(d).display !== 'none') {
                    d.style.display = 'none';
                }
            }
            // Force-hide drawers
            const drawers = document.querySelectorAll('.el-drawer__wrapper');
            for (const d of drawers) {
                if (getComputedStyle(d).display !== 'none') {
                    d.style.display = 'none';
                }
            }
            // REMOVE (not hide) modal masks — prevents them blocking future clicks
            const masks = document.querySelectorAll('.v-modal, .el-overlay');
            for (const m of masks) m.remove();
            // Remove body scroll lock
            document.body.classList.remove('el-popup-parent--hidden');
            document.body.style.removeProperty('overflow');
            document.body.style.removeProperty('padding-right');
        }""")
        page.wait_for_timeout(500)

    def _build_search_term(self, terminal_name: str) -> str:
        """
        Build a clean search term from a terminal address.
        """
        first_line = terminal_name.split("\n")[0].strip().rstrip(",")

        # Remove common noise words that appear in addresses but not in Jordex names
        noise = {"rotterdam", "netherlands", "nl", "the", "bv", "b.v.", "b.v"}
        words = first_line.split()

        # Deduplicate consecutive words (case-insensitive)
        deduped = []
        for w in words:
            if not deduped or w.lower() != deduped[-1].lower():
                deduped.append(w)

        # Remove noise words
        cleaned = [w for w in deduped if w.lower().strip(".,") not in noise]

        # If nothing left after cleaning, use original deduped
        if not cleaned:
            cleaned = deduped

        # Take first 3 words max
        if len(cleaned) > 3:
            cleaned = cleaned[:3]

        return " ".join(cleaned)

    def _click_address_book_button(self, page: Page, section_nth: int, label: str) -> bool:
        """Click the address book button in the destination section.
        Uses the exact selector recorded from Playwright session:
        #pane-destination > div > .el-card > .el-card__body > .el-form > div >
        .routing-tab-panel__body > div:nth-child(N) > div:nth-child(2) > div:nth-child(3) >
        .el-form-item > .el-form-item__content > div > .address-select__body--select >
        .address-select__toolbar > .address-select-toolbar > button
        """
        # Strategy 1: Exact recorded selector (most reliable)
        exact_selector = (
            f"#pane-destination > div > .el-card > .el-card__body > .el-form > div > "
            f".routing-tab-panel__body > div:nth-child({section_nth}) > div:nth-child(2) > "
            f"div:nth-child(3) > .el-form-item > .el-form-item__content > div > "
            f".address-select__body--select > .address-select__toolbar > .address-select-toolbar > button"
        )
        try:
            btn = page.locator(exact_selector).first
            if btn.is_visible(timeout=3000):
                btn.click(timeout=5000)
                log.info(f"[{SERVICE_KEY}]   {label} Address book clicked via exact selector")
                return True
        except Exception:
            pass

        # Strategy 2: Shorter scoped selectors
        for selector in [
            f"#pane-destination .routing-tab-panel__body > div:nth-child({section_nth}) .address-select-toolbar > button",
            f"#pane-destination .routing-tab-panel__body > div:nth-child({section_nth}) .address-select__toolbar button",
            f"#pane-destination .routing-tab-panel__body > div:nth-child({section_nth}) .address-select__body--select button",
        ]:
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=2000):
                    btn.click(timeout=3000)
                    log.info(f"[{SERVICE_KEY}]   {label} Address book clicked via: {selector[:70]}")
                    return True
            except Exception:
                continue

        # Strategy 3: JS fallback
        clicked = page.evaluate(f"""() => {{
            const pane = document.querySelector('#pane-destination');
            if (!pane) return false;
            const body = pane.querySelector('.routing-tab-panel__body');
            if (!body) return false;
            const section = body.children[{section_nth - 1}];
            if (!section) return false;
            const toolbar = section.querySelector(
                '.address-select-toolbar, .address-select__toolbar'
            );
            if (toolbar) {{
                const btn = toolbar.querySelector('button');
                if (btn) {{ btn.click(); return true; }}
            }}
            // Try any button in address-select area
            const areaBtn = section.querySelector('.address-select__body--select button');
            if (areaBtn) {{ areaBtn.click(); return true; }}
            return false;
        }}""")

        if clicked:
            log.info(f"[{SERVICE_KEY}]   {label} Address book clicked via JS fallback")
        return bool(clicked)

    def _click_dialog_row(self, page: Page, search_term: str, label: str) -> bool:
        """
        Click the best matching row in the Select Address dialog.
        """
        result = page.evaluate("""(target) => {
            const targetLower = target.toLowerCase().trim();
            const targetWords = targetLower.split(/\s+/);

            const dialogs = [...document.querySelectorAll('.el-dialog__wrapper')]
                .filter(d => d.style.display !== 'none');
            
            for (const dialog of dialogs) {
                const rows = [...dialog.querySelectorAll('table tbody tr, .el-table__body-wrapper tr')];
                if (!rows.length) continue;

                let bestRow = null;
                let bestScore = 0;

                for (let i = 0; i < rows.length; i++) {
                    const cells = rows[i].querySelectorAll('td, [role="cell"]');
                    if (!cells.length) continue;
                    
                    const name = (cells[0]?.innerText || '').trim().toLowerCase();
                    if (!name) continue;

                    let score = 0;
                    for (const word of targetWords) {
                        if (name.includes(word)) score++;
                    }
                    if (name.includes(targetLower)) score += 5;
                    if (targetLower.includes(name)) score += 3;

                    if (score > bestScore) {
                        bestScore = score;
                        bestRow = rows[i];
                    }
                }

                if (bestRow && bestScore > 0) {
                    bestRow.click();
                    const radio = bestRow.querySelector(
                        '.el-radio__input, .el-radio, input[type="radio"], ' +
                        '.el-radio__original, label.el-radio'
                    );
                    if (radio) radio.click();

                    const firstCell = bestRow.querySelector('td, [role="cell"]');
                    if (firstCell) firstCell.click();

                    const name = (bestRow.querySelector('td')?.innerText || '').trim();
                    return { clicked: true, score: bestScore, name: name.substring(0, 50) };
                }

                if (rows.length > 0) {
                    rows[0].click();
                    const radio = rows[0].querySelector('.el-radio__input, .el-radio, input[type="radio"]');
                    if (radio) radio.click();
                    return { clicked: true, score: 0, name: 'first-row-fallback' };
                }
            }
            return { clicked: false, score: 0 };
        }""", search_term) or {}

        log.info(f"[{SERVICE_KEY}]   {label} Row click result: {result}")
        return result.get("clicked", False)

    def _confirm_dialog(self, page: Page, label: str) -> bool:
        """
        Close the Select Address dialog after row selection.
        Recorded Playwright session shows the dialog uses a 'Close' button
        (not 'Select'/'Save'/'Confirm') to dismiss after selecting a row.
        """
        # Strategy 1: Exact recorded — get_by_role("button", name="Close")
        try:
            close_btn = page.get_by_role("button", name="Close")
            if close_btn.is_visible(timeout=2000):
                close_btn.click(timeout=3000)
                log.info(f"[{SERVICE_KEY}]   {label} Dialog closed via 'Close' button")
                return True
        except Exception:
            pass

        # Strategy 2: JS — look for any Close/primary button in visible dialog
        confirmed = page.evaluate("""() => {
            const dialogs = [...document.querySelectorAll('.el-dialog__wrapper')]
                .filter(d => getComputedStyle(d).display !== 'none');

            for (const dialog of dialogs) {
                const allBtns = [...dialog.querySelectorAll('button')]
                    .filter(b => b.offsetParent !== null);

                // First try: button named Close
                const closeBtn = allBtns.find(b =>
                    (b.innerText || '').trim().toLowerCase() === 'close'
                );
                if (closeBtn) { closeBtn.click(); return 'close-btn'; }

                // Second: primary button not labeled Search/New
                const primary = allBtns.find(b =>
                    b.classList.contains('el-button--primary') &&
                    !(b.innerText || '').toLowerCase().match(/search|new/)
                );
                if (primary) { primary.click(); return 'primary-btn'; }

                // Third: confirm keywords
                const keywords = ['select', 'save', 'confirm', 'ok', 'submit', 'apply'];
                const kw = allBtns.find(b =>
                    keywords.includes((b.innerText || '').trim().toLowerCase())
                );
                if (kw) { kw.click(); return 'keyword-btn'; }
            }
            return null;
        }""")

        if confirmed:
            log.info(f"[{SERVICE_KEY}]   {label} Dialog closed via JS: {confirmed}")
            return True

        # Strategy 3: Enter key fallback
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            log.info(f"[{SERVICE_KEY}]   {label} Dialog dismissed via Escape")
            return True
        except Exception:
            pass

        return False

    def _dismiss_dialog(self, page: Page):
        """Force-close any open dialog to prevent blocking subsequent actions."""
        try:
            # Try clicking the close button via Playwright
            close_btns = page.locator(".el-dialog__wrapper:not([style*='display: none']) .el-dialog__headerbtn, .el-dialog__wrapper:not([style*='display: none']) button[aria-label='Close']")
            if close_btns.count() > 0:
                for i in range(close_btns.count()):
                    if close_btns.nth(i).is_visible():
                        close_btns.nth(i).click()
            page.wait_for_timeout(500)
        except Exception:
            pass

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass

        # Fallback force hide
        page.evaluate("""() => {
            const dialogs = [...document.querySelectorAll('.el-dialog__wrapper')]
                .filter(d => d.style.display !== 'none');
            for (const dialog of dialogs) {
                dialog.style.display = 'none';
            }
            const masks = document.querySelectorAll('.v-modal, .el-overlay');
            for (const m of masks) m.style.display = 'none';
        }""")
        page.wait_for_timeout(300)

    def _fill_section_reference(self, page: Page, section_nth: int,
                                 ref_value: str, label: str):
        """
        Fill the Reference field in a Destination section.
        section_nth: 2 for Pick-up (3.1), 4 for Return (3.3).
        """
        log.info(f"[{SERVICE_KEY}]   {label} Reference: '{ref_value}'")
 
        # ── Scroll section into view ─────────────────────────────────
        page.evaluate(f"""() => {{
            const pane = document.querySelector('#pane-destination');
            if (!pane) return;
            const body = pane.querySelector('.routing-tab-panel__body');
            if (!body) return;
            const section = body.children[{section_nth - 1}];
            if (section) section.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
        }}""")
        page.wait_for_timeout(500)
 
        filled = False
 
        # ── Strategy 1: Scoped JS fill within the correct section ────
        try:
            filled = page.evaluate(f"""(refVal) => {{
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
 
                inp.focus();
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, '');
                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                setter.call(inp, refVal);
                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                inp.dispatchEvent(new Event('blur', {{bubbles: true}}));
                return true;
            }}""", ref_value)
        except Exception:
            pass
 
        if filled:
            log.info(f"[{SERVICE_KEY}]   {label} Reference filled OK")
            page.wait_for_timeout(300)
            return
 
        # ── Strategy 2: Playwright nth (dynamically computed) ────────
        try:
            # Find the global index of the Reference input in our section
            ref_nth = page.evaluate(f"""() => {{
                const pane = document.querySelector('#pane-destination');
                if (!pane) return -1;
                const body = pane.querySelector('.routing-tab-panel__body');
                if (!body) return -1;
                const section = body.children[{section_nth - 1}];
                if (!section) return -1;
 
                const allRefs = [...pane.querySelectorAll('input[placeholder="Reference"]')]
                    .filter(i => i.offsetParent !== null);
                const sectionRefs = [...section.querySelectorAll('input[placeholder="Reference"]')]
                    .filter(i => i.offsetParent !== null);
                if (!sectionRefs.length) return -1;
                return allRefs.indexOf(sectionRefs[0]);
            }}""")
 
            if ref_nth >= 0:
                ref_input = page.get_by_role("textbox", name="Reference").nth(ref_nth)
                if ref_input.is_visible(timeout=2000):
                    ref_input.click()
                    ref_input.fill("")
                    ref_input.fill(ref_value)
                    page.keyboard.press("Tab")  # trigger blur to commit in Vue
                    page.wait_for_timeout(300)
                    log.info(f"[{SERVICE_KEY}]   {label} Reference filled via nth({ref_nth})")
                    return
        except Exception as e:
            log.warning(f"[{SERVICE_KEY}]   {label} Reference strategy 2 failed: {e}")
 
        # ── Strategy 3: Hardcoded nth fallback ───────────────────────
        try:
            fallback_nth = 0 if section_nth == 2 else 2
            ref_input = page.get_by_role("textbox", name="Reference").nth(fallback_nth)
            if ref_input.is_visible(timeout=2000):
                ref_input.click()
                ref_input.fill("")
                ref_input.fill(ref_value)
                page.keyboard.press("Tab")
                page.wait_for_timeout(300)
                log.info(f"[{SERVICE_KEY}]   {label} Reference filled via hardcoded nth({fallback_nth})")
                return
        except Exception:
            pass
 
        log.warning(f"[{SERVICE_KEY}]   {label} Reference could not be filled")

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
        """
        Click Save on the routing page.
        Uses JS .click() to bypass the mf-drawer_content / mf-drawer_footer
        overlay that blocks Playwright's native click.
        """
        try:
            # ── Step 1: Clean up any stale overlays first ────────────
            # Dialogs/masks from address book may still be lingering
            self._cleanup_overlays(page)
 
            # ── Step 2: Scroll Save into view ────────────────────────
            save_found = page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button')];
                const saveBtn = btns.find(b => {
                    const text = (b.innerText || '').trim();
                    return text === 'Save' && b.offsetParent !== null && !b.disabled;
                });
                if (saveBtn) {
                    saveBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    return true;
                }
                return false;
            }""")
 
            if not save_found:
                log.warning(f"[{SERVICE_KEY}] Save button not found in DOM")
                return
 
            page.wait_for_timeout(800)
 
            # ── Step 3: Click via JS (bypasses any overlay) ──────────
            clicked = page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button')];
                const saveBtn = btns.find(b => {
                    const text = (b.innerText || '').trim();
                    return text === 'Save' && b.offsetParent !== null && !b.disabled;
                });
                if (saveBtn) {
                    saveBtn.click();
                    return true;
                }
                return false;
            }""")
 
            if not clicked:
                log.warning(f"[{SERVICE_KEY}] Save JS click failed")
                return
 
            log.info(f"[{SERVICE_KEY}] Save clicked via JS")
 
            # ── Step 4: Wait for save to complete ────────────────────
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
 
            # ── Step 5: Handle confirmation OK dialog ────────────────
            try:
                ok_btn = page.locator("button:has-text('OK'):visible").first
                if ok_btn.is_visible(timeout=2000):
                    ok_btn.click(timeout=3000)
                    page.wait_for_timeout(1000)
            except Exception:
                pass
 
            # ── Step 6: Check for error toast ────────────────────────
            error_toast = page.evaluate("""() => {
                const toasts = document.querySelectorAll(
                    '.el-message--error, .el-notification__content');
                for (const t of toasts) {
                    if (t.offsetParent && t.innerText.trim())
                        return t.innerText.trim();
                }
                return null;
            }""")
 
            if error_toast:
                log.warning(f"[{SERVICE_KEY}] Save may have error: {error_toast[:80]}")
            else:
                log.info(f"[{SERVICE_KEY}] Routing saved")
 
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