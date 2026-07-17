"""
extractor.py — Gemini AI Engine (Root Level, Shared by All Services)
=====================================================================
This is the SHARED engine. It provides:
  1. Gemini model initialisation (one singleton, imported by all services)
  2. classify_document(pdf_path)     → cheap doc-type router
  3. extract_oi_from_subject(subj)   → OI number regex helper
  4. save_result(result, path)       → save result dict as JSON

NOTE: Heavy extraction prompts and label-specific parsing logic live in
each service's own extractor.py, e.g.:
  services/arrival_notice/extractor.py  → ARRIVAL_NOTICE_PROMPT + parsing
  services/delivery_order/extractor.py  → DELIVERY_ORDER_PROMPT + parsing
  ... etc.

This file ONLY owns:
  - Gemini API setup
  - The cheap document-type classifier (to decide which label a PDF belongs to)
  - Shared helpers used across all services
"""

import base64, json, os, re, logging
from datetime import datetime

log = logging.getLogger("extractor")

# ── Gemini setup ───────────────────────────────────────────────────────
try:
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")
    import google.generativeai as genai
    from config import GEMINI_API_KEY, GEMINI_MODEL
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    GEMINI_AVAILABLE = bool(GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel(GEMINI_MODEL) if GEMINI_AVAILABLE else None
except ImportError:
    GEMINI_AVAILABLE = False
    gemini_model = None
    log.warning("google-generativeai not installed — Gemini unavailable")


# ══════════════════════════════════════════════════════════════════════
#  DOCUMENT TYPE ROUTER PROMPT
#  Cheap single-call classifier — runs BEFORE the heavier per-label
#  extraction prompts.
# ══════════════════════════════════════════════════════════════════════

CLASSIFY_DOC_TYPE_PROMPT = """You are a logistics document classifier.
Look at this PDF (check the title/header text on page 1) and classify it.

Return ONLY a valid JSON object (no markdown, no backticks, no extra text):
{"doc_type": "delivery_order" or "arrival_notice" or "dms_tax" or "dms_imp_ttw" or "invoice_carrier" or "unknown"}

Classification rules (check in this order):
1. "invoice_carrier" — document is an invoice from a carrier or shipping line. It typically has
   prominent text like "INVOICE", lists charges/fees, or has an "INVOICE NO." (Do NOT classify
   these as delivery orders).
2. "delivery_order" — the document's title/header says "DELIVERY ORDER", "Customer Release",
   "Container Release Notification", "Release Notification", "LAAT VOLGEN", or similar — i.e.
   it authorises pickup of a container and lists a pickup/release location and/or empty return
   location.
3. "arrival_notice" — the document's title/header says "ARRIVAL NOTICE" and its main purpose is
   announcing an ETA / arrival at the discharge port, NOT authorising container pickup.
4. "dms_tax" — contains ANY of: "Tax Status", "DMSCL", "Definitief", "Aangifte", tax assessment,
   Dutch customs tax declaration.
5. "dms_imp_ttw" — contains ANY of: "ttw", "TTW", "European Community",
   "IMPORT ACCOMPANYING DOCUMENT", "DECLARATION TYPE: IM", "Eurasian", import declaration type T1.
6. "unknown" — none of the above match confidently."""


def classify_document(pdf_path: str) -> str:
    """
    Classify a PDF as one of: delivery_order, arrival_notice, dms_tax,
    dms_imp_ttw, invoice_carrier, unknown. Cheap single call.
    """
    if not GEMINI_AVAILABLE:
        log.warning("classify_document: Gemini not available, returning 'unknown'")
        return "unknown"

    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        resp = gemini_model.generate_content(
            [
                {"mime_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()},
                CLASSIFY_DOC_TYPE_PROMPT,
            ],
            generation_config={"temperature": 0.0, "max_output_tokens": 50},
        )

        raw = resp.text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        parsed = json.loads(raw)
        doc_type = parsed.get("doc_type", "unknown")
        log.info("  classify_document(%s): %s", os.path.basename(pdf_path), doc_type)
        return doc_type

    except json.JSONDecodeError as e:
        log.warning("classify_document JSON parse failed: %s", e)
        return "unknown"
    except Exception as e:
        log.warning("classify_document failed: %s", e)
        return "unknown"


# ══════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════

def extract_oi_from_subject(subject: str) -> str | None:
    """Extract OI number from email subject. Used by Customs Docs label."""
    matches = re.findall(r'(OI\d{4,})', subject)
    return matches[-1] if matches else None


def save_result(result: dict, folder_path: str, filename: str = None):
    """
    Save result dict as JSON in folder_path.
    Filename defaults to doc_type-based name unless caller specifies one.
    """
    os.makedirs(folder_path, exist_ok=True)
    if not filename:
        filename = {
            "delivery_order":  "result.json",
            "arrival_notice":  "arrival_notice.json",
            "dms_tax":         "customs_result.json",
            "dms_imp_ttw":     "customs_result.json",
            "invoice_carrier": "result.json",
            "customer_docs":   "result.json",
            "booking":         "result.json",
        }.get(result.get("doc_type"), "result.json")
    with open(os.path.join(folder_path, filename), "w") as f:
        json.dump(result, f, indent=2, default=str)
