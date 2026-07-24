"""
shared/helpers.py — Outlook Automation Helpers (Shared)
========================================================
All Playwright-based helpers for navigating Outlook, collecting unread
emails, downloading attachments, and file management.

Used by every service's main label file (arrival_notice.py, invoice_carrier.py, etc.)
These functions accept a `page` argument — each service passes its OWN page.
"""
import hashlib, json, os, re, shutil, tempfile, logging
from datetime import datetime
from playwright.sync_api import Page, TimeoutError as PwTimeout
from config import ELEMENT_TIMEOUT, SHORT_WAIT, OUTPUT_DIR

log = logging.getLogger("helpers")


# ══════════════════════════════════════════════════════════════════════
#  Navigation
# ══════════════════════════════════════════════════════════════════════

def navigate_to_folder(page: Page, label: str):
    log.info(f"Opening folder: {label}")
    el = page.locator(f"text='{label}'").first
    el.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
    el.click()
    page.wait_for_timeout(SHORT_WAIT)
    page.wait_for_selector("#MailList", state="attached", timeout=ELEMENT_TIMEOUT)
    page.wait_for_timeout(SHORT_WAIT)


def _scroll(page: Page):
    try:
        page.locator("#MailList div[data-virtuoso-scroller='true']").first.evaluate(
            "el=>el.scrollBy(0,500)", timeout=2000
        )
    except Exception:
        pass
    page.wait_for_timeout(1500)


# ══════════════════════════════════════════════════════════════════════
#  Collect unread emails
# ══════════════════════════════════════════════════════════════════════

def collect_unread(page: Page, tracker, cat: str, limit: int, max_scrolls=50) -> list[dict]:
    try:
        page.locator("#MailList div[data-virtuoso-scroller='true']").first.evaluate(
            "el=>el.scrollTo(0,0)", timeout=2000
        )
        page.wait_for_timeout(1000)
    except Exception:
        pass
    seen, results = set(), []
    for _ in range(max_scrolls):
        rows = page.locator("#MailList div[role='option']")
        for i in range(rows.count()):
            if len(results) >= limit:
                return results
            r = rows.nth(i)
            if r.locator("div.DLvHz").count() == 0:
                continue
            try:
                cid = r.get_attribute("data-convid", timeout=2000)
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                if tracker.is_done(cat, cid):
                    continue
                results.append({"conv_id": cid})
            except Exception:
                continue
        if len(results) >= limit:
            return results
        if rows.count() == 0:
            log.info("    No messages found in folder.")
            break
        _scroll(page)
        new_rows = page.locator("#MailList div[role='option']")
        new_ids = set()
        for i in range(new_rows.count()):
            try:
                new_ids.add(new_rows.nth(i).get_attribute("data-convid", timeout=1000))
            except Exception:
                pass
        if not (new_ids - seen):
            break
    log.info(f"Collected {len(results)} unread in {cat}")
    return results


def click_row(page: Page, conv_id: str) -> bool:
    sel = f"#MailList div[role='option'][data-convid='{conv_id}']"
    try:
        r = page.locator(sel).first
        r.wait_for(state="visible", timeout=3000)
        r.click()
        page.wait_for_timeout(SHORT_WAIT)
        return True
    except Exception:
        pass
    page.locator("#MailList div[data-virtuoso-scroller='true']").first.evaluate("el=>el.scrollTo(0,0)")
    page.wait_for_timeout(1000)
    for _ in range(60):
        try:
            r = page.locator(sel).first
            if r.is_visible(timeout=500):
                r.click()
                page.wait_for_timeout(SHORT_WAIT)
                return True
        except Exception:
            pass
        _scroll(page)
    return False


def get_subject(page: Page) -> str:
    try:
        el = page.locator("#ReadingPaneContainerId span.JdFsz").first
        el.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
        return (el.get_attribute("title", timeout=3000) or el.inner_text(timeout=3000)).strip()
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════
#  Attachment downloading
# ══════════════════════════════════════════════════════════════════════

