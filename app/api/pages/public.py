from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.core.auth import is_public_enabled
from app.core.logger import logger

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


def _serve_public_page(filename: str):
    page_path = STATIC_DIR / "public" / "pages" / filename
    if page_path.exists():
        return FileResponse(page_path)

    logger.error(f"Public page missing from deployment bundle: {page_path}")
    return HTMLResponse(
        content=(
            "<h1>Public page unavailable</h1>"
            "<p>Static files are missing in this deployment. "
            "Please include app/static/** in your build bundle.</p>"
        ),
        status_code=503,
    )


@router.get("/", include_in_schema=False)
async def root():
    if is_public_enabled():
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/admin/login")


@router.get("/login", include_in_schema=False)
async def public_login():
    if not is_public_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_public_page("login.html")


@router.get("/imagine", include_in_schema=False)
async def public_imagine():
    if not is_public_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_public_page("imagine.html")


@router.get("/voice", include_in_schema=False)
async def public_voice():
    if not is_public_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_public_page("voice.html")


@router.get("/video", include_in_schema=False)
async def public_video():
    if not is_public_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_public_page("video.html")


@router.get("/chat", include_in_schema=False)
async def public_chat():
    if not is_public_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_public_page("chat.html")
