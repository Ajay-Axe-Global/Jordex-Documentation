"""
services/arrival_notice/fcs_handler.py — FCS Carrier Special Handling
======================================================================
When the carrier is FCS (Famous Pacific Shipping), the Arrival Notice
requires additional post-upload steps in Jordex:

  1. Upload with doc_type "Arrival Notice" (renamed "AN") instead of
     the default carrier-documents type.
  2. Carrier tab  → verify / update Vessel Name.
  3. Destination tab → add Warehouse Address (Name, Address Line 1,
     Postal Code, City, Country).
  4. Lane tab → update Arrival date (ETA).
  5. Click Save on each tab where changes are made.

Selectors are derived from the Playwright recording and the Jordex UI
analysis document.
"""

import logging, time
from playwright.sync_api import Page, TimeoutError as PwTimeout

log = logging.getLogger("service.arrival_notice.fcs")

# ── Carrier detection ────────────────────────────────────────────────

FCS_CARRIER_NAMES = {
    "FPS", "FPS FAMOUS PACIFIC SHIPPING", "FAMOUS PACIFIC SHIPPING",
    "FCS", "SHANGHAI F S CONTAINER LINE",
}


def is_fcs_carrier(carrier_name: str | None, source_file: str | None = None) -> bool:
    """Return True if the carrier is FCS / Famous Pacific Shipping."""
    if carrier_name:
        name_upper = carrier_name.upper().strip()
        for pattern in FCS_CARRIER_NAMES:
            if pattern in name_upper:
                return True
    # Fallback: check if PDF filename contains FPS/FCS hints
    if source_file:
        sf = source_file.upper()
        if "FPS" in sf or "FCS" in sf or "FAMOUS PACIFIC" in sf:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════
#  1. OPEN VIEW ROUTING
# ══════════════════════════════════════════════════════════════════════

def _open_view_routing(page: Page) -> bool:
    """Click 'View routing' to open the routing sidebar/modal."""
    try:
        opened = page.evaluate("""() => {
            // Attempt 1: Specific class
            const el = document.querySelector('.routing-sidebar__routing-label');
            if (el) { el.click(); return true; }

            // Attempt 2: span with text 'View routing'
            const spans = [...document.querySelectorAll('span')];
            const vr = spans.find(s => s.textContent.trim() === 'View routing');
            if (vr) { vr.click(); return true; }

            // Attempt 3: any leaf element with exact text
            const all = [...document.querySelectorAll('*')];
            const leaf = all.find(e => e.childElementCount === 0 &&
                                       e.innerText?.trim() === 'View routing');
            if (leaf) { leaf.click(); return true; }

            return false;
        }""")
        if opened:
            page.wait_for_timeout(2000)
            return True
        log.warning("  FCS: Could not find 'View routing' element")
        return False
    except Exception as e:
        log.warning("  FCS: Failed to open View routing: %s", e)
        return False


# ══════════════════════════════════════════════════════════════════════
#  2. CARRIER TAB — VESSEL NAME UPDATE
# ══════════════════════════════════════════════════════════════════════

def _navigate_to_carrier_tab(page: Page) -> bool:
    """Click the Carrier tab."""
    try:
        page.get_by_role("tab", name="Carrier").click(timeout=5000)
        page.wait_for_timeout(2000)
        return True
    except Exception as e:
        log.warning("  FCS: Could not navigate to Carrier tab: %s", e)
        return False


def _get_current_vessel_name(page: Page) -> str | None:
    """Read the current vessel name from the Carrier tab."""
    try:
        vessel_name = page.evaluate("""() => {
            const items = [...document.querySelectorAll('.el-form-item')];
            const vesselItem = items.find(el => el.innerText.includes('Vessel name'));
            if (!vesselItem) return null;
            const input = vesselItem.querySelector('input');
            return input ? input.value : null;
        }""")
        return (vessel_name or "").strip() or None
    except Exception:
        return None