OUTLOOK_BASE_URL = "https://outlook.cloud.microsoft.com/mail/"
 
 
def is_page_responsive(page, timeout: int = 8000) -> bool:
    """
    Detect frozen browser page with a trivial JS eval.
 
    Uses 8s timeout (not 5s) — Outlook can be legitimately slow
    under load without being truly frozen.
    """
    try:
        result = page.evaluate("() => document.readyState", timeout=timeout)
        return result is not None
    except Exception:
        log.warning("  Page health check FAILED — page is unresponsive")
        return False
 
 
def recover_page(page, service_key: str = "", wait_selector: str = None) -> bool:
    """
    3-stage recovery for frozen Outlook/Jordex pages.
 
    Stage 1: page.reload() — handles soft freezes (JS event loop stalled)
    Stage 2: goto('about:blank') + goto(original URL) — breaks frozen DOM
    Stage 3: If both fail, return False so caller can escalate
 
    Returns True if page is responsive after recovery.
    """
    log.warning("[%s] Attempting page recovery...", service_key)
 
    # ── Capture the current URL before we lose it ────────────────────
    current_url = None
    try:
        current_url = page.url
    except Exception:
        pass
    if not current_url or current_url == "about:blank":
        current_url = OUTLOOK_BASE_URL
 
    # ── Stage 1: Soft reload ─────────────────────────────────────────
    log.info("[%s] Recovery Stage 1: reload()", service_key)
    try:
        page.reload(timeout=15000, wait_until="commit")
        page.wait_for_timeout(3000)
        if is_page_responsive(page, timeout=8000):
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, state="attached", timeout=10000)
                except Exception:
                    pass
            log.info("[%s] Stage 1 recovery SUCCEEDED", service_key)
            return True
    except Exception as e:
        log.warning("[%s] Stage 1 failed: %s", service_key, e)
 
    # ── Stage 2: Force-navigate to blank page, then back ─────────────
    log.info("[%s] Recovery Stage 2: goto('about:blank') → goto(URL)", service_key)
    try:
        # Navigate to blank page — this abandons the frozen DOM entirely
        # Use a short timeout because even goto can hang on a frozen renderer
        try:
            page.goto("about:blank", timeout=10000, wait_until="commit")
        except Exception:
            # Even about:blank timed out — try one more thing
            log.warning("[%s] Stage 2: about:blank timed out, trying keyboard interrupt", service_key)
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
                page.goto("about:blank", timeout=10000, wait_until="commit")
            except Exception:
                pass
 
        page.wait_for_timeout(2000)
 
        # Check if blank page is responsive
        if not is_page_responsive(page, timeout=5000):
            log.error("[%s] Stage 2: even about:blank is unresponsive", service_key)
            return False
 
        # Navigate back to original URL
        log.info("[%s] Stage 2: navigating back to %s", service_key, current_url[:60])
        page.goto(current_url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
 
        if is_page_responsive(page, timeout=8000):
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, state="attached", timeout=15000)
                except Exception:
                    log.warning("[%s] Stage 2: page responsive but selector '%s' not found",
                                service_key, wait_selector)
            log.info("[%s] Stage 2 recovery SUCCEEDED", service_key)
            return True
        else:
            log.error("[%s] Stage 2: page still unresponsive after re-navigation", service_key)
            return False
 
    except Exception as e:
        log.error("[%s] Stage 2 failed: %s", service_key, e)
        return False
 
 
