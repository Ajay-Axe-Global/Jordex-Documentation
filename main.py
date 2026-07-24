"""
main.py — FastAPI Application Entry Point
==========================================
Start with:  python main.py
Or:          uvicorn main:app --host 0.0.0.0 --port 5050 --reload

Dashboard UI: http://localhost:5050
"""

import logging, os, re, threading
from logging.handlers import TimedRotatingFileHandler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from routes import router
from config import BASE_DIR, LOGS_DIR

os.makedirs(LOGS_DIR, exist_ok=True)

_THREAD_TAG_RE = re.compile(r"^svc-(\w+)$")


class ServiceTagFilter(logging.Filter):
    """Tags every record with the service_key of the thread that emitted it.

    Each service runs its own Playwright loop on a thread named "svc-{SERVICE_KEY}"
    (see services/*/*.py), so log calls made from shared helpers (jordex/, outlook/,
    extractor.py) — which never self-tag with "[service_key]" — still get attributed
    to the correct label instead of falling into an "other" bucket.
    """
    def filter(self, record):
        m = _THREAD_TAG_RE.match(threading.current_thread().name)
        record.service = m.group(1) if m else "system"
        return True


_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] [%(service)s] %(message)s",
    datefmt="%H:%M:%S",
)
_tag_filter = ServiceTagFilter()

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)
_console_handler.addFilter(_tag_filter)

# One file per day, keep 30 days of history — backs the Statistics tab.
_file_handler = TimedRotatingFileHandler(
    os.path.join(LOGS_DIR, "service.log"),
    when="midnight",
    backupCount=30,
    encoding="utf-8",
)
_file_handler.suffix = "%Y-%m-%d.log"
_file_handler.setFormatter(_formatter)
_file_handler.addFilter(_tag_filter)

logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
log = logging.getLogger("main")

app = FastAPI(
    title="Jordex Documentation API",
    description="Transport import automation — 6 independent label services",
    version="2.0.0",
)

# CORS (allow localhost UI to call the API)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include all routes
app.include_router(router)


# ── Serve frontend ─────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(BASE_DIR, "frontend", "index.html")
    if os.path.exists(html_path):
        with open(html_path, encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Jordex Documentation</h1><p>Frontend not found.</p>")


@app.get("/favicon.ico")
async def favicon():
    return HTMLResponse("", status_code=204)


# ── Run ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    log.info("Starting Jordex Documentation server at http://localhost:5050")
    uvicorn.run("main:app", host="0.0.0.0", port=5050, reload=False, log_level="info", access_log=False)