def _update_vessel_name(page: Page, vessel_name: str) -> bool:
    """
    Update the Vessel Name field on the Carrier tab using DOM injection.
    Uses the same approach as the existing codebase (typing char-by-char
    with InputEvent dispatch, then selecting from autocomplete dropdown).
    """
    try:
        log.info("  FCS: Updating vessel name to '%s'", vessel_name)

        # Step 1: Clear existing value and type new one
        result = page.evaluate("""(vesselName) => {
            const items = [...document.querySelectorAll('.el-form-item')];
            const vesselItem = items.find(el => el.innerText.includes('Vessel name'));
            if (!vesselItem) return { success: false, error: 'Vessel name field not found' };

            const input = vesselItem.querySelector('input');
            if (!input) return { success: false, error: 'Input element not found' };

            // Focus and clear
            input.focus();
            input.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));

            // Try to click clear icon
            const clearIcon = vesselItem.querySelector('.el-icon-circle-close');
            if (clearIcon) clearIcon.click();

            // Use native setter to clear
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            setter.call(input, '');
            input.dispatchEvent(new Event('input', { bubbles: true }));

            return { success: true, inputFound: true };
        }""", vessel_name)

        if not result.get("success"):
            log.warning("  FCS: %s", result.get("error", "Unknown error"))
            return False

        page.wait_for_timeout(500)

        # Step 2: Type character by character to trigger autocomplete
        page.evaluate("""async (vesselName) => {
            const items = [...document.querySelectorAll('.el-form-item')];
            const vesselItem = items.find(el => el.innerText.includes('Vessel name'));
            const input = vesselItem.querySelector('input');
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;

            for (let i = 0; i < vesselName.length; i++) {
                setter.call(input, vesselName.substring(0, i + 1));
                input.dispatchEvent(new InputEvent('input', {
                    bubbles: true, inputType: 'insertText', data: vesselName[i]
                }));
                await new Promise(r => setTimeout(r, 80));
            }
        }""", vessel_name)

        page.wait_for_timeout(1500)

        # Step 3: Try to select from autocomplete dropdown
        selected = page.evaluate("""(vesselName) => {
            const selectors = [
                '.el-autocomplete-suggestion li',
                '.el-select-dropdown__item'
            ];
            let options = [];
            for (const s of selectors) {
                const found = Array.from(document.querySelectorAll(s))
                    .filter(el => el.offsetParent !== null);
                if (found.length > 0) { options = found; break; }
            }

            if (options.length === 0) return { selected: false, reason: 'no_dropdown' };

            // Score each option
            const target = vesselName.toUpperCase();
            let bestOption = null;
            let bestScore = -1;

            for (const opt of options) {
                const text = (opt.textContent || '').trim().toUpperCase();
                let score = 0;
                if (text === target) score = 100;
                else if (text.includes(target)) score = 80;
                else if (target.includes(text)) score = 60;
                else {
                    // Partial word match
                    const words = target.split(/\s+/);
                    for (const w of words) {
                        if (text.includes(w)) score += 10;
                    }
                }
                if (score > bestScore) {
                    bestScore = score;
                    bestOption = opt;
                }
            }

            if (bestOption && bestScore > 0) {
                bestOption.click();
                return { selected: true, score: bestScore };
            }
            return { selected: false, reason: 'no_match' };
        }""", vessel_name)

        if selected.get("selected"):
            log.info("  FCS: Vessel name selected from dropdown (score=%s)",
                     selected.get("score"))
        else:
            log.info("  FCS: No dropdown match — typed value remains: '%s'", vessel_name)

        page.wait_for_timeout(1000)
        return True

    except Exception as e:
        log.warning("  FCS: Failed to update vessel name: %s", e)
        return False


def _save_carrier_tab(page: Page) -> bool:
    """Click the Save button on the Carrier tab."""
    try:
        # Look for Save button within the carrier tab pane
        save_clicked = page.evaluate("""() => {
            // Try specific pane first
            const pane = document.querySelector('#pane-carrier') ||
                         document.querySelector('[aria-labelledby*="carrier"]');
            const scope = pane || document;

            const buttons = [...scope.querySelectorAll('button')];
            const saveBtn = buttons.find(b =>
                b.textContent.trim().toLowerCase() === 'save' &&
                b.offsetParent !== null
            );
            if (saveBtn) { saveBtn.click(); return true; }

            // Fallback: any visible Save button
            const allBtns = [...document.querySelectorAll('button')];
            const fallback = allBtns.find(b =>
                b.textContent.trim().toLowerCase() === 'save' &&
                b.offsetParent !== null
            );
            if (fallback) { fallback.click(); return true; }

            return false;
        }""")
        if save_clicked:
            page.wait_for_timeout(2000)
            log.info("  FCS: Carrier tab saved")
        return save_clicked
    except Exception as e:
        log.warning("  FCS: Failed to save Carrier tab: %s", e)
        return False