def download_attachments_to_temp(page, max_retries: int = 2) -> list[str]:
    """
    ★ FIX 3 — Download all attachments with retry + menu dismissal.
 
    Changes from original:
      - Retries each attachment up to `max_retries` times on failure
      - Presses Escape between retries to dismiss stale context menus
      - Checks page responsiveness before retrying
      - Returns partial results (files that DID download) instead of
        empty list when some fail
 
    Returns list of temp file paths (may be partial on failures).
    """
    # Broaden container selector to catch 'av-container' (used for images)
    ctr = page.locator(
        "#ReadingPaneContainerId div[role='listbox'][aria-label='file attachments'],"
        "#ReadingPaneContainerId div[role='listbox'][aria-label='bijlagen'],"
        "#ReadingPaneContainerId div.av-container"
    ).first
    try:
        ctr.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
    except Exception:
        return []
 
    atts = ctr.locator("div[role='option']")
    att_count = atts.count()
 
    if att_count == 0:
        return []
 
    temp_dir = tempfile.mkdtemp(prefix="outlook_dl_")
    saved = []
    failed_names = []                                          # ★ FIX 3
 
    for i in range(att_count):
        att = atts.nth(i)
        fname = _att_name(att)
        log.info("  Downloading: %s", fname)
        downloaded = False                                     # ★ FIX 3
 
        for attempt in range(max_retries + 1):                 # ★ FIX 3
            try:
                # ── Dismiss stale menu on retry ──────────────
                if attempt > 0:                                # ★ FIX 3
                    log.info("  Retry %d/%d for: %s", attempt, max_retries, fname)
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(500)
                    except Exception:
                        pass
 
                    # Check page is still alive before retrying
                    if not is_page_responsive(page):           # ★ FIX 2+3
                        recovered = recover_page(page, wait_selector="#ReadingPaneContainerId")
                        if not recovered:
                            log.error("  Page frozen — aborting downloads")
                            failed_names.append(fname)
                            break
 
                att.hover()
                page.wait_for_timeout(400)                     # ★ FIX 5: 500→400
 
                more_btn = att.locator("button[aria-label='More actions']").first
                if more_btn.is_visible():
                    more_btn.click()
                else:
                    # Fallback for images: right-click to open context menu
                    att.click(button="right")
 
                page.wait_for_timeout(800)                     # ★ FIX 5: 1000→800
 
                dl_btn = page.locator(
                    "div[role='menu'] button:has-text('Download'),"
                    "div[role='menu'] div[role='menuitem']:has-text('Download'),"
                    "ul[role='menu'] button:has-text('Download'),"
                    "div[role='menu'] button:has-text('Downloaden'),"
                    "div[role='menu'] div[role='menuitem']:has-text('Downloaden'),"
                    "button[name='Download'],button[name='Downloaden'],"
                    "span:text-is('Download'),span:text-is('Downloaden')"
                ).first
 
                with page.expect_download(timeout=30000) as di:
                    dl_btn.click()
 
                path = os.path.join(temp_dir, fname)
                b, e = os.path.splitext(fname)
                c = 1
                while os.path.exists(path):
                    path = os.path.join(temp_dir, f"{b}_{c}{e}")
                    c += 1
                di.value.save_as(path)
                saved.append(path)
                log.info("  Temp saved: %s", path)
                downloaded = True                              # ★ FIX 3
                break                                          # ★ FIX 3: exit retry loop
 
            except Exception as exc:
                log.warning("  Attempt %d failed: %s - %s", attempt + 1, fname, exc)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
 
        if not downloaded:                                     # ★ FIX 3
            failed_names.append(fname)
            log.error("  GAVE UP on: %s after %d attempts", fname, max_retries + 1)
 
    if failed_names:                                           # ★ FIX 3
        log.warning("  Download summary: %d OK, %d FAILED %s",
                     len(saved), len(failed_names), failed_names)
 
    return saved
 

def _att_name(att) -> str:
    try:
        return att.locator("div.VlyYV").first.get_attribute("title", timeout=3000) or "unknown"
    except Exception:
        pass
    try:
        a = att.get_attribute("aria-label", timeout=3000) or ""
        m = re.match(r"^(.+?)\s+Open\s+", a)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return "unknown"


# ══════════════════════════════════════════════════════════════════════
#  File management
# ══════════════════════════════════════════════════════════════════════

def _file_md5(filepath: str) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def move_file_to_folder(tmp_path: str, dest_dir: str) -> str | None:
    """
    Move a temp file to dest_dir with MD5 duplicate detection.
    Returns saved filename, or None if skipped as exact duplicate.
    """
    os.makedirs(dest_dir, exist_ok=True)
    tmp_md5 = _file_md5(tmp_path)
    for existing in os.listdir(dest_dir):
        ep = os.path.join(dest_dir, existing)
        if os.path.isfile(ep) and _file_md5(ep) == tmp_md5:
            log.info("    Skipped (duplicate of %s): %s", existing, os.path.basename(tmp_path))
            return None

    fname = os.path.basename(tmp_path)
    dest = os.path.join(dest_dir, fname)
    if os.path.exists(dest):
        b, e = os.path.splitext(fname)
        c = 1
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{b}_{c}{e}")
            c += 1
    shutil.move(tmp_path, dest)
    log.info("    Saved: %s", dest)
    return os.path.basename(dest)


