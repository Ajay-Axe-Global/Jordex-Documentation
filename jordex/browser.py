import logging
import time as _time
from config import JORDEX_OCEAN_URL

log = logging.getLogger("jordex.browser")

def apply_zoom(page):
    try:
        page.evaluate("document.body.style.zoom = '0.75'")
    except: pass

def _wait_loading(page, timeout=12000):
    try:
        page.locator(".el-loading-mask, ._loading-anim").first.wait_for(state="hidden", timeout=timeout)
    except: pass
    page.wait_for_timeout(800)


# ══════════════════════════════════════════════════════════════════════
#  Jordex Update Popup Detection & Recovery
# ══════════════════════════════════════════════════════════════════════

def dismiss_update_popup(page, service_key: str = "") -> bool:
    """
    Detect and dismiss the Jordex "A new version is available" update popup.

    Detection strategy — the popup has a visible "Refresh" button:
      1. Fast JS DOM scan (<5ms) — looks for any visible <button> whose
         text is exactly or closely "Refresh" / "Vernieuwen" (Dutch).
      2. Also checks for broader update-panel keywords as a safety net
         ("new version", "update", "please refresh", "vernieuwen").
      3. If found → page.reload() once, then re-navigate to the ocean
         shipment list so subsequent operations can proceed normally.

    Called at the entry point of every major Jordex operation (search,
    upload, go_back). Runs reactively — NOT on a background timer.

    Returns:
        True  — popup was found and page was reloaded.
        False — no popup; caller continues normally.
    """
    try:
        found = page.evaluate("""() => {
            // ── Primary check: visible button with "Refresh" text ──────────
            // This is the unique fingerprint of the Jordex update popup.
            // No other part of the Jordex UI has a standalone "Refresh" button.
            const refreshKeywords = ['refresh', 'vernieuwen', 'herladen', 'reload'];

            const allButtons = [...document.querySelectorAll('button')];
            for (const btn of allButtons) {
                if (!btn.offsetParent) continue;           // not visible
                const rect = btn.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                const text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                if (refreshKeywords.some(kw => text === kw || text.startsWith(kw))) {
                    // Extra safety: make sure we're NOT inside a normal Jordex dialog
                    // (upload dialog, confirm dialog) — those have 'Save'/'Cancel' nearby
                    const dialog = btn.closest('.el-dialog, .el-message-box');
                    if (dialog) {
                        // A dialog with a Refresh button AND Save/Cancel is a normal dialog
                        const hasSave   = !!dialog.querySelector('button[class*="primary"]');
                        const hasCancel = !!(dialog.innerText || '').toLowerCase().includes('cancel');
                        if (hasSave && hasCancel) continue;  // normal dialog, skip
                    }
                    return { found: true, via: 'refresh_button', text: text };
                }
            }

            // ── Secondary check: update-panel keyword scan ────────────────
            // Catches popups that have text but their Refresh button uses an
            // icon-only button or has unusual markup.
            const updateKeywords = [
                'new version', 'nieuwe versie',
                'please refresh', 'vernieuw de pagina',
                'update available', 'update beschikbaar',
                'reload the page', 'pagina vernieuwen',
            ];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
            let node;
            while ((node = walker.nextNode())) {
                if (!node.offsetParent) continue;
                const rect = node.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                // Only scan leaf-ish nodes to avoid matching giant containers
                if (node.children.length > 5) continue;
                const text = (node.innerText || '').trim().toLowerCase();
                if (text.length < 5 || text.length > 400) continue;
                for (const kw of updateKeywords) {
                    if (text.includes(kw)) {
                        return { found: true, via: 'keyword', text: text.slice(0, 80) };
                    }
                }
            }

            return { found: false };
        }""")
    except Exception as e:
        log.debug("[%s] dismiss_update_popup JS eval error: %s", service_key, e)
        return False

    if not found or not found.get("found"):
        return False

    via  = found.get("via", "?")
    text = found.get("text", "")
    log.warning(
        "[%s] ⚠ Jordex update popup DETECTED (via=%s text='%s') — reloading page...",
        service_key, via, text,
    )

    # ── Reload & recover ──────────────────────────────────────────────
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
    except Exception as e:
        log.warning("[%s] dismiss_update_popup: reload failed (%s) — trying goto", service_key, e)
        try:
            page.goto(JORDEX_OCEAN_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception as e2:
            log.error("[%s] dismiss_update_popup: goto also failed: %s", service_key, e2)
            return True  # popup was found even if recovery is partial

    # ── Ensure we're back on the shipment list ────────────────────────
    if "ocean" not in page.url.lower():
        try:
            page.goto(JORDEX_OCEAN_URL, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
        except Exception as e:
            log.warning("[%s] dismiss_update_popup: post-reload navigation failed: %s", service_key, e)

    apply_zoom(page)
    log.info("[%s] ✓ Page reloaded after update popup — resuming.", service_key)
    return True


def normalize_dashboard_filters(page, timeout=3000):
    if not page.url.endswith("ocean"):
        log.info("Navigating to shipment list before normalizing filters...")
        try:
            page.goto(JORDEX_OCEAN_URL, wait_until="load")
            page.wait_for_timeout(3000)
        except: pass

    log.info("Normalizing Jordex dashboard filters (fast JS evaluation)...")
    try:
        page.evaluate("""() => {
  const TARGET = { status: "Active", loadType: "All", shipment: "Import", orderBy: "", from: "" };

  function getValue(filter, label = null) {
    if (!filter) return "";
    const items = filter.querySelectorAll(".filter-select-container-item");
    if (!items.length) return "";
    if (label && items[0].classList.contains("filter-select-label") && items[0].innerText.trim() === label)
      return items.length > 1 ? items[1].innerText.trim() : "";
    return items[0].innerText.trim();
  }

  function readState() {
    const f = document.querySelectorAll(".filter-select-container");
    if (f.length < 5) return { status: "", loadType: "", shipment: "", orderBy: "", from: "" };
    return {
      status:   getValue(f[0]),
      loadType: getValue(f[1], "Load type"),
      shipment: getValue(f[2]),
      orderBy:  getValue(f[3], "Order by"),
      from:     getValue(f[4], "From"),
    };
  }

  const FILTER_MAP = [
    { index: 0, key: "status",   target: TARGET.status   },
    { index: 1, key: "loadType", target: TARGET.loadType },
    { index: 2, key: "shipment", target: TARGET.shipment },
    { index: 3, key: "orderBy",  target: TARGET.orderBy  },
    { index: 4, key: "from",     target: TARGET.from     },
  ];

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  function reactClick(el) {
    if (!el) return;
    ["mousedown", "mouseup", "click"].forEach(t =>
      el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true }))
    );
  }

  function findVisibleByText(text) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    const hits = [];
    let n;
    while ((n = walker.nextNode()))
      if (n.children.length <= 1 && n.innerText?.trim() === text) hits.push(n);
    return hits.find(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; }) || null;
  }

  async function clearFilter(index) {
    const filter = document.querySelectorAll(".filter-select-container")[index];
    const icon = filter?.querySelector("i.filter-clear.el-icon-circle-close");
    if (icon) reactClick(icon);
    await sleep(200);
  }

  async function setFilter(index, target) {
    const filter = document.querySelectorAll(".filter-select-container")[index];
    reactClick(filter);
    await sleep(300);
    const el = findVisibleByText(target);
    if (el) reactClick(el);
    else document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await sleep(200);
  }

  return (async () => {
    const state = readState();
    if (!state) return true;
    const mismatches = FILTER_MAP.filter(({ key, target }) => state[key] !== target);
    if (!mismatches.length) return true;

    for (const { index, key, target } of mismatches) {
      const current = state[key];
      if (current === target) continue;
      if (target === "") {
        await clearFilter(index);
      } else {
        if (current !== "") await clearFilter(index);
        await setFilter(index, target);
      }
    }
    return true;
  })();
}""")
        page.wait_for_timeout(1000)
    except Exception as e:
        log.warning(f"Filter normalization issue: {e}")

def search_and_open(page, query, row_index=0):
    # ── Guard: dismiss Jordex update popup before doing anything ─────
    # The popup silently breaks search input, dialogs and navigation.
    # Detecting it here (at the entry to every search) means we recover
    # before the search even starts, so no retries are wasted.
    dismiss_update_popup(page, service_key="search_and_open")

    if not page.url.endswith("ocean"):
        log.info("Navigating to shipment list before searching...")
        try:
            page.goto(JORDEX_OCEAN_URL, wait_until="load")
            page.wait_for_timeout(3000)
        except: pass
        
    normalize_dashboard_filters(page)
    
    log.info(f"Searching for {query} in Jordex (row index: {row_index})...")
    try:
        page.locator(".el-table__body").first.wait_for(state="visible", timeout=10000)
    except:
        page.wait_for_timeout(3000)

    filled = False
    search_start = _time.monotonic()

    for attempt in range(1, 8):
        # ── 30-second stall guard ────────────────────────────────────
        # If we've been trying to fill the search input for >30s without
        # success, the page is likely frozen (update popup or JS hang).
        # Check for the popup and give it one more attempt after recovery.
        elapsed = _time.monotonic() - search_start
        if elapsed > 30 and not filled:
            log.warning(
                "search_and_open: search input not found after %.0fs — "
                "checking for update popup...", elapsed
            )
            if dismiss_update_popup(page, service_key="search_and_open/stall"):
                normalize_dashboard_filters(page)
                search_start = _time.monotonic()  # reset timer after recovery
            else:
                log.warning("search_and_open: no popup found — page may be genuinely broken")

        for sel in [
            "input.el-input__inner[placeholder='Search']",
            "input.el-input__inner[placeholder*='search']",
            "input.el-input__inner[placeholder*='Search']",
            ".mf-search-input input"
        ]:
            try:
                inp = page.locator(sel).first
                if inp.is_visible(timeout=2000):
                    inp.click(); page.wait_for_timeout(500)
                    inp.fill(""); inp.fill(query)
                    page.wait_for_timeout(500)
                    inp.press("Enter")
                    filled = True
                    break
            except: continue
        if filled: break
        page.wait_for_timeout(1000)
    
    if not filled:
        log.error("Search input not found.")
        return False, 0

    page.wait_for_timeout(500)
    _wait_loading(page)

    total_matching_rows = 0
    target_row = None
    for attempt in range(4):
        # 1. Try to find the exact text in row (Works for MBL/OI)
        for sel in [f"tr.shipment-row:has-text('{query}')", f".mf-table tr:has-text('{query}')", f"tr:has-text('{query}')"]:
            try:
                rows = [r for r in page.locator(sel).all() if r.is_visible()]
                if len(rows) > 0:
                    total_matching_rows = max(total_matching_rows, len(rows))
                if len(rows) > row_index:
                    target_row = rows[row_index]
                    break
            except: continue
        if target_row: break
        
        # 2. If exact text not found (e.g. Container Search), just pick the row_index-th row in the table
        try:
            rows = [r for r in page.locator("tr.shipment-row").all() if r.is_visible(timeout=1000)]
            if len(rows) > 0:
                total_matching_rows = max(total_matching_rows, len(rows))
            if len(rows) > row_index:
                target_row = rows[row_index]
                break
        except: pass

        page.wait_for_timeout(2000)

    if not target_row:
        if row_index == 0:
            log.warning(f"Shipment {query} not found in Jordex.")
        return False, 0

    for retry in range(2):
        try:
            target_row.scroll_into_view_if_needed(timeout=4000)
            target_row.click(timeout=8000)
            page.wait_for_load_state("load", timeout=30000)
            page.wait_for_timeout(2000)
            apply_zoom(page)
            return True, total_matching_rows
        except Exception as e:
            if retry == 0:
                log.warning(f"Failed to open shipment row on first try: {e}. Retrying...")
                page.wait_for_timeout(2000)
            else:
                log.error(f"Failed to open shipment row after retry: {e}")
                return False, 0

def go_back(page):
    log.info("Going back to shipment list...")
    # Wait for any prior UI transitions (like closing sidebars/saving) to finish
    page.wait_for_timeout(1500)

    try:
        page.evaluate("""() => {
            const btn = [...document.querySelectorAll('button')].find(btn => btn.innerText.includes('Back'));
            if (btn) btn.click();
        }""")
        page.wait_for_timeout(500)
        _wait_loading(page)
    except: pass
    
    # Verification and fallback
    if "ocean" not in page.url.lower():
        log.warning("Did not return to ocean list via Back button. Force navigating...")
        # Before forcing navigation, check if the update popup is blocking the Back button
        if dismiss_update_popup(page, service_key="go_back"):
            # Popup was the culprit — retry the Back button once after reload
            try:
                page.evaluate("""() => {
                    const btn = [...document.querySelectorAll('button')].find(btn => btn.innerText.includes('Back'));
                    if (btn) btn.click();
                }""")
                page.wait_for_timeout(1500)
                _wait_loading(page)
            except: pass
        if "ocean" not in page.url.lower():
            try: page.goto(JORDEX_OCEAN_URL, wait_until="load"); page.wait_for_timeout(3000)
            except: pass
    
    _wait_loading(page)
    apply_zoom(page)