# ══════════════════════════════════════════════════════════════════════
#  3. DESTINATION TAB — WAREHOUSE ADDRESS
# ══════════════════════════════════════════════════════════════════════

def _navigate_to_destination_tab(page: Page) -> bool:
    """Click the Destination tab inside View Routing."""
    try:
        dest_tab = page.get_by_text("Destination")
        dest_tab.first.click(timeout=5000)
        page.wait_for_timeout(2000)
        return True
    except Exception as e:
        log.warning("  FCS: Could not navigate to Destination tab: %s", e)
        return False


def _click_add_address_button(page: Page) -> bool:
    """Click the '+' button in the Destination tab to open the Address dialog."""
    try:
        # Primary: exact selector from Playwright recording
        add_btn_selector = (
            "#pane-destination > div > .el-card > .el-card__body > .el-form > div > "
            ".routing-tab-panel__body > div:nth-child(2) > .wrap > div:nth-child(2) > "
            ".el-form-item > .el-form-item__content > div > "
            ".address-select__body--select > .address-select__toolbar > "
            ".address-select-toolbar > .el-button"
        )
        btn = page.locator(add_btn_selector).first
        if btn.is_visible(timeout=3000):
            btn.click()
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass

    # Fallback: find any "+" or "add" button in the destination pane
    try:
        clicked = page.evaluate("""() => {
            const pane = document.querySelector('#pane-destination');
            if (!pane) return false;
            const btns = [...pane.querySelectorAll('.address-select-toolbar .el-button, button')];
            const addBtn = btns.find(b => {
                const text = b.textContent.trim();
                const icon = b.querySelector('i.el-icon-plus, i.el-icon-circle-plus');
                return icon || text === '+' || text === 'Add';
            });
            if (addBtn) { addBtn.click(); return true; }
            return false;
        }""")
        if clicked:
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass

    log.warning("  FCS: Could not find Add Address button in Destination tab")
    return False


