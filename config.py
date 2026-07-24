"""
config.py — Jordex-Documentation Project Config
=================================================
Central configuration for all services. Each service reads from here.
Profile paths are per-label so every service has its own isolated browser session.
"""
import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── Paths ──────────────────────────────────────────────────────────────
OUTPUT_DIR    = os.path.join(BASE_DIR, "output")
TRACKING_FILE = os.path.join(BASE_DIR, "tracking.json")
PROFILES_DIR  = os.path.join(BASE_DIR, "profiles")
LOGS_DIR      = os.path.join(BASE_DIR, "logs")

# ── Outlook credentials ────────────────────────────────────────────────
EMAIL    = "axebpo.import@jordex.com"
PASSWORD = os.environ.get("OUTLOOK_PASSWORD", "")

# ── Gemini AI ──────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-2.5-flash-lite"

# ── Jordex credentials ─────────────────────────────────────────────────
JORDEX_EMAIL     = os.environ.get("JORDEX_IMPORT_EMAIL", "axebpo@jordex.com")
JORDEX_PASSWORD  = os.environ.get("JORDEX_IMPORT_PASSWORD", "")
JORDEX_BASE_URL  = os.environ.get("JORDEX_BASE_URL", "https://jit.jordex.com")
JORDEX_OCEAN_URL = JORDEX_BASE_URL + os.environ.get("JORDEX_SHIPMENTS_PATH", "/shipments/ocean")

# ── Browser settings ───────────────────────────────────────────────────
BROWSER_HEADLESS = os.environ.get("BROWSER_HEADLESS", "false").lower() == "true"
BROWSER_ZOOM     = float(os.environ.get("BROWSER_ZOOM", "0.75"))
BROWSER_ARGS     = [
    "--disable-blink-features=AutomationControlled",
    "--start-maximized",
]

# ── Outlook URLs ───────────────────────────────────────────────────────
OUTLOOK_LOGIN_URL = "https://outlook.office.com/mail/"
OUTLOOK_MAIL_URL  = "https://outlook.office.com/mail/"

# ── Timeout config ─────────────────────────────────────────────────────
MFA_TIMEOUT      = 120_000
MFA_PUSH_WAIT    = 20_000
NAV_TIMEOUT      = 30_000
ELEMENT_TIMEOUT  = 15_000
SHORT_WAIT       = 2_000

# ── Label definitions ──────────────────────────────────────────────────
# (outlook_sidebar_label, output_subfolder, extraction_mode, service_key)
# extraction_mode: "oi" = OI from subject, "mbl" = Gemini MBL extraction
LABELS = [
    ("01.Customs docs",         "Customs_Docs",         "oi",  "customs_docs"),
    ("02.Delivery Order",       "Delivery_Order",       "mbl", "delivery_order"),
    ("03.Arrival Notice",       "Arrival_Notice",       "mbl", "arrival_notice"),
    ("04.Customer Docs",        "Customer_Docs",        "mbl", "customer_docs"),
    ("05.Invoice Carrier",      "Invoice_Carrier",      "mbl", "invoice_carrier"),
    ("06.Booking confirmation", "Booking_Confirmation", "mbl", "booking"),
]

# ── Per-label profile directories ─────────────────────────────────────
# Each service gets its own isolated browser profile (own cookies/session)
def get_outlook_profile(service_key: str) -> str:
    return os.path.join(PROFILES_DIR, "outlook", service_key)

def get_jordex_profile(service_key: str) -> str:
    return os.path.join(PROFILES_DIR, "jordex", service_key)

# ── Jordex upload mapping ──────────────────────────────────────────────
# Maps output_subfolder → (Jordex doc_type, Jordex display_name)
# Used by each service when uploading to Jordex after download
JORDEX_MAPPING = {
    "Customs_Docs":         ("Customs Clearance", None),          # per-file names via CUSTOMS_DOCS_FILE_MAP
    "Delivery_Order":       ("Container release", "DO"),
    "Arrival_Notice":       ("Carrier documents", "AN"),
    "Customer_Docs":        ("Carrier documents", None),          # per-file names via build_customer_docs_file_map
    "Invoice_Carrier":      ("Carrier documents", "Invoice carrier"),
    "Booking_Confirmation": ("Carrier documents", "Booking Confirmation"),
}

# ── Batch & cooldown ───────────────────────────────────────────────────
ROUND_ROBIN_BATCH   = 5    # emails per label per pass
LABEL_INTERVAL_SEC  = 10   # seconds between passes
