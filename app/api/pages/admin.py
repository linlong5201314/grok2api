from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.core.logger import logger

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


def _admin_fallback_page(title: str):
    return HTMLResponse(
        content=(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title>"
            "<style>body{font-family:Arial,sans-serif;max-width:760px;"
            "margin:40px auto;padding:0 16px;line-height:1.6}"
            "code{background:#f4f4f4;padding:2px 6px;border-radius:4px}"
            "a{color:#0a58ca;text-decoration:none}a:hover{text-decoration:underline}"
            "</style></head><body>"
            f"<h1>{title}</h1>"
            "<p>Admin static pages are unavailable in this deployment package.</p>"
            "<p>Use API endpoints directly for management:</p>"
            "<ul>"
            "<li><code>GET /v1/admin/config</code></li>"
            "<li><code>GET /v1/admin/tokens</code></li>"
            "<li><a href='/docs'>/docs</a></li>"
            "</ul>"
            "<p>Deployment tip: include <code>app/**</code> in Vercel function files.</p>"
            "</body></html>"
        ),
        status_code=200,
    )


def _serve_admin_page(filename: str, title: str):
    page_path = STATIC_DIR / "admin" / "pages" / filename
    if page_path.is_file():
        return FileResponse(page_path)

    logger.error(f"Admin page missing from deployment bundle: {page_path}")
    return _admin_fallback_page(title)


@router.get("/admin", include_in_schema=False)
async def admin_root():
    return RedirectResponse(url="/admin/login")


@router.get("/admin/login", include_in_schema=False)
async def admin_login():
    return _serve_admin_page("login.html", "Admin Login")


@router.get("/admin/config", include_in_schema=False)
async def admin_config():
    return _serve_admin_page("config.html", "Admin Config")


@router.get("/admin/cache", include_in_schema=False)
async def admin_cache():
    return _serve_admin_page("cache.html", "Admin Cache")


@router.get("/admin/token", include_in_schema=False)
async def admin_token():
    return _serve_admin_page("token.html", "Admin Token")
