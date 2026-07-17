"""
outlook/session.py — Per-Label Outlook Session Manager
=======================================================
Each service creates its own OutlookSession with a UNIQUE profile_dir.
This gives every label complete isolation:
  - Own cookies / login session
  - Own browser profile folder
  - MFA done ONCE per label, then reused forever

Usage (inside each service):
    from outlook.session import OutlookSession
    session = OutlookSession(service_key="arrival_notice", pw=pw_instance)
    page = session.start()   # logs in or reuses saved session
    ...
    session.close()
"""
import os, time, json, logging
from pathlib import Path
from playwright.sync_api import sync_playwright, BrowserContext, Page, TimeoutError as PWTimeout
from config import (
    EMAIL, PASSWORD,
    OUTLOOK_LOGIN_URL, OUTLOOK_MAIL_URL,
    MFA_TIMEOUT, MFA_PUSH_WAIT, NAV_TIMEOUT, ELEMENT_TIMEOUT, SHORT_WAIT,
    BROWSER_ARGS, BROWSER_HEADLESS,
    get_outlook_profile,
)

log = logging.getLogger("outlook.session")

POLL_INTERVAL_MS = 1000

LOGGED_IN_SELECTORS = [
    "button[aria-label*='New mail' i]",
    "button[aria-label*='New message' i]",
    "button:has-text('New mail')",
    "button:has-text('New message')",
    "div[role='main'] [role='listbox']",
    "#MailList",
]


class LoginError(RuntimeError):
    pass


