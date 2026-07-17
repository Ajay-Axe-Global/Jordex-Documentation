"""
session_manager.py — Central Registry for All 6 Services
=========================================================
Holds one instance of each service class.
All routes.py endpoints call through here.
"""
import logging

from services.arrival_notice.arrival_notice  import ArrivalNoticeService
from services.invoice_carrier.invoice_carrier import InvoiceCarrierService
from services.customs_docs.customs_docs      import CustomsDocsService
from services.delivery_order.delivery_order  import DeliveryOrderService
from services.customer_docs.customer_docs    import CustomerDocsService
from services.booking.booking                import BookingService

log = logging.getLogger("session_manager")

# ── Service registry ───────────────────────────────────────────────────
# Maps service_key → service instance
_SERVICES: dict = {}


def _init():
    global _SERVICES
    _SERVICES = {
        "arrival_notice":  ArrivalNoticeService(),
        "invoice_carrier": InvoiceCarrierService(),
        "customs_docs":    CustomsDocsService(),
        "delivery_order":  DeliveryOrderService(),
        "customer_docs":   CustomerDocsService(),
        "booking":         BookingService(),
    }
    log.info("SessionManager: all 6 services initialized (idle)")


def get_service(service_key: str):
    """Return the service instance for the given key."""
    return _SERVICES.get(service_key)


def all_services() -> dict:
    return _SERVICES


def start_service(service_key: str) -> dict:
    svc = get_service(service_key)
    if not svc:
        return {"ok": False, "message": f"Unknown service: {service_key}"}
    return svc.start()


def stop_service(service_key: str) -> dict:
    svc = get_service(service_key)
    if not svc:
        return {"ok": False, "message": f"Unknown service: {service_key}"}
    return svc.stop()


def get_all_status() -> dict:
    return {key: svc.get_status() for key, svc in _SERVICES.items()}


def get_service_status(service_key: str) -> dict:
    svc = get_service(service_key)
    if not svc:
        return {"error": f"Unknown service: {service_key}"}
    return svc.get_status()


# Initialise on import
_init()