def cleanup_temp(temp_files: list[str]):
    """Remove temp files and their directory."""
    for f in temp_files:
        try:
            os.remove(f)
        except Exception:
            pass
    if temp_files:
        try:
            os.rmdir(os.path.dirname(temp_files[0]))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
#  Subject folder fallback
# ══════════════════════════════════════════════════════════════════════

def _sanitize(name: str) -> str:
    c = re.sub(r'[<>:"/\\|?*]', '_', name)
    return re.sub(r'[_\s]+', '_', c).strip('_. ')[:80]


def subject_folder_fallback(subject: str) -> str:
    """Derive a folder name from the email subject when MBL extraction fails."""
    m = re.search(r'(OI\d{4,})', subject)
    if m: return m.group(1)
    m = re.search(r'Instant DO.*?(\d{6,12})\s*-\s*([A-Z0-9]{8,})', subject)
    if m: return f"{m.group(1)}_{m.group(2)}"
    m = re.search(r'Release.*?-\s*([A-Z0-9]{8,})', subject)
    if m: return m.group(1)
    m = re.search(r'([A-Z0-9]{8,})\s+Delivery Order Request.*?(\d{6,12})', subject)
    if m: return f"{m.group(2)}_{m.group(1)}"
    m = re.search(r'dossiernr\.?\s*(\d{4,})', subject)
    if m: return f"dossier_{m.group(1)}"
    m = re.search(r'(JI-\d{4}-\d{5,})', subject)
    if m: return m.group(1)
    return _sanitize(subject)


def normalize_oi_reference(raw: str) -> str:
    """
    Fix common OCR/LLM misreads in OI references.
 
    Problem:
      Gemini sometimes reads "OI2619032" as "012619032" (letter O → digit 0).
      Jordex search fails because "012619032" doesn't match "OI2619032".
 
    Rules:
      - If it starts with "0I" (zero + I) → replace with "OI"
      - If it starts with "01" (zero + one) followed by 5+ digits → likely "OI"
      - If it starts with "0i" → replace with "OI"
      - Preserve anything that already starts with "OI" or "OE"
      - Strip whitespace and hyphens
 
    Examples:
      "012619032"  → "OI2619032"
      "0I2619032"  → "OI2619032"
      "OI2619032"  → "OI2619032"  (no change)
      "OE2614817"  → "OE2614817"  (no change)
      " 012619039" → "OI2619039"
      "0i2619034"  → "OI2619034"
    """
    if not raw:
        return raw
 
    cleaned = raw.strip().replace("-", "").replace(" ", "")
 
    # Already correct
    if re.match(r'^O[IE]\d', cleaned, re.IGNORECASE):
        return cleaned[:2].upper() + cleaned[2:]
 
    # Starts with "0I" or "0i" (zero then letter I) → clearly OI
    if re.match(r'^0[Ii]\d', cleaned):
        fixed = "OI" + cleaned[2:]
        log.info("  OI normalized: '%s' → '%s'", raw, fixed)
        return fixed
 
    # Fix 'O1' or 'o1' (Letter O then Number 1) → misread of OI
    if re.match(r'^[oO]1\d', cleaned):
        fixed = "OI" + cleaned[2:]
        log.info("  OI normalized (O1 fix): '%s' → '%s'", raw, fixed)
        return fixed
 
    # Starts with "01" followed by 5+ digits → very likely OI (not a real number)
    if re.match(r'^01\d{5,}$', cleaned):
        fixed = "OI" + cleaned[2:]
        log.info("  OI normalized: '%s' → '%s'", raw, fixed)
        return fixed
 
    # Starts with just "0" followed by 6+ digits and no other letters
    # This catches "02619032" → "OI2619032" patterns (dropped the I entirely)
    # BUT only if length matches typical OI format (OI + 7 digits = 9 chars)
    if re.match(r'^0\d{7}$', cleaned):
        fixed = "OI" + cleaned[1:]
        log.info("  OI normalized (single zero prefix): '%s' → '%s'", raw, fixed)
        return fixed
 
    return cleaned
 
 
