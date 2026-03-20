from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app.core.auth import is_function_enabled

router = APIRouter()


@router.get("/", include_in_schema=False)
async def root():
    if is_function_enabled():
        return RedirectResponse(url="/playground")
    return RedirectResponse(url="/admin/login")


@router.get("/playground", include_in_schema=False)
async def function_playground():
    if not is_function_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse(url="/login")


@router.get("/login", include_in_schema=False)
async def function_login():
    if not is_function_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse(url="/static/function/pages/login.html")


@router.get("/imagine", include_in_schema=False)
async def function_imagine():
    if not is_function_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse(url="/static/function/pages/imagine.html")


@router.get("/voice", include_in_schema=False)
async def function_voice():
    if not is_function_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse(url="/static/function/pages/voice.html")


@router.get("/video", include_in_schema=False)
async def function_video():
    if not is_function_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse(url="/static/function/pages/video.html")


@router.get("/chat", include_in_schema=False)
async def function_chat():
    if not is_function_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    return RedirectResponse(url="/static/function/pages/chat.html")
