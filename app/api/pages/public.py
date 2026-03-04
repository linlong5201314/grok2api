from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.auth import is_public_enabled
from app.core.logger import logger

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


def _public_fallback_page(title: str):
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
            "<p>Public static pages are unavailable in this deployment package.</p>"
            "<p>The API service is still available. Try:</p>"
            "<ul>"
            "<li><a href='/v1/models'>/v1/models</a></li>"
            "<li><a href='/docs'>/docs</a></li>"
            "<li><a href='/admin/login'>/admin/login</a></li>"
            "</ul>"
            "<p>Deployment tip: include <code>app/**</code> in Vercel function files.</p>"
            "</body></html>"
        ),
        status_code=200,
    )


def _serve_public_page(filename: str, title: str):
    page_path = STATIC_DIR / "public" / "pages" / filename
    try:
        content = page_path.read_text(encoding="utf-8")
        return HTMLResponse(content=content)
    except (OSError, FileNotFoundError):
        logger.error(f"Public page missing from deployment bundle: {page_path}")
        return _public_fallback_page(title)


@router.get("/", include_in_schema=False)
async def root():
    if is_public_enabled():
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/admin/login")


@router.get("/login", include_in_schema=False)
async def public_login():
    if not is_public_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_public_page("login.html", "Public Login")


@router.get("/imagine", include_in_schema=False)
async def public_imagine():
    if not is_public_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_public_page("imagine.html", "Public Imagine")


@router.get("/voice", include_in_schema=False)
async def public_voice():
    if not is_public_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_public_page("voice.html", "Public Voice")


@router.get("/video", include_in_schema=False)
async def public_video():
    if not is_public_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_public_page("video.html", "Public Video")


@router.get("/chat", include_in_schema=False)
async def public_chat():
    if not is_public_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return _serve_public_page("chat.html", "Public Chat")
