"""
Grok2API 应用入口

FastAPI 应用初始化和路由注册
"""

from contextlib import asynccontextmanager
import os
import platform
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


def _resolve_public_dir() -> Path:
    for candidate in (BASE_DIR / "public", BASE_DIR / "_public"):
        if candidate.exists():
            return candidate
    return BASE_DIR / "_public"


PUBLIC_DIR = _resolve_public_dir()

# Ensure the project root is on sys.path even if the process CWD differs by platform.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

env_file = BASE_DIR / ".env"
if env_file.exists():
    load_dotenv(env_file)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi import Depends  # noqa: E402

from app.core.auth import verify_api_key  # noqa: E402
from app.core.config import config, get_config  # noqa: E402
from app.core.logger import logger, setup_logging  # noqa: E402
from app.core.exceptions import register_exception_handlers  # noqa: E402
from app.core.response_middleware import ResponseLoggerMiddleware  # noqa: E402
from app.api.v1.chat import router as chat_router  # noqa: E402
from app.api.v1.image import router as image_router  # noqa: E402
from app.api.v1.video import router as video_router  # noqa: E402
from app.api.v1.files import router as files_router  # noqa: E402
from app.api.v1.models import router as models_router  # noqa: E402
from app.api.v1.response import router as responses_router  # noqa: E402
from app.services.token import get_scheduler  # noqa: E402
from app.api.v1.admin import router as admin_router
from app.api.v1.function import router as function_router
from app.api.pages import router as pages_router
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

_ENV_TRUE_VALUES = {"1", "true", "yes", "on"}


def _parse_env_bool(value: str) -> bool:
    return str(value).strip().lower() in _ENV_TRUE_VALUES


def _parse_env_int(value: str) -> int:
    return int(str(value).strip())


def _parse_env_list(value: str) -> list[str]:
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _is_vercel_runtime() -> bool:
    return bool(os.getenv("VERCEL")) or bool(os.getenv("VERCEL_ENV"))


def _should_run_background_jobs() -> bool:
    raw = os.getenv("BACKGROUND_JOBS_ENABLED")
    if raw is not None and raw != "":
        return _parse_env_bool(raw)
    return not _is_vercel_runtime()


# 初始化日志
setup_logging(
    level=os.getenv("LOG_LEVEL", "INFO"),
    json_console=False,
    file_logging=_parse_env_bool(os.getenv("LOG_FILE_ENABLED", "true")),
)


