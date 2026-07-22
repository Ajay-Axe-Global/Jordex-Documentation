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
    subject_folder_fallback, normalize_oi_reference,
    mark_as_unread, search_jordex_with_fallback,
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

    def _process_batch(self, page, tracker):
        navigate_to_folder(page, OUTLOOK_LABEL)
        msgs = collect_unread(page, tracker, CAT, limit=ROUND_ROBIN_BATCH)
        if not msgs:
            return []

        base = os.path.join(OUTPUT_DIR, CAT)
        processed_items = []

        for msg in msgs:
            if self._stop_evt.is_set():
                break
            cid = msg["conv_id"]

            if not click_row(page, cid):
                tracker.mark(CAT, cid, "", "", [], "failed")
                continue

            subject = get_subject(page) or cid[:40]
            temp_files = download_attachments_to_temp(page)

            if not temp_files:
                tracker.mark(CAT, cid, subject, subject_folder_fallback(subject), [], "no_attachment")
                continue

            oi = extract_oi_from_subject(subject)
            if oi:
                oi = normalize_oi_reference(oi)
            folder_name = oi if oi else subject_folder_fallback(subject)
            extraction = {
                "oi_number": oi, "subject": subject,
                "source_file": os.path.basename(temp_files[0]),
                "extracted_at": datetime.now().isoformat(),
            }

            final_dir = os.path.join(base, folder_name)
            os.makedirs(final_dir, exist_ok=True)
            saved_files = []
            for tmp in temp_files:
                saved = move_file_to_folder(tmp, final_dir)
                if saved:
                    saved_files.append(saved)

            has_tax = False
            has_ttw = False
            forward_flags = []
            tax_info = {}

            if saved_files:
                saved_files, tax_info = self._classify_and_rename(final_dir, saved_files)
                if tax_info:
                    extraction.update(tax_info)

                # What did we get in this email?
                for f in saved_files:
                    fl = f.lower()
                    if "dms_tax" in fl or "utb" in fl:
                        has_tax = True
                    if "dms_imp_ttw" in fl or "ttw" in fl:
                        has_ttw = True

                # Forward flags from TAX/UTB document
                if has_tax and tax_info:
                    status_val = (tax_info.get("status") or "").strip().upper()
                    verschuldigd = tax_info.get("amount_verschuldigd", "0,00")

                    # SOP 7B: if not "DEFINITIEF" (e.g. "VOORLOPIG") → forward
                    if status_val and status_val != "DEFINITIEF":
                        forward_flags.append("voorlopig")
                        log.info(f"  UTB status='{status_val}' → forward_to_import")

                    # SOP 7A: if Verschuldigd > €0.00 → forward
                    if verschuldigd:
                        try:
                            amt = float(str(verschuldigd).replace(".", "").replace(",", "."))
                            if amt > 0.005:
                                forward_flags.append("verschuldigd_nonzero")
                                log.info(f"  Verschuldigd=€{verschuldigd} → forward_to_import")
                        except (ValueError, AttributeError):
                            pass

            save_result(extraction, final_dir, "customs_result.json")
            cleanup_temp(temp_files)

            sec_ref = folder_name if oi and folder_name != oi else None
            tracker.mark(CAT, cid, subject, folder_name, saved_files, "downloaded", secondary_ref=sec_ref)
            self._processed += 1
            processed_items.append({
                "conv_id":       cid,
                "cat":           CAT,
                "folder_path":   final_dir,
                "folder_name":   folder_name,
                "oi_number":     oi,
                "mbl":           None,
                "has_tax":       has_tax,
                "has_ttw":       has_ttw,
                "forward_flags": forward_flags,
                "secondary_ref": sec_ref,
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

    def _upload_to_jordex(self, jordex_page, outlook_page, tracker, items):
        """
        1. Upload files
        2. Scan Documents tab: both TTW + TAX present?
           - Yes → set task Completed
           - No  → leave task open
        3. If task already Completed → skip
        """
        doc_type, _ = JORDEX_MAPPING[CAT]
        normalize_dashboard_filters(jordex_page)

        # Deduplicate: track folder_names already uploaded this batch
        uploaded_folders: set[str] = set()

        for item in items:
            if self._stop_evt.is_set():
                break
            query = item.get("oi_number") or item.get("folder_name")
            if not query:
                continue

            folder_name = item.get("folder_name") or query
            if folder_name in uploaded_folders:
                log.info(
                    f"[{SERVICE_KEY}] Skipping duplicate folder '{folder_name}' "
                    f"(conv_id={item['conv_id'][:20]}…) — already uploaded this batch"
                )
                tracker.update_status(CAT, item["conv_id"], "uploaded")
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
            uploaded = False
            try:
                while row_index < 10:
                    success, rows_found = search_and_open(jordex_page, used_ref, row_index=row_index)
                    if not success:
                        break

                    # ── Upload ───────────────────────────────────────────
                    upload_attachments(
                        jordex_page, item["folder_path"], doc_type, None,
                        file_map=CUSTOMS_DOCS_FILE_MAP,
                    )

                    # ── Task status logic ────────────────────────────────
                    task_status = self._read_task_status(jordex_page)

                    if task_status == "completed":
                        log.info(f"[{SERVICE_KEY}] Task already Completed → skip")
                    else:
                        has_both = self._check_both_customs_docs_exist(jordex_page)
                        if has_both:
                            log.info(f"[{SERVICE_KEY}] Both TTW + TAX found → set Completed")
                            self._set_task_completed(jordex_page)
                        else:
                            log.info(f"[{SERVICE_KEY}] Missing a doc → task stays open")

                    go_back(jordex_page)
                    uploaded = True
                    self._uploaded += 1
                    row_index += 1
                    if rows_found <= row_index:
                        break
            except Exception as e:
                log.error(f"[{SERVICE_KEY}] Error during upload loop for {query}: {e}", exc_info=True)
            finally:
                if uploaded:
                    status = "uploaded"
                    forward_flags = item.get("forward_flags", [])
                    if forward_flags:
                        status = "uploaded_needs_forward"
                        item["forward_reason"] = ", ".join(forward_flags)
                        log.info(f"[{SERVICE_KEY}] OI={query} needs forward: {forward_flags}")
                    tracker.update_status(CAT, item["conv_id"], status)
                    uploaded_folders.add(folder_name)
                else:
                    log.warning(f"[{SERVICE_KEY}] Could not open/upload shipment for {query}")

    def _check_both_customs_docs_exist(self, page) -> bool:
        """
        HOW IT WORKS:
        
        After upload_attachments(), we're on the Documents tab.
        The table HTML looks like this (from your Jordex DOM):

            <table class="mf-table">
              <tbody>
                <tr>
                  <td><span>Customs Clearance</span></td>     ← cell[0] = Type
                  <td><span>dms_tax.pdf</span></td>           ← cell[1] = Name
                  <td><span>17 Jul 2026</span></td>           ← cell[2] = Date
                  <td><span>Axebpo 1</span></td>              ← cell[3] = Uploaded by
                  ...
                </tr>
                <tr>
                  <td><span>Customs Clearance</span></td>
                  <td><span>dms_imp_ttw.pdf</span></td>
                  ...
                </tr>
              </tbody>
            </table>

        We loop through ALL rows. For each row where:
          - Type = "Customs Clearance"  AND
          - Uploaded by = "Axebpo 1" (our bot — ignore docs uploaded by others)
        Then check:
          - If Name contains "dms_tax" or "utb" → found TAX ✓
          - If Name contains "ttw" → found TTW ✓
        
        Return True only when BOTH are found (both uploaded by Axebpo).

        WHY: If Kenny O uploaded a TAX manually and we only uploaded TTW,
        we should NOT mark the task complete — we need to upload our own TAX too.
        Only when BOTH docs are from Axebpo do we own the full set.
        """
        try:
            # Click Documents tab to make sure we see the table
            try:
                tab = page.locator(".el-tabs__item", has_text="Documents").first
                if tab.is_visible(timeout=2000):
                    tab.click()
                    page.wait_for_timeout(2000)
            except Exception:
                pass

            result = page.evaluate("""() => {
                // Find the documents table — match your actual DOM structure
                const tables = document.querySelectorAll('.documents-table table, .mf-table');
                let hasTax = false;
                let hasTtw = false;
                let details = [];

                for (const table of tables) {
                    const rows = table.querySelectorAll('tbody tr');
                    for (const row of rows) {
                        const cells = row.querySelectorAll('td');
                        if (cells.length < 4) continue;

                        // Read the text from the <span> inside each cell
                        const typeText = (cells[0]?.querySelector('span')?.innerText 
                                       || cells[0]?.innerText || '').trim();
                        const nameText = (cells[1]?.querySelector('span')?.innerText 
                                       || cells[1]?.innerText || '').trim();
                        const uploadedBy = (cells[3]?.querySelector('span')?.innerText 
                                         || cells[3]?.innerText || '').trim();

                        // Only count rows that are:
                        //   1. Type = "Customs Clearance"
                        //   2. Uploaded by "Axebpo" (our bot account)
                        const isCustomsClearance = typeText.toLowerCase().includes('customs clearance');
                        const isOurUpload = uploadedBy.toLowerCase().includes('axebpo');

                        if (isCustomsClearance && isOurUpload) {
                            const nameLower = nameText.toLowerCase();
                            
                            if (nameLower.includes('dms_tax') || nameLower.includes('utb')) {
                                hasTax = true;
                                details.push('TAX: ' + nameText + ' (by ' + uploadedBy + ')');
                            }
                            if (nameLower.includes('ttw')) {
                                hasTtw = true;
                                details.push('TTW: ' + nameText + ' (by ' + uploadedBy + ')');
                            }
                        }
                    }
                }

                return { 
                    hasTax: hasTax, 
                    hasTtw: hasTtw, 
                    hasBoth: hasTax && hasTtw,
                    found: details
                };
            }""") or {}

            log.info(f"[{SERVICE_KEY}] Docs check: tax={result.get('hasTax')} "
                     f"ttw={result.get('hasTtw')} both={result.get('hasBoth')} "
                     f"found={result.get('found', [])}")

            return result.get("hasBoth", False)

        except Exception as e:
            log.warning(f"[{SERVICE_KEY}] Docs check failed: {e}")
            return False

    def _read_task_status(self, page) -> str:
        """
        HOW IT WORKS:
        
        The Tasks sidebar is always visible on the right side of the shipment page.
        Each task is a row like:

            <div class="task task-row">
              <div class="mf-tasks-title">Customs document(s) received</div>
              ...status indicator...
            </div>

        The task status is shown via:
        - A color indicator (green = completed, red = to do)
        - Or text inside the task row

        We DON'T need to open the task to read the status.
        We just need to find the task row and check its visual state.

        But since the visual state is unreliable (CSS classes vary),
        we open the task briefly, read the status input, then close it.

        Returns: "completed", "to do", "pending", or "" if unknown.
        """
        try:
            # Wait briefly for tasks to render in the sidebar
            try:
                page.wait_for_selector('.task.task-row, .task-row, .mf-tasks-title', state='attached', timeout=3000)
            except Exception:
                pass

            # Strategy 1: Try to read status without opening the task
            # The task row may have a status class or data attribute
            status = page.evaluate("""() => {
                // Find the task with matching title
                const tasks = [...document.querySelectorAll('.task.task-row, .task-row')];
                for (const task of tasks) {
                    const title = task.querySelector('.mf-tasks-title');
                    if (!title) continue;
                    if (!title.innerText.trim().includes('Customs document')) continue;

                    // Check for status text in the row
                    // Some layouts show status as a tag/badge
                    const statusEls = task.querySelectorAll(
                        '.el-tag, [class*="status"], .task-status-badge'
                    );
                    for (const el of statusEls) {
                        const t = el.innerText.trim().toLowerCase();
                        if (['completed', 'to do', 'pending', 'not applicable'].includes(t)) {
                            return t;
                        }
                    }

                    // Check for status via class names
                    if (task.classList.contains('completed') || 
                        task.querySelector('.completed')) {
                        return 'completed';
                    }

                    // Check any input with Task status placeholder
                    const input = task.querySelector('input[placeholder="Task status"]');
                    if (input && input.value) {
                        return input.value.trim().toLowerCase();
                    }

                    return '__found_but_unknown__';
                }
                return '__not_found__';
            }""") or ""

            log.info(f"[{SERVICE_KEY}] Task status (quick read): '{status}'")

            if status == "completed":
                return "completed"
            if status in ("to do", "pending", "not applicable"):
                return status

            # Strategy 2: If quick read didn't work, open the task to read status
            if status in ("__found_but_unknown__", ""):
                return self._read_task_status_by_opening(page)

            return ""

        except Exception as e:
            log.warning(f"[{SERVICE_KEY}] Task status read failed: {e}")
            return ""

    def _read_task_status_by_opening(self, page) -> str:
        """
        Open the task panel briefly to read the status, then close it.
        
        When you click the task row, a panel opens with:
          - Task title
          - "Task status" dropdown showing current value (e.g. "To do")
          - Internal comments textarea
          - Close button
        
        We read the dropdown value and close.
        """
        try:
            # Click the task to open it
            opened = page.evaluate("""() => {
                const tasks = [...document.querySelectorAll('.task.task-row, .task-row')];
                for (const task of tasks) {
                    const title = task.querySelector('.mf-tasks-title');
                    if (title && title.innerText.trim().includes('Customs document')) {
                        // Click the arrow to open task detail
                        const arrow = task.querySelector('.mf-arrow-right, [class*="arrow"]');
                        if (arrow) { arrow.click(); return true; }
                        task.click();
                        return true;
                    }
                }
                return false;
            }""")

            if not opened:
                return ""

            page.wait_for_timeout(1500)

            # Read the status from the dropdown/input
            status = ""
            try:
                status_input = page.get_by_placeholder("Task status")
                if status_input.is_visible(timeout=2000):
                    status = (status_input.input_value() or "").strip().lower()
                    log.info(f"[{SERVICE_KEY}] Task status (from panel): '{status}'")
            except Exception:
                # Fallback: read any visible status text in the panel
                status = page.evaluate("""() => {
                    const inputs = document.querySelectorAll('input[placeholder="Task status"]');
                    for (const inp of inputs) {
                        if (inp.offsetParent) return inp.value.trim().toLowerCase();
                    }
                    return '';
                }""") or ""

            # Close the panel
            try:
                close_btn = page.get_by_role("button", name="Close")
                if close_btn.is_visible(timeout=1000):
                    close_btn.click()
                else:
                    page.keyboard.press("Escape")
            except Exception:
                page.keyboard.press("Escape")
            page.wait_for_timeout(500)

            return status

        except Exception as e:
            log.warning(f"[{SERVICE_KEY}] Task panel read failed: {e}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return ""

    def _set_task_completed(self, page):
        """
        HOW IT WORKS (matches your Playwright recording):

        Step 1: Click the "Customs document(s) received" task row to open it
                Uses: [...document.querySelectorAll(".task.task-row")]
                        .find(el => el.querySelector(".mf-tasks-title")
                        ?.innerText.trim() === "Customs document(s) received")

        Step 2: Click the "Task status" dropdown
                Uses: page.get_by_placeholder("Task status").click()

        Step 3: Select "Completed" from the dropdown
                Uses: page.get_by_text("Completed", exact=True).click()

        Step 4: Close the panel
                Uses: page.get_by_role("button", name="Close").click()
        """
        try:
            # Step 1: Open the task
            opened = page.evaluate("""() => {
                const tasks = [...document.querySelectorAll('.task.task-row')];
                const target = tasks.find(el => {
                    const title = el.querySelector('.mf-tasks-title');
                    return title && title.innerText.trim().includes('Customs document');
                });
                if (target) {
                    const arrow = target.querySelector('.mf-arrow-right svg, .mf-arrow-right');
                    if (arrow) { arrow.click(); return 'arrow'; }
                    target.click();
                    return 'row';
                }
                return null;
            }""")

            if not opened:
                log.warning(f"[{SERVICE_KEY}] Task 'Customs document(s) received' not found in sidebar")
                return

            log.info(f"[{SERVICE_KEY}] Task opened via: {opened}")
            page.wait_for_timeout(2000)

            # Step 2: Click status dropdown
            try:
                page.get_by_placeholder("Task status").click()
                page.wait_for_timeout(1000)
            except Exception as e:
                log.warning(f"[{SERVICE_KEY}] Status dropdown failed: {e}")
                page.keyboard.press("Escape")
                return

            # Step 3: Select "Completed"
            try:
                page.get_by_text("Completed", exact=True).click()
                page.wait_for_timeout(1500)
                log.info(f"[{SERVICE_KEY}] Task set to Completed")
            except Exception as e:
                log.warning(f"[{SERVICE_KEY}] Could not select Completed: {e}")
                # Fallback: try via dropdown items
                clicked = page.evaluate("""() => {
                    const items = [...document.querySelectorAll('.el-select-dropdown__item')]
                        .filter(el => el.offsetParent !== null);
                    for (const item of items) {
                        if (item.innerText.trim() === 'Completed') {
                            item.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                if not clicked:
                    log.warning(f"[{SERVICE_KEY}] Completed option not found")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(500)
                    page.keyboard.press("Escape")
                    return
                page.wait_for_timeout(1500)

            # Step 4: Click SAVE to commit the status change
            try:
                page.get_by_role("button", name="Save").click()
                page.wait_for_timeout(3500)
                log.info(f"[{SERVICE_KEY}] Task saved (Save button clicked)")
            except Exception as e:
                log.warning(f"[{SERVICE_KEY}] Save button click failed: {e} — trying Escape")
                page.keyboard.press("Escape")
                page.wait_for_timeout(1500)

            log.info(f"[{SERVICE_KEY}] Task completion done")

        except Exception as e:
            log.error(f"[{SERVICE_KEY}] Task update failed: {e}")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass 
