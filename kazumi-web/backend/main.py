import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from proxy_routes import close_client, router as proxy_router
from sniff_routes import router as sniff_router

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIST = os.environ.get("KAZUMI_WEB_DIST", os.path.join(BACKEND_DIR, "web"))

app = FastAPI(title="Kazumi Web Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(proxy_router)
app.include_router(sniff_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if os.path.isdir(WEB_DIST):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(WEB_DIST, "assets")),
        name="assets",
    )

    @app.get("/{path:path}")
    async def spa(path: str):
        if not path and os.path.isfile(os.path.join(WEB_DIST, "index.html")):
            return FileResponse(os.path.join(WEB_DIST, "index.html"))
        full = os.path.normpath(os.path.join(WEB_DIST, path))
        if full.startswith(WEB_DIST) and os.path.isfile(full):
            return FileResponse(full)
        index = os.path.join(WEB_DIST, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        return FileResponse(os.devnull)


@app.on_event("shutdown")
async def on_shutdown():
    await close_client()


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("KAZUMI_HOST", "0.0.0.0")
    port = int(os.environ.get("KAZUMI_PORT", "9090"))
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        log_level="info",
        ws_ping_interval=None,
    )
