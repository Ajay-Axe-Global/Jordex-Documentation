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

def _identify_filters(page):
    return page.evaluate("""() => {
        const containers = [...document.querySelectorAll('.filter-select-container')];
        const map = {};
        containers.forEach((c, i) => {
            const text = (c.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const hasLabel = c.querySelector('.filter-select-label');
            const labelText = hasLabel ? hasLabel.innerText.trim().toLowerCase() : '';
            const isActive = c.classList.contains('active');
            const hasClear = !!c.querySelector('.filter-clear');

            // ── Filters WITH a label (unselected / default state) ──
            if (labelText.includes('load type'))       { map['load_type'] = i; return; }
            if (labelText.includes('order'))            { map['order_by'] = i; return; }
            if (labelText.includes('from'))             { map['from'] = i; return; }
            if (labelText.includes('to '))              { map['to'] = i; return; }
            if (labelText.includes('origin'))           { map['origin'] = i; return; }
            if (labelText.includes('direction'))        { map['direction'] = i; return; }
            if (labelText.includes('status'))           { map['status'] = i; return; }

            // ── Filters WITHOUT a label (active / selected state) ──
            // Status: "Active", "Inactive", "Completed"
            if (/^(active|inactive|completed|all)/.test(text) && !text.includes('from') && !text.includes('to ')) {
                if (!map['status']) map['status'] = i;
                return;
            }
            // Direction: "Import", "Export"  
            if (/^(import|export)/.test(text)) {
                if (!map['direction']) map['direction'] = i;
                return;
            }
            // Load type when active: "FCL", "LCL" (but not "All" — that's ambiguous)
            if (/^(fcl|lcl)/.test(text)) {
                if (!map['load_type']) map['load_type'] = i;
                return;
            }

            // ── Date filters (active chips like "From 12 Jul", "To 20 Jul") ──
            if (/\\d{1,2}\\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(text)) {
                if (text.startsWith('from') || text.startsWith('van')) {
                    map['from'] = i;
                } else if (text.startsWith('to') || text.startsWith('tot')) {
                    map['to'] = i;
                } else if (!map['from']) {
                    map['from'] = i;  // generic date → treat as "from"
                } else {
                    map['to'] = i;
                }
                return;
            }

            // ── Order-by when active: "Arrival", "Departure", "ETA", etc. ──
            if (isActive && hasClear && !map['order_by']) {
                // If we get here, it's an active filter with a clear button
                // that didn't match status/direction/load/date → likely order_by
                if (/^(arrival|departure|eta|etd|created|updated|ata|atd)/.test(text)) {
                    map['order_by'] = i;
                    return;
                }
            }
        });
        return map;
    }""")

def _get_filter_value(page, index):
    return page.evaluate(f"""() => {{
        const c = document.querySelectorAll('.filter-select-container')[{index}];
        if (!c) return '';
        const items = [...c.querySelectorAll('.filter-select-container-item')];
        const texts = items
            .filter(el => !el.classList.contains('filter-select-label'))
            .map(el => (el.innerText || '').trim())
            .filter(t => t && !t.includes('\\u00a0'));
        return texts[0] || '';
    }}""")

def _click_filter_arrow(page, index, timeout=3000):
    containers = page.locator(".filter-select-container")
    container = containers.nth(index)
    arrow = container.locator(".pointer.el-icon-arrow-right")
    arrow.click(timeout=timeout)
    page.wait_for_timeout(600)

def _click_filter_clear(page, index, timeout=3000):
    containers = page.locator(".filter-select-container")
    container = containers.nth(index)
    clear_btn = container.locator(".filter-clear.el-icon-circle-close")
    try:
        if clear_btn.is_visible(timeout=1500):
            clear_btn.click(timeout=timeout)
            page.wait_for_timeout(500)
            _wait_loading(page)
            return True
    except: pass
    return False

def _select_dropdown_option(page, option_text, timeout=3000):
    try:
        # Check if already selected
        selected_opt = page.locator("li.selected:visible").filter(has_text=option_text).first
        if selected_opt.is_visible(timeout=500):
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            return
    except: pass

    try:
        page.get_by_role("main").locator("li").filter(has_text=option_text).first.click(timeout=timeout)
    except:
        try:
            page.locator("li:visible").filter(has_text=option_text).first.click(timeout=timeout)
        except: pass
        
    page.wait_for_timeout(500)
    _wait_loading(page)

def normalize_dashboard_filters(page, timeout=3000):
    if not page.url.endswith("ocean"):
        log.info("Navigating to shipment list before normalizing filters...")
        try:
            page.goto(JORDEX_OCEAN_URL, wait_until="load")
            page.wait_for_timeout(3000)
        except: pass

    log.info("Normalizing Jordex dashboard filters...")
    try:
        page.wait_for_timeout(2000)
        fmap = _identify_filters(page)

        if 'status' in fmap:
            try:
                current = _get_filter_value(page, fmap['status']).strip().lower()
                if current != 'active':
                    _click_filter_arrow(page, fmap['status'], timeout)
                    _select_dropdown_option(page, "Active", timeout)
            except: pass

        if 'load_type' in fmap:
            try:
                current = _get_filter_value(page, fmap['load_type']).strip().lower()
                if current != 'all':
                    _click_filter_arrow(page, fmap['load_type'], timeout)
                    _select_dropdown_option(page, "All", timeout)
            except: pass

        if 'direction' in fmap:
            try:
                current = _get_filter_value(page, fmap['direction']).strip().lower()
                if current != 'import':
                    _click_filter_arrow(page, fmap['direction'], timeout)
                    _select_dropdown_option(page, "Import", timeout)
            except: pass

        for f in ['order_by', 'from', 'to']:
            if f in fmap:
                try: _click_filter_clear(page, fmap[f])
                except: pass

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
        return False

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
    try:
        page.evaluate("""() => {
            const btn = [...document.querySelectorAll('button')].find(btn => btn.innerText.includes('Back'));
            if (btn) btn.click();
        }""")
        page.wait_for_timeout(500)
        _wait_loading(page)
    except: pass
    
    if not page.url.endswith("ocean"):
        try: page.goto(JORDEX_OCEAN_URL, wait_until="load"); page.wait_for_timeout(3000)
        except: pass
    
    _wait_loading(page)
    apply_zoom(page)