def _fill_address_dialog(page: Page, warehouse: dict) -> bool:
    """
    Fill the Address dialog with warehouse details.

    warehouse dict keys:
      - warehouse_name:    e.g. "POD LOGISTICS & WAREHOUSING BV"
      - address_line_1:    e.g. "SHANNONWEG 72"
      - postal_code:       e.g. "3197 LH"
      - city:              e.g. "BOTLEK ROTTERDAM"
      - country:           e.g. "Netherlands"

    Flow:
      1. Type name in the autocomplete Name field.
      2. If a matching dropdown option appears, select it (pre-fills all fields → done).
      3. Otherwise, manually fill each field and select country.
      4. Click Save.
    """
    name = (warehouse.get("warehouse_name") or "").strip()
    if not name:
        log.warning("  FCS: No warehouse name to fill")
        return False

    try:
        # Wait for the Address dialog to be visible
        page.wait_for_selector(
            "div[aria-label='Address'] , div.el-dialog:has-text('Address')",
            state="visible", timeout=5000
        )
    except Exception:
        log.warning("  FCS: Address dialog did not appear")
        return False

    try:
        # ── Step 1: Type warehouse name in the autocomplete field ────
        name_input = page.locator(
            ".el-autocomplete > .el-input > .el-input__inner"
        ).first
        name_input.click()
        page.wait_for_timeout(300)
        name_input.fill(name)
        page.wait_for_timeout(1500)

        # ── Step 2: Check for autocomplete match ────────────────────
        dropdown_matched = page.evaluate("""(targetName) => {
            const items = document.querySelectorAll(
                '.el-autocomplete-suggestion__list li, ' +
                '.el-autocomplete-suggestion li'
            );
            const visible = [...items].filter(i => i.offsetParent !== null);
            const target = targetName.toUpperCase();

            for (const item of visible) {
                const text = (item.textContent || '').trim().toUpperCase();
                if (text.includes(target) || target.includes(text)) {
                    item.click();
                    return true;
                }
            }
            return false;
        }""", name)

        if dropdown_matched:
            log.info("  FCS: Warehouse address selected from autocomplete dropdown")
            page.wait_for_timeout(1000)
            # Dropdown selection may pre-fill everything — just save
            _click_dialog_save(page)
            return True

        # ── Step 3: Manual fill — no dropdown match ──────────────────
        log.info("  FCS: No autocomplete match — filling address fields manually")

        # Address line 1
        addr1 = (warehouse.get("address_line_1") or "").strip()
        if addr1:
            # The address line 1 field is typically the 2nd autocomplete or a plain input
            addr1_input = page.locator(
                "div:nth-child(2) > .el-form-item > .el-form-item__content > "
                ".el-autocomplete > .el-input > .el-input__inner"
            ).first
            try:
                addr1_input.click(timeout=2000)
                addr1_input.fill(addr1)
                page.wait_for_timeout(500)
            except Exception:
                # Fallback: find by position in form
                page.evaluate("""(value) => {
                    const dialog = document.querySelector('[aria-label="Address"]') ||
                                   document.querySelector('.el-dialog');
                    if (!dialog) return;
                    const inputs = [...dialog.querySelectorAll('input.el-input__inner')];
                    if (inputs.length >= 2) {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        setter.call(inputs[1], value);
                        inputs[1].dispatchEvent(new Event('input', { bubbles: true }));
                        inputs[1].dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""", addr1)

        # Postal code
        postal = (warehouse.get("postal_code") or "").strip()
        if postal:
            postal_input = page.locator(
                "div:nth-child(4) > .el-form-item > .el-form-item__content > "
                ".el-input > .el-input__inner"
            ).first
            try:
                postal_input.click(timeout=2000)
                postal_input.fill(postal)
                page.wait_for_timeout(300)
            except Exception:
                page.evaluate("""(value) => {
                    const dialog = document.querySelector('[aria-label="Address"]') ||
                                   document.querySelector('.el-dialog');
                    if (!dialog) return;
                    const labels = [...dialog.querySelectorAll('.el-form-item__label, label')];
                    const postalLabel = labels.find(l =>
                        l.textContent.trim().toLowerCase().includes('postal'));
                    if (postalLabel) {
                        const formItem = postalLabel.closest('.el-form-item');
                        const input = formItem?.querySelector('input');
                        if (input) {
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            setter.call(input, value);
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }
                }""", postal)

        # City
        city = (warehouse.get("city") or "").strip()
        if city:
            city_input = page.locator(
                ".el-form > .el-row > div:nth-child(5) > .el-form-item > "
                ".el-form-item__content > .el-input > .el-input__inner"
            ).first
            try:
                city_input.click(timeout=2000)
                city_input.fill(city)
                page.wait_for_timeout(300)
            except Exception:
                page.evaluate("""(value) => {
                    const dialog = document.querySelector('[aria-label="Address"]') ||
                                   document.querySelector('.el-dialog');
                    if (!dialog) return;
                    const labels = [...dialog.querySelectorAll('.el-form-item__label, label')];
                    const cityLabel = labels.find(l =>
                        l.textContent.trim().toLowerCase() === 'city');
                    if (cityLabel) {
                        const formItem = cityLabel.closest('.el-form-item');
                        const input = formItem?.querySelector('input');
                        if (input) {
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            ).set;
                            setter.call(input, value);
                            input.dispatchEvent(new Event('input', { bubbles: true }));
                            input.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }
                }""", city)

        # Country — uses a Select dropdown
        country = (warehouse.get("country") or "").strip()
        if country:
            try:
                # Click the country select placeholder
                country_select = page.get_by_role("dialog", name="Address").get_by_placeholder("Select")
                country_select.click()
                page.wait_for_timeout(500)
                country_select.fill(country)
                page.wait_for_timeout(1000)

                # Select from dropdown
                import re as _re
                page.locator("li").filter(
                    has_text=_re.compile(rf"^{_re.escape(country)}$", _re.IGNORECASE)
                ).first.click(timeout=3000)
                page.wait_for_timeout(500)
            except Exception as e:
                log.warning("  FCS: Country selection failed: %s", e)

        # ── Step 4: Save the address dialog ──────────────────────────
        _click_dialog_save(page)
        return True

    except Exception as e:
        log.warning("  FCS: Failed to fill address dialog: %s", e)
        # Try to close dialog on failure
        try:
            page.get_by_role("button", name="close drawer").click()
        except Exception:
            pass
        return False


