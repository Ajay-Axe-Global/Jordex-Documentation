"""
custom_docs.py — Customs Docs Classifier
=========================================
Handles the "01.Customs docs" Outlook label.

Emails under this label contain Dutch customs clearance PDFs.
There are exactly two possible document types:
  - dms_tax      : Dutch tax assessment (Tax Status / DMSCL / Definitief / Aangifte)
  - dms_imp_ttw  : European Community import declaration (TTW / IMPORT ACCOMPANYING DOCUMENT)

The FOLDER NAME always comes from the OI number in the email subject line
(extracted by extract_oi_from_subject() in extractor.py — no Gemini needed for that).

This module's only job: given a PDF, return which of the two customs types it is.
That type is then used to pick the correct Jordex doc_type / display_name for upload.

Called by utils.py process_label_batch() when cat == "Customs_Docs".
"""

import base64, json, os, re, logging

log = logging.getLogger("custom_docs")


# ══════════════════════════════════════════════════════════════════════
#  JORDEX UPLOAD MAP  (used by run.py)
# ══════════════════════════════════════════════════════════════════════

# Maps classified doc_type → (Jordex doc_type, Jordex display_name)
# passed directly to upload_attachments()
CUSTOMS_DOC_UPLOAD_MAP = {
    "dms_tax":     ("Customs Clearance", "DMS Tax"),
    "dms_imp_ttw": ("Customs Clearance", "DMS IMP TTW"),
    "unknown":     ("Customs Clearance", "Customs Document"),  # safe fallback
}


# ══════════════════════════════════════════════════════════════════════
#  CLASSIFICATION PROMPT
# ══════════════════════════════════════════════════════════════════════

CLASSIFY_CUSTOMS_PROMPT = """You are a customs document classifier. Examine this PDF and determine its type.

Return ONLY a valid JSON object (no markdown, no backticks, no extra text):
{
  "doc_type": "dms_tax" or "dms_imp_ttw" or "unknown",
  "status": "extracted Status (e.g. Definitief), or null",
  "amount_verschuldigd": "extracted amount next to Verschuldigd (e.g. 14.034,95), or null"
}

Classification rules:
- Return "dms_tax" if the document contains ANY of:
    "Tax Status", "DMSCL", "Definitief", "Aangifte",
    tax assessment, Dutch customs tax declaration.

- Return "dms_imp_ttw" if the document contains ANY of:
    "ttw", "TTW", "European Community",
    "IMPORT ACCOMPANYING DOCUMENT", "DECLARATION TYPE: IM",
    "Eurasian", import declaration type T1.

- Return "unknown" if neither set of keywords is found."""


# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════

def _keyword_fallback(pdf_path: str) -> str:
    """
    Pure-text keyword scan as fallback when Gemini is unavailable or fails.
    Returns "dms_tax", "dms_imp_ttw", or "unknown".
    """
    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        pass

    if not text:
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(pdf_path)
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
        except Exception:
            pass

    if not text:
        return "unknown"

    text_upper = text.upper()

    TAX_KEYWORDS = ["TAX STATUS", "DMSCL", "DEFINITIEF", "AANGIFTE"]
    TTW_KEYWORDS = ["TTW", "EUROPEAN COMMUNITY", "IMPORT ACCOMPANYING DOCUMENT",
                    "DECLARATION TYPE: IM", "EURASIAN"]

    if any(kw in text_upper for kw in TAX_KEYWORDS):
        return "dms_tax"
    if any(kw in text_upper for kw in TTW_KEYWORDS):
        return "dms_imp_ttw"
    return "unknown"


# ══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

def classify_customs_doc(pdf_path: str, gemini_model=None) -> dict:
    """
    Classify a Customs Docs PDF as dms_tax, dms_imp_ttw, or unknown.

    Returns:
      {
        "doc_type":      "dms_tax" | "dms_imp_ttw" | "unknown",
        "jordex_upload": (doc_type_str, display_name_str),
        "source_file":   "filename.pdf",
        "flag":          None | "needs_manual_review",
      }

    The folder_name / OI reference is NOT determined here —
    it comes from the email subject via extract_oi_from_subject().
    """
    result = {
        "doc_type": "unknown",
        "jordex_upload": CUSTOMS_DOC_UPLOAD_MAP["unknown"],
        "source_file": os.path.basename(pdf_path),
        "flag": None,
    }

    # ── Gemini path ──────────────────────────────────────────────────
    if gemini_model is not None:
        try:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()

            resp = gemini_model.generate_content(
                [
                    {"mime_type": "application/pdf",
                     "data": base64.b64encode(pdf_bytes).decode()},
                    CLASSIFY_CUSTOMS_PROMPT,
                ],
                generation_config={"temperature": 0.0, "max_output_tokens": 50},
            )

            raw = resp.text.strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            parsed = json.loads(raw)
            doc_type = parsed.get("doc_type", "unknown")
            log.info("  Customs Gemini: %s → %s", os.path.basename(pdf_path), doc_type)

        except json.JSONDecodeError as e:
            log.warning("  Customs JSON parse failed: %s — keyword fallback", e)
            doc_type = _keyword_fallback(pdf_path)
        except Exception as e:
            log.warning("  Customs Gemini failed: %s — keyword fallback", e)
            doc_type = _keyword_fallback(pdf_path)

    # ── No Gemini: keyword fallback ──────────────────────────────────
    else:
        log.warning("  Gemini not available — keyword fallback for %s", os.path.basename(pdf_path))
        doc_type = _keyword_fallback(pdf_path)

    # Guard: only accept known types
    if doc_type not in CUSTOMS_DOC_UPLOAD_MAP:
        doc_type = "unknown"

    result["doc_type"] = doc_type
    result["jordex_upload"] = CUSTOMS_DOC_UPLOAD_MAP[doc_type]

    if doc_type == "unknown":
        result["flag"] = "needs_manual_review"

    return result