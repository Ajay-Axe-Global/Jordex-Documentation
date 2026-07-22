import os
import glob
import json
import hashlib
import tempfile
import logging
from datetime import datetime
from .browser import apply_zoom

log = logging.getLogger("jordex.documents")

# Customs docs rename rules — used as file_map in upload_attachments()
CUSTOMS_DOCS_FILE_MAP = {
    "dms_tax.pdf":     ("Customs Clearance", "dms_tax"),
    "dms_imp_ttw.pdf": ("Customs Clearance", "dms_imp_ttw"),
}

# Customer docs upload mapping — doc_type → (Jordex document_type, display_name)
# Mirrors customer_docs.CUSTOMER_DOC_UPLOAD_MAP.
# CUSTOMER_DOC_TYPE_MAP = {
#     "HOUSE BILL OF LADING":  ("Carrier documents", "HBL"),
#     "MASTER BILL OF LADING": ("Carrier documents", "MBL"),
#     "COMMERCIAL INVOICE":    ("Carrier documents", "Commercial Invoice"),
#     "AGENT INVOICE":         ("Carrier documents", "Agent Invoice"),
#     "DEBIT NOTE":            ("Carrier documents", "Debit Note"),
#     "PACKING LIST":          ("Carrier documents", "Packing List"),
#     "ARRIVAL NOTICE":        ("Carrier documents", "AN"),
#     "BOOKING CONFIRMATION":  ("Carrier documents", "Booking Confirmation"),
#     "ADDITIONAL FILES":      ("Carrier documents", "Additional Files"),
# }

CUSTOMER_DOC_TYPE_MAP = {
    "HOUSE BILL OF LADING":  ("House BL",              "HBL",                  ""),
    "MASTER BILL OF LADING": ("Master BL",             "MBL",                  ""),
    "COMMERCIAL INVOICE":    ("Commercial Invoice",    "Commercial Invoice",   ""),
    "AGENT INVOICE":         ("Agent Invoice",         "Agent Invoice",        ""),
    "BOOKING CONFIRMATION":  ("Booking Confirmation",  "Booking Confirmation", ""),
    "PACKING LIST":          ("Packing List",          "Packing List",         ""),
    "DEBIT NOTE":            ("Additional Files",      "Additional Files",     "Debit Note"),
    "ARRIVAL NOTICE":        ("Additional Files",      "AN",     ""),
    "ADDITIONAL FILES":      ("Additional Files",      "Additional Files",     ""),  # comment from doc_title
}

# ── Hash-based duplicate detection helpers ────────────────────────────

def get_file_hash(file_path):
    """SHA-256 hash of a local file."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _get_today_dates():
    """Return today's date in both padded and non-padded formats for matching."""
    now = datetime.now()
    today_padded = now.strftime("%d %b %Y")        # "03 Jul 2026"
    # Strip leading zero for non-padded format (works on Windows & Linux)
    today_no_pad = today_padded.lstrip("0") if today_padded.startswith("0") else today_padded  # "3 Jul 2026"
    return today_padded, today_no_pad