def _click_dialog_save(page: Page):
    """Click Save in the Address dialog."""
    try:
        page.get_by_role("dialog").get_by_role("button", name="Save").click(timeout=5000)
        page.wait_for_timeout(2000)
        log.info("  FCS: Address dialog saved")
    except Exception as e:
        log.warning("  FCS: Could not click Save in Address dialog: %s", e)


def _save_destination_tab(page: Page) -> bool:
    """Click the Save button on the Destination tab."""
    try:
        # Try the specific save button near the Destination pane
        # Using the selector pattern from the Playwright recording for the
        # floated toolbar save button
        save_selector = (
            "#pane-destination > div > .el-card > .el-card__body > .el-form > div > "
            ".routing-tab-panel__body > div:nth-child(2) > .wrap > div:nth-child(2) > "
            ".el-form-item > .el-form-item__content > div > "
            ".address-select__body--select > .address-select__toolbar--floated > "
            "button > span > .more-btn__content > .address-select-toolbar > "
            "button:nth-child(2)"
        )
        btn = page.locator(save_selector).first
        if btn.is_visible(timeout=2000):
            btn.click()
            page.wait_for_timeout(2000)
            log.info("  FCS: Destination tab saved (floated toolbar)")
            return True
    except Exception:
        pass

    # Fallback: generic Save button in destination pane
    try:
        save_clicked = page.evaluate("""() => {
            const pane = document.querySelector('#pane-destination');
            if (!pane) return false;
            const btns = [...pane.querySelectorAll('button')];
            const saveBtn = btns.find(b =>
                b.textContent.trim().toLowerCase() === 'save' &&
                b.offsetParent !== null
            );
            if (saveBtn) { saveBtn.click(); return true; }
            return false;
        }""")
        if save_clicked:
            page.wait_for_timeout(2000)
            log.info("  FCS: Destination tab saved (fallback)")
            return True
    except Exception:
        pass

    log.warning("  FCS: Could not save Destination tab")
    return False


# ══════════════════════════════════════════════════════════════════════
#  4. LANE TAB — ARRIVAL DATE UPDATE
# ══════════════════════════════════════════════════════════════════════

def _navigate_to_lane_tab(page: Page) -> bool:
    """Click the Lane tab inside View Routing."""
    try:
        lane_tab = page.locator(
            "#tab-lane-1, .el-tabs__item:has-text('Lane')"
        ).first
        if lane_tab.is_visible(timeout=3000):
            lane_tab.click()
            page.wait_for_timeout(1500)
            return True
        # Fallback: role selector
        page.get_by_role("tab", name="Lane").click(timeout=5000)
        page.wait_for_timeout(1500)
        return True
    except Exception as e:
        log.warning("  FCS: Could not navigate to Lane tab: %s", e)
        return False


def _update_arrival_date(page: Page, arrival_date: str) -> bool:
    """
    Update the Arrival date on the Lane tab.

    arrival_date should be in DD/MM/YY or DD/MM/YYYY format.
    The datepicker expects YYYY-MM-DD internally.
    """
    if not arrival_date:
        return False

    # Convert DD/MM/YY(YY) → YYYY-MM-DD for the datepicker
    parts = arrival_date.split("/")
    if len(parts) != 3:
        log.warning("  FCS: Invalid arrival_date format: '%s'", arrival_date)
        return False

    day, month, year = parts
    if len(year) == 2:
        year = "20" + year
    date_iso = f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    try:
        # Click the arrival-start date field
        arrival_loc = page.get_by_label("Lane").locator("#arrival-start")
        arrival_loc.click(timeout=5000)
        page.wait_for_timeout(1000)

        # Use JS to set the value on the active datepicker input
        page.evaluate("""(dateVal) => {
            const inputs = [...document.querySelectorAll('input.el-input__inner')]
                .filter(i => i.placeholder === 'Select date' && i.offsetParent !== null);
            const inp = inputs[inputs.length - 1]; // Active datepicker input
            if (!inp) return false;

            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            setter.call(inp, dateVal);
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }""", date_iso)

        page.wait_for_timeout(500)

        # Click OK in the datepicker modal
        page.evaluate("""() => {
            const btns = [...document.querySelectorAll('button')];
            const ok = btns.find(b =>
                (b.textContent.trim() === 'OK' || b.textContent.trim() === 'Confirm') &&
                b.offsetParent !== null
            );
            if (ok) ok.click();
        }""")

        page.wait_for_timeout(1000)
        log.info("  FCS: Arrival date updated to %s (ISO: %s)", arrival_date, date_iso)
        return True

    except Exception as e:
        log.warning("  FCS: Failed to update arrival date: %s", e)
        return False


