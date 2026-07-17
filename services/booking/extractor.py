"""
services/booking/extractor.py — Booking Confirmation Extractor
===============================================================
Booking confirmations don't require Gemini extraction.
The folder name comes from the email subject.
This module exists for consistency but has no Gemini prompt.
"""
import os, re, logging
from datetime import datetime

log = logging.getLogger("booking.extractor")


def extract_booking(subject: str, pdf_path: str = None) -> dict:
    """
    For booking confirmations, the folder name comes from the subject.
    Returns minimal extraction dict for tracking purposes.
    """
    return {
        "doc_type":     "booking",
        "subject":      subject,
        "extracted_at": datetime.now().isoformat(),
        "flag":         None,
    }