def get_today_existing_hashes(page, display_name):
    """
    Find rows in the Documents tab matching display_name uploaded TODAY,
    download each one via cell click (popup or download), and return
    a set of their SHA-256 hashes.
    """
    import threading

    today_padded, today_no_pad = _get_today_dates()
    hashes = set()

    # Find row indices where NAME matches AND UPLOADED date is today
    row_indices = page.evaluate("""([targetName, today1, today2]) => {
        const indices = [];
        const rows = document.querySelectorAll('table tbody tr, .el-table__body tr');
        rows.forEach((row, i) => {
            const cells = row.querySelectorAll('td, [role="cell"]');
            if (cells.length < 3) return;
            const rawName = (cells[1]?.innerText || '').trim().toLowerCase();
            const name = rawName.replace(/\\.pdf$/i, '');
            const date = (cells[2]?.innerText || '').trim();
            if (name === targetName.toLowerCase()
                && (date === today1 || date === today2)) {
                indices.push(i);
            }
        });
        return indices;
    }""", [display_name, today_padded, today_no_pad])

    if not row_indices:
        return hashes

    log.info(f"  Found {len(row_indices)} '{display_name}' uploaded today — downloading for hash comparison...")

    for idx in row_indices:
        try:
            # Use the same popup/download pattern as Shipment_Process.resolve_pdf_url
            event = threading.Event()
            result = {}

            def on_popup(popup):
                result['popup'] = popup
                event.set()

            def on_download(download):
                result['download'] = download
                event.set()

            page.once("popup", on_popup)
            page.once("download", on_download)

            # Click the NAME cell (column 1) to trigger popup or download
            page.evaluate("""(rowIdx) => {
                const rows = document.querySelectorAll('table tbody tr, .el-table__body tr');
                const row = rows[rowIdx];
                if (!row) return;
                const cells = row.querySelectorAll('td, [role="cell"]');
                if (cells.length > 1) {
                    const nameCell = cells[1];
                    const link = nameCell.querySelector('a') || nameCell.querySelector('span');
                    if (link) link.click();
                    else nameCell.click();
                }
            }""", idx)

            # Wait up to 10 seconds for either event
            for _ in range(100):
                if event.is_set():
                    break
                page.wait_for_timeout(100)

            # Cleanup listeners
            try:
                page.remove_listener("popup", on_popup)
                page.remove_listener("download", on_download)
            except Exception:
                pass

            tmp_path = os.path.join(tempfile.gettempdir(), f"jordex_hash_check_{idx}.pdf")

            if "download" in result:
                # Direct download — save and hash
                result["download"].save_as(tmp_path)
                file_hash = get_file_hash(tmp_path)
                hashes.add(file_hash)
                os.remove(tmp_path)
                log.info(f"    Existing file [{idx}] hash (download): {file_hash[:16]}...")

            elif "popup" in result:
                # PDF opened in viewer — extract URL and download via API
                viewer = result["popup"]
                viewer.wait_for_load_state("domcontentloaded")

                pdf_url = None
                try:
                    embed = viewer.locator("embed").first
                    if embed.is_visible(timeout=2000):
                        pdf_url = embed.get_attribute("original-url") or embed.get_attribute("src")
                except Exception:
                    pass

                if not pdf_url:
                    try:
                        iframe = viewer.locator("iframe").first
                        if iframe.is_visible(timeout=2000):
                            pdf_url = iframe.get_attribute("src")
                    except Exception:
                        pass

                if not pdf_url and "shipment-document" in viewer.url:
                    pdf_url = viewer.url

                viewer.close()

                if pdf_url:
                    if pdf_url.startswith("/"):
                        pdf_url = "https://jit-api.jordex.com" + pdf_url
                    response = page.request.get(pdf_url, timeout=30000)
                    if response.ok:
                        body = response.body()
                        with open(tmp_path, "wb") as f:
                            f.write(body)
                        file_hash = get_file_hash(tmp_path)
                        hashes.add(file_hash)
                        os.remove(tmp_path)
                        log.info(f"    Existing file [{idx}] hash (popup): {file_hash[:16]}...")
                    else:
                        log.warning(f"    Could not download PDF from URL for row {idx}")
                else:
                    log.warning(f"    No PDF URL found in popup for row {idx}")
            else:
                log.warning(f"    No download or popup triggered for row {idx}")

        except Exception as e:
            log.warning(f"    Could not download existing file at row {idx}: {e}")
            # Dismiss any stuck dialog/popup
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            except Exception:
                pass

    return hashes

def get_today_existing_filenames(page):
    """
    Scrape all filenames from the Documents tab that were uploaded TODAY.
    
    Used for Customer_Docs where Jordex keeps the original filename
    (e.g. "HBL_FSNBS2604650.pdf") — so we can skip by filename match
    without needing to download and hash.
    
    Returns:
        list of lowercase filenames uploaded today.
    """
    today_padded, today_no_pad = _get_today_dates()

    filenames = page.evaluate("""([today1, today2]) => {
        const results = [];
        const rows = document.querySelectorAll('table tbody tr, .el-table__body tr');
        for (const row of rows) {
            const cells = row.querySelectorAll('td, [role="cell"]');
            if (cells.length < 3) continue;
            const name = (cells[1]?.innerText || '').trim().toLowerCase();
            const date = (cells[2]?.innerText || '').trim();
            if ((date === today1 || date === today2) && name) {
                results.push(name);
            }
        }
        return results;
    }""", [today_padded, today_no_pad]) or []

    return filenames
# ── File map builders ─────────────────────────────────────────────────

def build_invoice_carrier_file_map(folder_path: str) -> dict:
    """
    Build a per-file upload map for Invoice Carrier by reading result.json list.
    """
    file_map = {}
    json_path = os.path.join(folder_path, "result.json")
    if os.path.exists(json_path):
        try:
            with open(json_path) as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = [data]
            for item in data:
                src_file = item.get("source_file")
                inv_no = item.get("invoice_no")
                if src_file:
                    display_name = "Invoice carrier"
                    file_map[src_file] = ("Carrier documents", display_name)
        except Exception as e:
            log.warning(f"Could not build invoice file map: {e}")

    # Fallback for any missing PDFs
    pdf_files = glob.glob(os.path.join(folder_path, "*.[pP][dD][fF]"))
    for pdf_path in pdf_files:
        filename = os.path.basename(pdf_path)
        if filename not in file_map:
            file_map[filename] = ("Carrier documents", "Invoice carrier")

    return file_map


