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
  GET  /api/tracker                       → paginated/searchable tracking.json data
  GET  /api/logs/stats                    → per-label log level counts for a day
  GET  /api/logs/raw                      → raw log lines for a day, optional service filter
  GET  /api/output/{label}                → list output files for a label
"""

import json, os, re, logging
from typing import Optional
from datetime import date as date_cls
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse

import session_manager
from config import OUTPUT_DIR, TRACKING_FILE, LABELS, LOGS_DIR

log    = logging.getLogger("routes")
router = APIRouter()

VALID_SERVICES = {"arrival_notice", "invoice_carrier", "customs_docs",
                  "delivery_order", "customer_docs", "booking"}

# ── Log parsing (backs the Statistics tab) ─────────────────────────────
# Format written by main.py: "HH:MM:SS [LEVEL] [service] message"
# The "[service]" tag comes from ServiceTagFilter (thread-based), so lines
# logged by shared helpers (jordex/, outlook/, extractor.py) are correctly
# attributed to whichever label's thread called them, not lumped into "other".
LOG_LINE_RE       = re.compile(r"^(\d{2}:\d{2}:\d{2}) \[(\w+)\] \[(\w+)\] (.*)$")
SELF_TAG_PREFIX_RE = re.compile(r"^\[\w+\]\s*")


def _log_file_for_date(date_str: str) -> str:
    if date_str == date_cls.today().isoformat():
        return os.path.join(LOGS_DIR, "service.log")
    return os.path.join(LOGS_DIR, f"service.log.{date_str}.log")


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
async def get_tracker(
    search: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    if not os.path.exists(TRACKING_FILE):
        return JSONResponse({"labels": [], "recent": [], "total": 0, "page": page,
                              "page_size": page_size, "tracking": {}})
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

    # Recent activity — flatten across the FULL tracking.json, not just what's on screen
    recent = []
    for cat, entries in tracking.items():
        for cid, info in entries.items():
            recent.append({
                "category":     cat,
                "subject":      info.get("subject", ""),
                "folder":       info.get("folder_name", ""),
                "mbl":          info.get("mbl", ""),
                "carrier_code": info.get("carrier_code", ""),
                "files":        info.get("files", []),
                "status":       info.get("status", ""),
                "time":         info.get("processed_at", ""),
            })
    recent.sort(key=lambda x: x["time"], reverse=True)

    if category:
        recent = [r for r in recent if r["category"] == category]

    if search:
        s = search.lower()
        recent = [r for r in recent if
                   s in (r["mbl"] or "").lower()
                   or s in (r["folder"] or "").lower()
                   or s in (r["subject"] or "").lower()
                   or s in (r["carrier_code"] or "").lower()]

    total = len(recent)
    start = (page - 1) * page_size
    page_items = recent[start:start + page_size]

    return JSONResponse({
        "labels": labels_info, "recent": page_items, "total": total,
        "page": page, "page_size": page_size, "tracking": tracking,
    })


@router.get("/api/logs/stats")
async def get_log_stats(date: Optional[str] = Query(None)):
    """Per-label INFO/WARNING/ERROR counts + recent issues for a given day (default: today)."""
    date_str = date or date_cls.today().isoformat()
    path = _log_file_for_date(date_str)

    service_keys = {svc for _, _, _, svc in LABELS}
    services = {svc: {"INFO": 0, "WARNING": 0, "ERROR": 0, "recent_issues": []} for svc in service_keys}
    services["other"] = {"INFO": 0, "WARNING": 0, "ERROR": 0, "recent_issues": []}

    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = LOG_LINE_RE.match(line.strip())
                if not m:
                    continue
                ts, level, svc, msg = m.groups()
                if level not in ("INFO", "WARNING", "ERROR"):
                    continue
                if svc not in services:
                    svc = "other"
                services[svc][level] += 1
                if level in ("WARNING", "ERROR"):
                    # Strip a redundant manual "[service_key]" prefix some callers
                    # already embed in the message text — the tag column covers it.
                    clean_msg = SELF_TAG_PREFIX_RE.sub("", msg)
                    services[svc]["recent_issues"].append({"time": ts, "level": level, "message": clean_msg})
                    services[svc]["recent_issues"] = services[svc]["recent_issues"][-20:]

    return JSONResponse({"date": date_str, "services": services})


@router.get("/api/logs/raw")
async def get_log_raw(
    date: Optional[str] = Query(None),
    service: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=5000),
):
    """Raw log lines for a given day, optionally filtered to one service/label."""
    date_str = date or date_cls.today().isoformat()
    path = _log_file_for_date(date_str)

    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                line = raw_line.rstrip("\n")
                if service:
                    m = LOG_LINE_RE.match(line)
                    if not m or m.group(3) != service:
                        continue
                lines.append(line)

    return JSONResponse({"date": date_str, "service": service, "lines": lines[-limit:]})


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