# ══════════════════════════════════════════════════════════════════════
#  2. MULTI-ATTACHMENT SKIP CHECK
# ══════════════════════════════════════════════════════════════════════
  
def should_skip_multi_attachment(page, max_allowed: int = 1) -> bool:
    """
    Check if the currently open email has more than `max_allowed` PDF attachments.
    If yes, return True → caller should skip this email entirely.
 
    Uses two strategies:
      1. Playwright locator on attachment listbox
      2. JS DOM fallback counting all attachment items
    """
    try:
        # ── Strategy 1: Playwright locator ───────────────────────────
        container = None
        for selector in [
            "#ReadingPaneContainerId div[role='listbox'][aria-label='file attachments']",
            "#ReadingPaneContainerId div[role='listbox'][aria-label='bestandsbijlagen']",  # Dutch
            "#ReadingPaneContainerId div[role='listbox']",  # generic fallback
        ]:
            try:
                loc = page.locator(selector).first
                if loc.is_visible(timeout=3000):
                    container = loc
                    break
            except Exception:
                continue
 
        if container:
            attachments = container.locator("div[role='option']")
            total_count = attachments.count()
            log.info("  Attachment check: found %d attachment(s) via locator", total_count)
 
            if total_count > max_allowed:
                # Count PDFs specifically
                pdf_count = 0
                for i in range(total_count):
                    att = attachments.nth(i)
                    try:
                        name = att.locator("div.VlyYV").first.get_attribute("title", timeout=2000) or ""
                    except Exception:
                        try:
                            aria = att.get_attribute("aria-label", timeout=2000) or ""
                            name = aria.split(" Open ")[0] if " Open " in aria else aria
                        except Exception:
                            name = ""
 
                    if name.lower().endswith(".pdf"):
                        pdf_count += 1
 
                log.info("  Attachment check: %d PDF(s) out of %d total", pdf_count, total_count)
 
                if pdf_count > max_allowed:
                    log.info("  SKIPPING: %d PDFs > max_allowed=%d", pdf_count, max_allowed)
                    return True
                else:
                    log.info("  NOT skipping: only %d PDF(s)", pdf_count)
                    return False
            else:
                log.info("  NOT skipping: total attachments %d <= %d", total_count, max_allowed)
                return False
 
        # ── Strategy 2: JS DOM fallback ──────────────────────────────
        log.info("  Attachment check: locator failed, trying JS fallback")
        js_count = page.evaluate("""() => {
            const pane = document.getElementById('ReadingPaneContainerId');
            if (!pane) return -1;
 
            // Try listbox first
            const listbox = pane.querySelector('div[role="listbox"]');
            if (listbox) {
                return listbox.querySelectorAll('div[role="option"]').length;
            }
 
            // Fallback: count any attachment-like elements
            const attachments = pane.querySelectorAll(
                '[data-testid="AttachmentCard"], ' +
                '.attachments-area div[role="option"], ' +
                '.attachment-item'
            );
            return attachments.length;
        }""")
 
        log.info("  Attachment check (JS): found %d attachment(s)", js_count)
 
        if js_count > max_allowed:
            log.info("  SKIPPING (JS): %d attachments > max_allowed=%d", js_count, max_allowed)
            return True
        elif js_count == -1:
            log.warning("  Attachment check: ReadingPane not found — NOT skipping")
            return False
        else:
            log.info("  NOT skipping (JS): %d attachments", js_count)
            return False
 
    except Exception as e:
        log.warning("  Attachment check FAILED: %s — NOT skipping (fail-open)", e)
        return False


# ══════════════════════════════════════════════════════════════════════
#  Mark email as UNREAD
# ══════════════════════════════════════════════════════════════════════