# def build_customer_docs_file_map(folder_path: str) -> dict:
#     """
#     Build a per-file upload map for a Customer Docs folder by reading the
#     classification JSON files saved alongside each PDF.

#     Each PDF should have a companion <stem>_classification.json produced
#     by customer_docs.classify_customer_doc(). If no JSON is found for a
#     PDF, it falls back to ("Carrier documents", "Additional Files").

#     Returns:
#         {
#             "HLCUSZX2605APPZ0_hbl.pdf": ("Carrier documents", "HBL"),
#             "invoice.pdf":              ("Carrier documents", "Commercial Invoice"),
#             ...
#         }
#     """
#     file_map = {}
#     pdf_files = glob.glob(os.path.join(folder_path, "*.[pP][dD][fF]"))

#     for pdf_path in pdf_files:
#         filename = os.path.basename(pdf_path)
#         stem = os.path.splitext(filename)[0]

#         # Look for companion classification JSON
#         json_path = os.path.join(folder_path, f"{stem}_classification.json")
#         doc_type = "ADDITIONAL FILES"

#         if os.path.exists(json_path):
#             try:
#                 with open(json_path) as f:
#                     data = json.load(f)
#                 doc_type = data.get("doc_type", "ADDITIONAL FILES")
#             except Exception as e:
#                 log.warning("build_customer_docs_file_map: could not read %s: %s", json_path, e)

#         upload_tuple = CUSTOMER_DOC_TYPE_MAP.get(doc_type, ("Carrier documents", "Additional Files"))
#         file_map[filename] = upload_tuple
#         log.info("  Customer file map: '%s' → %s", filename, upload_tuple)

#     return file_map

def build_customer_docs_file_map(folder_path: str) -> dict:
    """
    Build a per-file upload map for Customer Docs by reading classification JSONs.
 
    Returns:
        {
            "hbl_doc.pdf":    ("House BL", "HBL", ""),
            "debit.pdf":      ("Additional Files", "Additional Files", "Debit Note"),
            "unknown.pdf":    ("Additional Files", "Additional Files", "Certificate of Origin"),
        }
 
    Each value is a 3-tuple: (jordex_type, display_name, comment)
    """
    import glob, json, os
 
    file_map = {}
    pdf_files = glob.glob(os.path.join(folder_path, "*.[pP][dD][fF]"))
 
    for pdf_path in pdf_files:
        filename = os.path.basename(pdf_path)
        stem = os.path.splitext(filename)[0]
 
        # Read companion classification JSON
        json_path = os.path.join(folder_path, f"{stem}_classification.json")
        doc_type = "ADDITIONAL FILES"
        doc_title = ""
 
        if os.path.exists(json_path):
            try:
                with open(json_path) as f:
                    data = json.load(f)
                doc_type = data.get("doc_type", "ADDITIONAL FILES")
                doc_title = data.get("doc_title", "")  # extracted header/title for comment
            except Exception as e:
                log.warning("build_customer_docs_file_map: could not read %s: %s", json_path, e)
 
        # Look up the 3-tuple
        mapping = CUSTOMER_DOC_TYPE_MAP.get(doc_type, ("Additional Files", "Additional Files", ""))
        jordex_type, _, default_comment = mapping
        display_name = ""  # Leave blank to skip renaming entirely in JS
 
        # For "Additional Files": use doc_title from extractor as comment if available
        if jordex_type == "Additional Files" and not default_comment and doc_title:
            comment = doc_title.title()[:250]  # Jordex comment limit is 250 chars
        elif default_comment:
            comment = default_comment
        else:
            comment = ""
 
        file_map[filename] = (jordex_type, display_name, comment)
        log.info("  Customer file map: '%s' → type='%s' name='%s' comment='%s'",
                 filename, jordex_type, display_name, comment)
 
    return file_map
 

# ── Main upload function ──────────────────────────────────────────────

