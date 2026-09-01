# ── Builder ───────────────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

ENV PATH="$UV_PROJECT_ENVIRONMENT/bin:$PATH"

# glibc wheels exist for every dependency (curl-cffi, orjson, tiktoken, ...),
# so no compiler toolchain is needed on Debian slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin uv to a minor version for reproducible builds.
# Bump manually when you want to pick up a newer uv release.
COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /uvx /bin/

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project \
    && find /opt/venv -type d \
         \( -name "__pycache__" -o -name "tests" -o -name "test" -o -name "testing" \) \
         -prune -exec rm -rf {} + \
    && find /opt/venv -type f -name "*.pyc" -delete \
    && find /opt/venv -type f -name "*.so" -exec strip --strip-unneeded {} + 2>/dev/null; true \
    && rm -rf /root/.cache /tmp/uv-cache

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    VIRTUAL_ENV=/opt/venv \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8000 \
    SERVER_WORKERS=1

ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata \
        ca-certificates \
        libcurl4 \
    && rm -rf /var/lib/apt/lists/*

# Bundle the OFFICIAL sing-box release so subscription core-protocol nodes
# (vmess/vless+reality/trojan/ss/hysteria2) work out of the box.
#
# NOTE: Alpine's own musl sing-box package fails REALITY verification against
# live vless-reality servers ("reality verification failed"), which is fatal
# for every chat request through such nodes. The official glibc build handles
# the same config correctly — and on a glibc base it runs without compat hacks.
ARG SINGBOX_VERSION=1.12.9
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64)  sb_arch="amd64" ;; \
        aarch64) sb_arch="arm64" ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-linux-${sb_arch}.tar.gz"; \
    python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/sing-box.tar.gz')" "$url"; \
    tar -xzf /tmp/sing-box.tar.gz -C /tmp; \
    mv "/tmp/sing-box-${SINGBOX_VERSION}-linux-${sb_arch}/sing-box" /usr/local/bin/sing-box; \
    chmod +x /usr/local/bin/sing-box; \
    rm -rf /tmp/sing-box.tar.gz "/tmp/sing-box-${SINGBOX_VERSION}-linux-${sb_arch}"; \
    sing-box version

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY pyproject.toml config.defaults.toml ./
COPY app ./app
COPY scripts ./scripts

RUN mkdir -p /app/data /app/logs \
    && chmod +x /app/scripts/entrypoint.sh /app/scripts/init_storage.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,os; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('SERVER_PORT','8000')}/health\", timeout=4)"]

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["sh", "-c", "exec granian --interface asgi --host ${SERVER_HOST} --port ${SERVER_PORT} --workers ${SERVER_WORKERS} app.main:app"]