class OutlookSession:
    """
    Manages a persistent Chromium context for Outlook.
    Each instance uses its own profile_dir so sessions are fully isolated.
    """

    def __init__(self, service_key: str, headless: bool = BROWSER_HEADLESS, pw=None):
        self.service_key   = service_key
        self.headless      = headless
        self._pw           = pw
        self._owns_pw      = pw is None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.profile_dir   = get_outlook_profile(service_key)
        self._cookie_file  = os.path.join(self.profile_dir, "cookies_backup.json")
        self._debug_shot   = os.path.join(self.profile_dir, "login_failure.png")

    # ── Public API ──────────────────────────────────────────────────────

    def start(self) -> Page:
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        if self._owns_pw and self._pw is None:
            self._pw = sync_playwright().start()

        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            channel="chrome",
            headless=self.headless,
            args=BROWSER_ARGS,
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
            ignore_https_errors=True,
        )

        self._restore_cookies()
        self._cleanup_tabs()

        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(ELEMENT_TIMEOUT)

        try:
            self._page.goto(OUTLOOK_MAIL_URL, wait_until="commit", timeout=NAV_TIMEOUT)
        except Exception as e:
            log.warning(f"[{self.service_key}] Initial navigation raised: {e}")

        self._page.wait_for_timeout(1500)

        if self._is_logged_in():
            log.info(f"[{self.service_key}] Session valid - already logged in")
        else:
            log.info(f"[{self.service_key}] Not logged in - running login flow")
            self._run_login_loop()

        self._confirm_logged_in_or_raise()
        self._backup_cookies()
        log.info(f"[{self.service_key}] Outlook ready: {self._page.url}")
        return self._page

    def close(self):
        if self._context:
            try:
                self._backup_cookies()
            except Exception:
                pass
            try:
                self._context.close()
            except Exception:
                pass
        if self._pw and self._owns_pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        log.info(f"[{self.service_key}] Browser closed")

    @property
    def page(self) -> Page:
        return self._page

    # ── Logged-in detection ────────────────────────────────────────────

    def _is_logged_in(self, timeout_ms: int = 3000) -> bool:
        url = self._page.url
        if "login.microsoftonline.com" in url:
            return False
        for sel in LOGGED_IN_SELECTORS:
            try:
                if self._page.locator(sel).first.is_visible(timeout=timeout_ms):
                    return True
            except Exception:
                continue
        return False

    def _confirm_logged_in_or_raise(self):
        deadline = time.monotonic() + (NAV_TIMEOUT / 1000)
        while time.monotonic() < deadline:
            if self._is_logged_in(timeout_ms=2000):
                self._page.wait_for_timeout(SHORT_WAIT)
                return
            self._page.wait_for_timeout(1000)
        self._dump_debug("final-confirm-failed")
        raise LoginError(
            f"[{self.service_key}] Could not confirm Outlook mail UI. URL: {self._page.url}"
        )

    # ── Login loop ─────────────────────────────────────────────────────

    def _run_login_loop(self):
        try:
            self._page.goto(OUTLOOK_LOGIN_URL, wait_until="commit", timeout=NAV_TIMEOUT)
        except Exception as e:
            log.warning(f"[{self.service_key}] goto login raised: {e}")
        self._page.wait_for_timeout(SHORT_WAIT)

        deadline = time.monotonic() + (MFA_TIMEOUT / 1000)
        mfa_push_deadline = None
        last_state = None

        while time.monotonic() < deadline:
            if self._is_logged_in(timeout_ms=1500):
                log.info(f"[{self.service_key}] Reached Outlook mail UI")
                return

            state = self._detect_screen_state()
            if state != last_state:
                log.info(f"[{self.service_key}] Screen state: {state}")
                last_state = state

            if state == "email_input":
                self._fill_email()
            elif state == "account_tile":
                self._click_account_tile()
            elif state == "stay_signed_in":
                self._handle_stay_signed_in()
            elif state == "mfa_number":
                if mfa_push_deadline is None:
                    mfa_push_deadline = time.monotonic() + (MFA_PUSH_WAIT / 1000)
                    num = self._get_mfa_number()
                    if num:
                        log.info(f"[{self.service_key}] >>> MFA CODE: [ {num} ] <<<")
                    else:
                        log.info(f"[{self.service_key}] >>> Approve sign-in on phone <<<")
                if time.monotonic() > mfa_push_deadline:
                    log.info(f"[{self.service_key}] Push timeout - trying password link")
                    self._click_use_password_link()
                    mfa_push_deadline = None
            elif state == "use_password_link":
                self._click_use_password_link()
            elif state == "password_field":
                self._enter_password_and_submit()

            self._page.wait_for_timeout(POLL_INTERVAL_MS)

        self._dump_debug("login-loop-timeout")
        raise LoginError(
            f"[{self.service_key}] Login did not complete within {MFA_TIMEOUT/1000:.0f}s. "
            f"Last state: {last_state}, URL: {self._page.url}"
        )

    def _detect_screen_state(self) -> str:
        checks = [
            ("input[type='submit'][value='Yes']", "stay_signed_in"),
        ]
        for sel, state in checks:
            try:
                if self._page.locator(sel).first.is_visible(timeout=800):
                    return state
            except Exception:
                pass

        try:
            if self._page.get_by_text("Approve sign in request", exact=True).first.is_visible(timeout=800):
                return "mfa_number"
        except Exception:
            pass
        try:
            if self._page.locator("div#displaySign").first.is_visible(timeout=800):
                return "mfa_number"
        except Exception:
            pass

        try:
            real_pwd = self._page.locator(
                "input[type='password'][name='passwd']:not([aria-hidden='true']):not(.moveOffScreen)"
            ).first
            if real_pwd.is_visible(timeout=800):
                return "password_field"
        except Exception:
            pass

        try:
            if self._page.get_by_role("link", name="Use your password instead").first.is_visible(timeout=800):
                return "use_password_link"
        except Exception:
            pass

        try:
            email_box = self._page.locator(
                "input[type='email']:not([aria-hidden='true']), input[name='loginfmt']:not([aria-hidden='true'])"
            ).first
            if email_box.is_visible(timeout=800):
                return "email_input"
        except Exception:
            pass

        try:
            if self._page.locator(f"div[role='button']:has-text('{EMAIL}')").first.is_visible(timeout=800):
                return "account_tile"
        except Exception:
            pass

        return "unknown"

    # ── Screen action helpers ──────────────────────────────────────────

    def _fill_email(self):
        try:
            box = self._page.locator("input[type='email'], input[name='loginfmt']").first
            box.click()
            box.fill(EMAIL)
            box.press("Enter")
        except Exception as e:
            log.warning(f"[{self.service_key}] fill_email: {e}")

    def _click_account_tile(self):
        try:
            self._page.locator(f"div[role='button']:has-text('{EMAIL}')").first.click()
        except Exception as e:
            log.warning(f"[{self.service_key}] account_tile: {e}")

    def _get_mfa_number(self) -> str | None:
        try:
            el = self._page.locator("div#displaySign").first
            if el.is_visible(timeout=1500):
                return el.inner_text(timeout=1500).strip()
        except Exception:
            pass
        return None

    def _click_use_password_link(self):
        try:
            self._page.get_by_role("link", name="Use your password instead").first.click(timeout=3000)
        except Exception as e:
            log.warning(f"[{self.service_key}] password_link: {e}")

    def _enter_password_and_submit(self):
        try:
            pwd = self._page.locator(
                "input[type='password'][name='passwd']:not([aria-hidden='true']):not(.moveOffScreen)"
            ).first
            pwd.click()
            pwd.fill(PASSWORD)
            self._page.wait_for_timeout(300)
            self._page.locator("input[type='submit'][value='Sign in'], button:has-text('Sign in')").first.click()
        except Exception as e:
            log.warning(f"[{self.service_key}] enter_password: {e}")

    def _handle_stay_signed_in(self):
        try:
            btn = self._page.locator("input[type='submit'][value='Yes']").first
            if btn.is_visible(timeout=1500):
                btn.click()
                self._page.wait_for_timeout(SHORT_WAIT)
        except Exception:
            pass

    # ── Cookie management ──────────────────────────────────────────────

    def _backup_cookies(self):
        try:
            cookies = self._context.cookies()
            with open(self._cookie_file, "w") as f:
                json.dump(cookies, f, indent=2)
        except Exception as e:
            log.warning(f"[{self.service_key}] Cookie backup: {e}")

    def _restore_cookies(self):
        if not os.path.exists(self._cookie_file):
            return
        try:
            with open(self._cookie_file) as f:
                cookies = json.load(f)
            valid = [c for c in cookies
                     if any(d in c.get("domain", "") for d in ["microsoft", "outlook", "office", "live"])]
            if valid:
                self._context.add_cookies(valid)
        except Exception as e:
            log.warning(f"[{self.service_key}] Cookie restore: {e}")

    def _cleanup_tabs(self):
        pages = self._context.pages
        if len(pages) > 1:
            for p in pages[1:]:
                try:
                    p.close()
                except Exception:
                    pass

    def _dump_debug(self, tag: str):
        try:
            path = self._debug_shot.replace(".png", f"_{tag}.png")
            self._page.screenshot(path=path, full_page=True)
            log.error(f"[{self.service_key}] Debug screenshot: {path}")
        except Exception as e:
            log.error(f"[{self.service_key}] Screenshot failed: {e}")