def upload_attachments(page, folder_path, document_type, display_name=None, file_map=None):
    """
    Go to Documents tab, upload PDFs from folder_path to Jordex.

    Duplicate detection strategy (two layers):
    
    1. FILENAME CHECK (for files that keep original name, e.g. Customer_Docs):
       If "HBL_FSNBS2604650.pdf" already exists in the table with today's date → skip.
       
    2. HASH CHECK (for files that get renamed, e.g. Invoice carrier, AN, DO):
       Downloads existing file from Jordex, computes SHA-256, compares with local.
       Same hash = exact duplicate → skip.
       Different hash = new file → upload.

    Args:
        page:          Playwright page object (Jordex browser tab).
        folder_path:   Local folder containing PDFs to upload.
        document_type: Default Jordex document type for the dropdown.
        display_name:  Default display name shown in Jordex after upload.
        file_map:      Optional dict {filename: (doc_type, display_name)} for per-file
                       type/name overrides.
    """
    log.info(f"Opening Documents tab to upload as '{document_type}' (name='{display_name}')...")
    try:
        tab = page.locator(".el-tabs__item", has_text="Documents").first
        if not tab.is_visible(timeout=3000):
            tab = page.get_by_role("tab", name="Documents")
        tab.click()
        page.wait_for_timeout(3000)
        apply_zoom(page)
    except Exception as e:
        log.warning(f"Documents tab not found: {e}")
        return

    pdf_files = sorted(glob.glob(os.path.join(folder_path, "*.[pP][dD][fF]")))
    if not pdf_files:
        log.info(f"No PDFs found in {folder_path} to upload.")
        return

    # Build a lowercase lookup for file_map so matching is case-insensitive
    file_map_lower = {}
    if file_map:
        file_map_lower = {k.lower(): v for k, v in file_map.items()}

    # ── LAYER 1: Scrape all filenames uploaded today (for filename-based check) ──
    existing_filenames_today = get_today_existing_filenames(page)
    if existing_filenames_today:
        log.info(f"  Files already uploaded today: {existing_filenames_today}")

    # ── LAYER 2: Collect display names that need hash-based check ──
    names_to_check = set()
    for pdf_path in pdf_files:
        filename = os.path.basename(pdf_path)
        if file_map_lower and filename.lower() in file_map_lower:
            entry = file_map_lower[filename.lower()]
            name = entry[1]
        else:
            name = display_name or os.path.splitext(filename)[0]
        if name:
            names_to_check.add(name)

    # Download & hash existing files uploaded TODAY (once per display name)
    existing_hashes = {}  # { "invoice carrier": {hash1, hash2, ...} }
    for name in names_to_check:
        hashes = get_today_existing_hashes(page, name)
        if hashes:
            existing_hashes[name.lower()] = hashes
            log.info(f"  '{name}' has {len(hashes)} file(s) uploaded today in Jordex.")
        else:
            log.info(f"  '{name}' has no files uploaded today — all local files will be uploaded.")

    # ── Upload loop ──
    for pdf_path in pdf_files:
        filename = os.path.basename(pdf_path)

        # Resolve per-file type, name, and comment (file_map takes priority)
        actual_comment = ""
        if file_map_lower and filename.lower() in file_map_lower:
            entry = file_map_lower[filename.lower()]
            if len(entry) == 3:
                actual_type, actual_name, actual_comment = entry
            else:
                actual_type, actual_name = entry
                actual_comment = ""
        else:
            if file_map is not None:
                log.info(f"  Skipping '{filename}' because it is not in the provided file_map.")
                continue
            actual_type = document_type
            actual_name = display_name or os.path.splitext(filename)[0]
            actual_comment = ""

        # ── LAYER 1: Filename-based check ──
        # For files that keep original name (e.g. Customer_Docs)
        # If "HBL_FSNBS2604650.pdf" already in table today → skip
        if filename.lower() in existing_filenames_today:
            log.info(f"  '{filename}' already exists in Documents tab (uploaded today) — skipping.")
            continue

        # ── LAYER 2: Hash-based check ──
        # For files that get renamed (e.g. Invoice carrier, AN, DO)
        # Download existing, compare SHA-256 → same = skip, different = upload
        if actual_name and actual_name.lower() in existing_hashes:
            local_hash = get_file_hash(pdf_path)
            if local_hash in existing_hashes[actual_name.lower()]:
                log.info(f"  '{filename}' is IDENTICAL to existing '{actual_name}' (uploaded today) — skipping.")
                continue
            else:
                log.info(f"  '{filename}' hash differs from existing '{actual_name}' — uploading as new file.")

        log.info(f"  Uploading '{filename}' as type='{actual_type}' name='{actual_name}'...")

        # ── Find + button ────────────────────────────────────────────
        plus_btn = None
        for sel in ["button.upload-button", "button:has(.el-icon-plus)"]:
            try:
                c = page.locator(sel).first
                if c.is_visible(timeout=2000):
                    plus_btn = c
                    break
            except:
                continue

        if not plus_btn:
            log.warning(f"  Upload button not found for '{filename}'.")
            continue

        # ── Set file via file chooser ────────────────────────────────
        try:
            with page.expect_file_chooser(timeout=5000) as fc_info:
                plus_btn.click(timeout=5000)
            fc_info.value.set_files(pdf_path)
            page.wait_for_timeout(2000)
        except Exception as e:
            log.warning(f"  Failed to set file '{filename}': {e}")
            page.keyboard.press("Escape")
            continue

        # Wait for upload dialog
        page.wait_for_timeout(1000)

        # ── Select Type dropdown ─────────────────────────────────────
        type_opened = page.evaluate("""() => {
            const dialog = [...document.querySelectorAll('.el-dialog')].find(d => d.offsetParent !== null);
            if (!dialog) return false;
            const select = dialog.querySelector('.el-select');
            if (select) { select.click(); return true; }
            const inp = dialog.querySelector('input.el-input__inner[readonly]');
            if (inp) { inp.click(); return true; }
            return false;
        }""")

        if type_opened:
            page.wait_for_timeout(1000)
            page.evaluate("""(typeLabel) => {
                const items = [...document.querySelectorAll('.el-select-dropdown__item')]
                    .filter(el => el.offsetParent !== null);
                // exact match first
                for (const el of items) {
                    if (el.textContent.trim().toLowerCase() === typeLabel.toLowerCase()) {
                        el.click(); return;
                    }
                }
                // partial match fallback
                for (const el of items) {
                    if (el.textContent.trim().toLowerCase().includes(typeLabel.toLowerCase())) {
                        el.click(); return;
                    }
                }
            }""", actual_type)
            page.wait_for_timeout(500)
        else:
            log.warning(f"  Could not open Type dropdown for '{filename}'.")

        # ── Set display name ─────────────────────────────────────────
        if actual_name and actual_type != "Customs Clearance":
            page.evaluate("""([name]) => {
                const dialog = [...document.querySelectorAll('.el-dialog')]
                    .find(d => d.offsetParent !== null);
                if (!dialog) return;
                const allInputs = [...dialog.querySelectorAll('input.el-input__inner')];
                const nameInp = allInputs.find(inp => {
                    if (inp.type === 'file') return false;
                    if (!inp.offsetParent) return false;
                    if (inp.closest('.el-select')) return false;
                    return true;
                });
                if (nameInp) {
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(nameInp, '');
                    nameInp.dispatchEvent(new Event('input', {bubbles: true}));
                    setter.call(nameInp, name);
                    nameInp.dispatchEvent(new Event('input', {bubbles: true}));
                    nameInp.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""", [actual_name])
            page.wait_for_timeout(800)

        # ── Set comment field (if provided) ──────────────────────────
        if actual_comment:
            page.evaluate('''([comment]) => {
                const dialog = [...document.querySelectorAll('.el-dialog')]
                    .find(d => d.offsetParent !== null);
                if (!dialog) return;
                const textarea = dialog.querySelector('textarea');
                if (textarea) {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value').set;
                    setter.call(textarea, '');
                    textarea.dispatchEvent(new Event('input', {bubbles: true}));
                    setter.call(textarea, comment);
                    textarea.dispatchEvent(new Event('input', {bubbles: true}));
                    textarea.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }''', [actual_comment])
            page.wait_for_timeout(500)
            log.info(f"  Comment set: '{actual_comment}'")

        # ── Save ─────────────────────────────────────────────────────
        saved = page.evaluate("""() => {
            const dialog = [...document.querySelectorAll('.el-dialog')].find(d => d.offsetParent !== null);
            if (!dialog) return false;
            const btns = dialog.querySelectorAll('button');
            for (const b of btns) {
                if (b.innerText.trim().includes('Save')) { b.click(); return true; }
            }
            return false;
        }""")

        if saved:
            page.wait_for_timeout(2500)
            try:
                ok = page.locator("button:has-text('OK'):visible").first
                if ok.is_visible(timeout=2000):
                    ok.click()
                    page.wait_for_timeout(1000)
            except:
                pass
            log.info(f"  OK Uploaded: '{filename}' as '{actual_name}'")
        else:
            log.warning(f"  FAILED to click Save for '{filename}'.")
            page.keyboard.press("Escape")

    log.info("Finished uploading documents.")