def mark_as_unread(outlook_page: Page, conv_id: str) -> bool:
    """
    Mark an email as unread in Outlook using Ctrl+U.

    Assumes the email is already in the currently open folder (which it
    always is, since we process emails from the folder we navigated to).

    Args:
        outlook_page: The Outlook Playwright page
        conv_id:      The data-convid of the email to mark unread

    Returns:
        True if successfully marked unread, False otherwise
    """
    try:
        if not click_row(outlook_page, conv_id):
            log.warning("  mark_as_unread: could not select email %s", conv_id)
            return False
        outlook_page.wait_for_timeout(500)
        outlook_page.keyboard.press("Control+u")
        outlook_page.wait_for_timeout(500)
        log.info("  Marked as UNREAD: %s", conv_id)
        return True
    except Exception as e:
        log.warning("  mark_as_unread FAILED for %s: %s", conv_id, e)
        return False


# ══════════════════════════════════════════════════════════════════════
#  Jordex search with fallback
# ══════════════════════════════════════════════════════════════════════
def search_jordex_with_fallback(
    jordex_page,
    outlook_page,
    primary_ref: str,
    secondary_ref: str = None,
    conv_id: str = None,
    tracker=None,
    cat: str = "",
    service_key: str = "",
    search_fn=None,
) -> tuple:
    """
    Validate which ref exists in Jordex WITHOUT keeping the row open.
 
    CHANGE: After finding a match, calls go_back() immediately so the
    while loop in _upload_to_jordex can do the actual opens cleanly.
    This eliminates the double-search problem (25s wasted per item).
    """
    from jordex.browser import go_back
 
    if primary_ref:
        log.info("[%s] Jordex search PRIMARY: '%s'", service_key, primary_ref)
        success, rows_found = search_fn(jordex_page, primary_ref, row_index=0)
        if success and rows_found > 0:
            go_back(jordex_page)           # ★ go back so while loop can re-open
            return True, primary_ref, rows_found
        log.warning("[%s] PRIMARY not found: '%s'", service_key, primary_ref)
 
    if secondary_ref and secondary_ref != primary_ref:
        log.info("[%s] Jordex search SECONDARY: '%s'", service_key, secondary_ref)
        success, rows_found = search_fn(jordex_page, secondary_ref, row_index=0)
        if success and rows_found > 0:
            log.info("[%s] SECONDARY found: '%s'", service_key, secondary_ref)
            go_back(jordex_page)           # ★ go back so while loop can re-open
            return True, secondary_ref, rows_found
        log.warning("[%s] SECONDARY not found: '%s'", service_key, secondary_ref)
 
    tried = " / ".join(filter(None, [primary_ref, secondary_ref]))
    log.warning("[%s] NOT FOUND in Jordex (%s) → marking email unread", service_key, tried)
    if conv_id and outlook_page:
        mark_as_unread(outlook_page, conv_id)
    if tracker and conv_id and cat:
        tracker.update_status(cat, conv_id, "jordex_not_found")
    return False, None, 0
 
 

# ══════════════════════════════════════════════════════════════════════
#  SCAC AND CARRIER CODE HELPERS
# ══════════════════════════════════════════════════════════════════════

KNOWN_SCAC_PREFIXES = {
    "HLCU", "MAEU", "MRKU", "MSCU", "MEDU", "ONEY", "YMLU", "EGLV",
    "COSU", "OOLU", "ZIMU", "CMDU", "HDMU", "PCIU", "WHLC", "SUDU",
    "COEU", "PNKG", "ANNU", "APLU", "CHNJ", "SMLM", "SNKO",
}

# Carrier name → SCAC fallback (when Gemini misses carrier_code)
CARRIER_NAME_TO_SCAC = {
    "ONE":                  "ONEY",
    "OCEAN NETWORK EXPRESS": "ONEY",
    "CMA CGM":              "CMDU",
    "HAPAG-LLOYD":          "HLCU",
    "HAPAG LLOYD":          "HLCU",
    "MAERSK":               "MAEU",
    "MSC":                  "MEDU",
    "OOCL":                 "OOLU",
    "EVERGREEN":            "EGLV",
    "ZIM":                  "ZIMU",
    "YANG MING":            "YMLU",
    "HMM":                  "HDMU",
    "HYUNDAI":              "HDMU",
    "COSCO":                "COSU",
    "PIL":                  "PCIU",
    "WAN HAI":              "WHLC",
    "HAMBURG SUD":          "SUDU",
    "HAMBURG SÜD":          "SUDU",
    "PANDA":                "PNKG",
}