def _save_lane_tab(page: Page) -> bool:
    """Click the Save button on the Lane tab."""
    try:
        save_clicked = page.evaluate("""() => {
            const pane = document.querySelector('#pane-lane-1') ||
                         document.querySelector('[aria-labelledby*="lane"]');
            const scope = pane || document;
            const btns = [...scope.querySelectorAll('button')];
            const saveBtn = btns.find(b =>
                b.textContent.trim().toLowerCase() === 'save' &&
                b.offsetParent !== null
            );
            if (saveBtn) { saveBtn.click(); return true; }
            return false;
        }""")
        if save_clicked:
            page.wait_for_timeout(2000)
            log.info("  FCS: Lane tab saved")
            return True
    except Exception:
        pass
    log.warning("  FCS: Could not save Lane tab")
    return False


# ══════════════════════════════════════════════════════════════════════
#  MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════

def handle_fcs_post_upload(page: Page, extraction: dict) -> bool:
    """
    After uploading the Arrival Notice document for an FCS carrier,
    perform the additional Jordex updates:

      1. Carrier tab  → update vessel name (if different).
      2. Destination tab → add warehouse address.
      3. Lane tab → update arrival date.
      4. Save each tab.

    Args:
        page:       The Jordex Playwright page (already on the shipment).
        extraction: The extraction dict from extract_arrival_notice, which
                    for FCS carriers contains extra keys:
                      - vessel_name
                      - devanning_date_raw / devanning_date
                      - warehouse_name, address_line_1, postal_code, city, country

    Returns:
        True if at least one update succeeded.
    """
    any_success = False
    vessel_name = (extraction.get("vessel_name") or "").strip()
    arrival_date = extraction.get("arrival_date")
    warehouse = {
        "warehouse_name": extraction.get("warehouse_name"),
        "address_line_1":  extraction.get("address_line_1"),
        "postal_code":     extraction.get("postal_code"),
        "city":            extraction.get("city"),
        "country":         extraction.get("country"),
    }
    has_warehouse = any(v for v in warehouse.values() if v)

    if not vessel_name and not arrival_date and not has_warehouse:
        log.info("  FCS: No additional data to update in Jordex")
        return False

    # ── Open View Routing ────────────────────────────────────────────
    if not _open_view_routing(page):
        return False

    # ── 1. Carrier tab: vessel name ──────────────────────────────────
    if vessel_name:
        if _navigate_to_carrier_tab(page):
            current_vessel = _get_current_vessel_name(page)
            if current_vessel and current_vessel.upper() == vessel_name.upper():
                log.info("  FCS: Vessel name already matches: '%s'", current_vessel)
            else:
                log.info("  FCS: Vessel mismatch — current='%s', new='%s'",
                         current_vessel, vessel_name)
                if _update_vessel_name(page, vessel_name):
                    _save_carrier_tab(page)
                    any_success = True

    # ── 2. Destination tab: warehouse address ────────────────────────
    if has_warehouse:
        if _navigate_to_destination_tab(page):
            if _click_add_address_button(page):
                if _fill_address_dialog(page, warehouse):
                    _save_destination_tab(page)
                    any_success = True

    # ── 3. Lane tab: arrival date ────────────────────────────────────
    if arrival_date:
        if _navigate_to_lane_tab(page):
            if _update_arrival_date(page, arrival_date):
                _save_lane_tab(page)
                any_success = True

    return any_success