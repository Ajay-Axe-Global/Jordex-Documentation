"""
main.py — FastAPI Application Entry Point
==========================================
Start with:  python main.py
Or:          uvicorn main:app --host 0.0.0.0 --port 5050 --reload

Dashboard UI: http://localhost:5050
"""

import logging, os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from routes import router
from config import BASE_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
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
    uvicorn.run("main:app", host="0.0.0.0", port=5050, reload=False, log_level="info")
