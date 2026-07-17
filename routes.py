"""
routes.py — FastAPI REST Endpoints
====================================
One endpoint per service for start/stop/status.
The UI dashboard calls these endpoints when the user toggles a service.

Endpoints:
  POST /api/services/{service_key}/start   → start service
  POST /api/services/{service_key}/stop    → stop service
  GET  /api/services/status               → all 6 services status
  GET  /api/services/{service_key}/status → single service status
  GET  /api/tracker                       → full tracking.json data
  GET  /api/output/{label}                → list output files for a label
"""

import json, os, logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, FileResponse

import session_manager
from config import OUTPUT_DIR, TRACKING_FILE, LABELS

log    = logging.getLogger("routes")
router = APIRouter()

VALID_SERVICES = {"arrival_notice", "invoice_carrier", "customs_docs",
                  "delivery_order", "customer_docs", "booking"}


# ── Service control endpoints ──────────────────────────────────────────

@router.post("/api/services/{service_key}/start")
async def start_service(service_key: str):
    if service_key not in VALID_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_key}")
    result = session_manager.start_service(service_key)
    return JSONResponse(result)


@router.post("/api/services/{service_key}/stop")
async def stop_service(service_key: str):
    if service_key not in VALID_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_key}")
    result = session_manager.stop_service(service_key)
    return JSONResponse(result)


@router.get("/api/services/status")
async def all_status():
    return JSONResponse(session_manager.get_all_status())


@router.get("/api/services/{service_key}/status")
async def service_status(service_key: str):
    if service_key not in VALID_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service_key}")
    return JSONResponse(session_manager.get_service_status(service_key))


# ── Data endpoints ─────────────────────────────────────────────────────

@router.get("/api/tracker")
async def get_tracker():
    if not os.path.exists(TRACKING_FILE):
        return JSONResponse({"labels": [], "tracking": {}})
    try:
        with open(TRACKING_FILE) as f:
            tracking = json.load(f)
    except json.JSONDecodeError:
        tracking = {}

    labels_info = []
    for label_text, cat, mode, svc in LABELS:
        entries = tracking.get(cat, {})
        labels_info.append({
            "label":       label_text,
            "category":    cat,
            "service_key": svc,
            "total":       len(entries),
            "downloaded":  sum(1 for v in entries.values() if v["status"] == "downloaded"),
            "uploaded":    sum(1 for v in entries.values() if v.get("status") == "uploaded"),
            "no_attachment": sum(1 for v in entries.values() if v["status"] == "no_attachment"),
            "failed":      sum(1 for v in entries.values() if v["status"] == "failed"),
            "total_files": sum(len(v.get("files", [])) for v in entries.values()),
        })

    # Recent activity
    recent = []
    for cat, entries in tracking.items():
        for cid, info in entries.items():
            recent.append({
                "category": cat,
                "subject":  info.get("subject", ""),
                "folder":   info.get("folder_name", ""),
                "mbl":      info.get("mbl", ""),
                "files":    info.get("files", []),
                "status":   info.get("status", ""),
                "time":     info.get("processed_at", ""),
            })
    recent.sort(key=lambda x: x["time"], reverse=True)

    return JSONResponse({"labels": labels_info, "recent": recent[:30], "tracking": tracking})


@router.get("/api/output")
@router.get("/api/output/")
async def list_output_root():
    entries = []
    if os.path.exists(OUTPUT_DIR):
        for name in os.listdir(OUTPUT_DIR):
            fp     = os.path.join(OUTPUT_DIR, name)
            is_dir = os.path.isdir(fp)
            entries.append({"name": name, "is_dir": is_dir, "path": name})
    return JSONResponse({"entries": entries, "path": ""})


@router.get("/api/output/{subpath:path}")
async def list_output(subpath: str):
    target = os.path.normpath(os.path.join(OUTPUT_DIR, subpath))
    if not target.startswith(os.path.normpath(OUTPUT_DIR)):
        raise HTTPException(status_code=403)

    if not os.path.exists(target):
        return JSONResponse({"entries": [], "path": subpath})

    if os.path.isfile(target):
        return FileResponse(target)

    entries = []
    for name in os.listdir(target):
        fp     = os.path.join(target, name)
        is_dir = os.path.isdir(fp)
        size   = os.path.getsize(fp) if not is_dir else 0
        mtime  = os.path.getmtime(fp)
        entries.append({
            "name":   name,
            "is_dir": is_dir,
            "size":   size,
            "mtime":  mtime,
            "path":   f"{subpath}/{name}" if subpath else name,
        })
    entries.sort(key=lambda x: (not x["is_dir"], -x["mtime"]))
    return JSONResponse({"entries": entries, "path": subpath})


@router.get("/files/{filepath:path}")
async def serve_file(filepath: str):
    target = os.path.normpath(os.path.join(OUTPUT_DIR, filepath))
    if not target.startswith(os.path.normpath(OUTPUT_DIR)):
        raise HTTPException(status_code=403)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404)
    return FileResponse(target)
