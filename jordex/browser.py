import logging
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
    for attempt in range(1, 8):
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
        try: page.goto(JORDEX_OCEAN_URL, wait_until="load"); page.wait_for_timeout(3000)
        except: pass
    
    _wait_loading(page)
    apply_zoom(page)