def _collect_runtime_env_overrides() -> tuple[dict, dict]:
    """Collect runtime config overrides from env for Railway-style deployment."""
    overrides: dict = {}
    applied: dict = {}

    def set_value(
        section: str,
        key: str,
        env_name: str,
        parser=None,
    ):
        raw = os.getenv(env_name)
        if raw is None or raw == "":
            return
        try:
            value = parser(raw) if parser else raw
        except Exception as exc:
            logger.warning(f"Skip invalid env {env_name}: {exc}")
            return
        overrides.setdefault(section, {})[key] = value
        applied[f"{section}.{key}"] = env_name

    set_value("app", "app_url", "APP_URL")
    set_value("app", "app_key", "APP_KEY")
    set_value("app", "api_key", "API_KEY")
    set_value("app", "function_enabled", "FUNCTION_ENABLED", _parse_env_bool)
    set_value("app", "function_key", "FUNCTION_KEY")
    set_value("app", "image_format", "IMAGE_FORMAT")
    set_value("app", "video_format", "VIDEO_FORMAT")
    set_value("app", "temporary", "APP_TEMPORARY", _parse_env_bool)
    set_value("app", "disable_memory", "DISABLE_MEMORY", _parse_env_bool)
    set_value("app", "stream", "APP_STREAM", _parse_env_bool)
    set_value("app", "thinking", "APP_THINKING", _parse_env_bool)
    set_value("app", "dynamic_statsig", "DYNAMIC_STATSIG", _parse_env_bool)
    set_value("app", "custom_instruction", "CUSTOM_INSTRUCTION")
    set_value("app", "filter_tags", "FILTER_TAGS", _parse_env_list)

    set_value("proxy", "base_proxy_url", "BASE_PROXY_URL")
    set_value("proxy", "asset_proxy_url", "ASSET_PROXY_URL")
    set_value("proxy", "enabled", "PROXY_ENABLED", _parse_env_bool)
    set_value("proxy", "flaresolverr_url", "FLARESOLVERR_URL")
    set_value("proxy", "refresh_interval", "CF_REFRESH_INTERVAL", _parse_env_int)
    set_value("proxy", "timeout", "CF_TIMEOUT", _parse_env_int)
    set_value("proxy", "cf_clearance", "CF_CLEARANCE")
    set_value("proxy", "browser", "PROXY_BROWSER")
    set_value("proxy", "user_agent", "PROXY_USER_AGENT")

    if (
        os.getenv("FLARESOLVERR_URL")
        and "enabled" not in overrides.get("proxy", {})
        and not get_config("proxy.enabled")
    ):
        overrides.setdefault("proxy", {})["enabled"] = True
        applied["proxy.enabled"] = "FLARESOLVERR_URL(auto)"

    return overrides, applied


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 1. 注册服务默认配置
    from app.core.config import config, register_defaults
    from app.services.grok.defaults import get_grok_defaults

    register_defaults(get_grok_defaults())

    # 2. 加载配置
    await config.ensure_loaded()

    runtime_env_overrides, applied_envs = _collect_runtime_env_overrides()
    if runtime_env_overrides:
        config.merge_runtime_overrides(runtime_env_overrides)
        logger.info(
            "Applied runtime config from env: {}",
            sorted(applied_envs.keys()),
        )

    # 3. 启动服务显示
    logger.info("Starting Grok2API...")
    logger.info(f"Platform: {platform.system()} {platform.release()}")
    logger.info(f"Python: {sys.version.split()[0]}")

    # 4. 启动 Token 刷新调度器
    background_jobs_enabled = _should_run_background_jobs()

    refresh_enabled = get_config("token.auto_refresh", True)
    scheduler = None
    if refresh_enabled and background_jobs_enabled:
        basic_interval = get_config("token.refresh_interval_hours", 8)
        super_interval = get_config("token.super_refresh_interval_hours", 2)
        interval = min(basic_interval, super_interval)
        scheduler = get_scheduler(interval)
        scheduler.start()
    elif refresh_enabled:
        logger.info("Skipping token refresh scheduler on serverless runtime")

    # 5. 启动 cf_clearance 自动刷新
    from app.services.cf_refresh import start as cf_refresh_start
    from app.services.cf_refresh import stop as cf_refresh_stop

    if background_jobs_enabled:
        cf_refresh_start()
    else:
        logger.info("Skipping cf_refresh scheduler on serverless runtime")

    logger.info("Application startup complete.")
    yield

    # 关闭
    logger.info("Shutting down Grok2API...")

    if background_jobs_enabled:
        cf_refresh_stop()

    from app.core.storage import StorageFactory

    if StorageFactory._instance:
        await StorageFactory._instance.close()

    if refresh_enabled and scheduler:
        scheduler.stop()


def create_app() -> FastAPI:
    """创建 FastAPI 应用"""
    app = FastAPI(
        title="Grok2API",
        lifespan=lifespan,
    )

    # CORS 配置
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 请求日志和 ID 中间件
    app.add_middleware(ResponseLoggerMiddleware)

    @app.middleware("http")
    async def ensure_config_loaded(request: Request, call_next):
        await config.ensure_loaded()
        return await call_next(request)

    # 注册异常处理器
    register_exception_handlers(app)

    # 注册路由
    app.include_router(
        chat_router, prefix="/v1", dependencies=[Depends(verify_api_key)]
    )
    app.include_router(
        image_router, prefix="/v1", dependencies=[Depends(verify_api_key)]
    )
    app.include_router(
        models_router, prefix="/v1", dependencies=[Depends(verify_api_key)]
    )
    app.include_router(
        responses_router, prefix="/v1", dependencies=[Depends(verify_api_key)]
    )
    app.include_router(
        video_router, prefix="/v1", dependencies=[Depends(verify_api_key)]
    )
    app.include_router(files_router, prefix="/v1/files")

    # 静态文件服务（统一使用 /_public/static）
    static_dir = PUBLIC_DIR / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # 注册管理与功能玩法路由
    app.include_router(admin_router, prefix="/v1/admin")
    app.include_router(function_router, prefix="/v1/function")
    app.include_router(pages_router)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse(url="/static/common/img/favicon/favicon.ico")

    # 健康检查接口（用于 Render、服务器保活检测等）
    @app.get("/health")
    def health():
        """
        健康检查接口，用于服务器保活或 Render 自动检测
        """
        return {"status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("PORT") or os.getenv("SERVER_PORT", "8000"))
    workers = int(os.getenv("SERVER_WORKERS", "1"))
    log_level = os.getenv("LOG_LEVEL", "INFO").lower()
    logger.error(
        "Direct startup via `python main.py` is disabled. "
        "Please run with Granian CLI to avoid Python wrapper issues."
    )
    logger.error(
        "Use: uv run granian --interface asgi "
        f"--host {host} --port {port} --workers {workers} "
        f"--log-level {log_level} main:app"
    )
    raise SystemExit(1)
