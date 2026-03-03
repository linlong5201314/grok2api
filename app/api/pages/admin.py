from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.core.logger import logger

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


def _serve_admin_page(filename: str):
    page_path = STATIC_DIR / "admin" / "pages" / filename
    if page_path.exists():
        return FileResponse(page_path)

    logger.error(f"Admin page missing from deployment bundle: {page_path}")
    return HTMLResponse(
        content=(
            "<h1>Admin page unavailable</h1>"
            "<p>Static files are missing in this deployment. "
            "Please include app/static/** in your build bundle.</p>"
        ),
        status_code=503,
    )


@router.get("/admin", include_in_schema=False)
async def admin_root():
    return RedirectResponse(url="/admin/login")


@router.get("/admin/login", include_in_schema=False)
async def admin_login():
    return _serve_admin_page("login.html")


@router.get("/admin/config", include_in_schema=False)
async def admin_config():
    return _serve_admin_page("config.html")


@router.get("/admin/cache", include_in_schema=False)
async def admin_cache():
    return _serve_admin_page("cache.html")


@router.get("/admin/token", include_in_schema=False)
async def admin_token():
    return _serve_admin_page("token.html")