def resolve_carrier_code(carrier_name: str, carrier_code: str) -> str | None:
    """Resolve carrier_code from Gemini output or fall back to carrier_name lookup."""
    # If Gemini returned a valid code, use it
    if carrier_code and carrier_code.upper() in KNOWN_SCAC_PREFIXES:
        return carrier_code.upper()
    # Fallback: match carrier_name against known mappings
    if carrier_name:
        name_upper = carrier_name.upper().strip()
        for key, scac in CARRIER_NAME_TO_SCAC.items():
            if key in name_upper:
                return scac
    return (carrier_code or "").upper() or None

# Known carrier B/L prefixes that are NOT the SCAC but DO indicate
# the carrier identity is already embedded in the reference number.
CARRIER_BL_PREFIXES = {
    "YM":   "YMLU",   # Yang Ming: YMJAN..., YMLUW...
    "ONE":  "ONEY",   # ONE: ONEY already caught, but ONE prefix too
    "HD":   "HDMU",   # HMM/Hyundai: HDMU caught, but HDJS... etc
    "CM":   "CMDU",   # CMA CGM: CMDU caught, but CMAJ... etc
    "ZI":   "ZIMU",   # ZIM
    "SU":   "SUDU",   # Hamburg Süd
    "WH":   "WHLC",   # Wan Hai
}

def ensure_scac_prefix(reference: str, carrier_code: str) -> str:
    """
    Ensure reference has a carrier prefix for Jordex search.
 
    FIX: Added Check 1.5 — detect double SCAC prefix.
    e.g. Gemini returns 'HDMUHDMNLE49370200' where HDMU is the SCAC
    and HDMN is the actual BL prefix. Strip the redundant SCAC.
    """
    if not reference or not carrier_code:
        return reference
 
    # 0. Global OCR Correction for OI numbers
    import re
    if re.match(r'^(01|0I|O1)\d{5,}$', reference.upper()):
        old_ref = reference
        reference = "OI" + reference[2:]
        log.info("  Global OCR Correction in SCAC applier: %s -> %s", old_ref, reference)
 
    ref_upper = reference.upper()
    code_upper = carrier_code.upper()
 
    # Check 0.5: Internal order numbers (OI/OE) — never prepend
    if ref_upper.startswith("OI") or ref_upper.startswith("OE"):
        log.info("  Ref '%s' is an internal order number (OI/OE) — no prepend", reference)
        return reference
 
    # Check 1: starts with a known 4-letter SCAC
    if ref_upper[:4] in KNOWN_SCAC_PREFIXES:
        # ★ Check 1.5: DOUBLE SCAC — Gemini prepended SCAC to a BL that
        #   already has a carrier prefix (e.g. HDMU + HDMNLE49370200)
        #   If removing the first 4 chars still looks like a valid BL
        #   with a carrier prefix, strip the redundant SCAC.
        remainder = ref_upper[4:]
        if ref_upper[:4] == code_upper:
            # Same SCAC twice? Check if remainder also starts with a known prefix
            for bl_prefix in CARRIER_BL_PREFIXES:
                if remainder.startswith(bl_prefix):
                    log.info("  Stripping double SCAC: '%s' → '%s' "
                             "(SCAC %s was prepended to BL with prefix %s)",
                             reference, reference[4:], code_upper, bl_prefix)
                    return reference[4:]
 
        log.info("  Ref '%s' already has known SCAC prefix — no prepend", reference)
        return reference
 
    # Check 2: starts with a known carrier BL prefix for this carrier
    for bl_prefix, scac in CARRIER_BL_PREFIXES.items():
        if ref_upper.startswith(bl_prefix) and scac == code_upper:
            log.info("  Ref '%s' has carrier BL prefix '%s' for %s — no prepend",
                     reference, bl_prefix, code_upper)
            return reference
 
    # Check 3: first 2 chars match carrier_code's first 2 chars
    if len(ref_upper) >= 2 and len(code_upper) >= 2 and ref_upper[:2] == code_upper[:2]:
        log.info("  Ref '%s' shares prefix with %s — no prepend", reference, code_upper)
        return reference
 
    # Otherwise: prepend the SCAC
    log.info("  Prepending SCAC %s to ref '%s'", code_upper, reference)
    return code_upper + reference