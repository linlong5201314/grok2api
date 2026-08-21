<img alt="Grok2API" src="https://github.com/user-attachments/assets/037a0a6e-7986-41cc-b4af-04df612ee886" />

[![Python](https://img.shields.io/badge/python-3.13%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.119%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Version](https://img.shields.io/badge/version-2.1.0-111827)](../pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-16a34a)](../LICENSE)
[![中文](https://img.shields.io/badge/中文-2563EB?logo=bookstack&logoColor=white)](../README.md)

> [!NOTE]
> This project is a deep fork of [chenyme/grok2api](https://github.com/chenyme/grok2api), for learning and research only. Please comply with Grok's terms of use and all applicable local laws and regulations.

<br>

Grok2API is a **FastAPI**-based Grok gateway that exposes Grok Web capabilities through OpenAI-compatible APIs.

## Features

### Gateway
- OpenAI-compatible endpoints: `/v1/models`, `/v1/chat/completions`, `/v1/responses`, `/v1/images/generations`, `/v1/images/edits`, `/v1/videos`, `/v1/videos/{video_id}`, `/v1/videos/{video_id}/content`
- Anthropic-compatible endpoint: `/v1/messages`
- Streaming / non-streaming chat, explicit reasoning output, function-tool passthrough, unified token accounting
- Multi-account pools, tier-aware selection, failure feedback, quota synchronization
- Local image/video caching and locally proxied media URLs

### 🌸 Subscription Egress (new in this fork)
- Airport subscription ingestion: base64 share-link lists and Clash YAML; `ss` / `vmess` / `vless` / `trojan` / `hysteria2` / `socks` / `http(s)` protocols
- Background speed testing (TCP + HTTP TTFB) with EWMA scoring and automatic dead-node demotion
- Per-account sticky egress binding: each account deterministically maps to one node among the top-N fastest — no IP hopping per account
- Optional sing-box core: core-protocol nodes egress via local mixed inbounds when a binary is configured
- Admin "Subscriptions" page: manage sources, one-click speed test, live node ranking (all credentials redacted)

### 🔒 Anti-Fingerprinting Hardening (new in this fork)
- Per-account stable UA / Accept-Language derived from the account token, always aligned with a curl_cffi-impersonatable browser build (UA never contradicts the TLS fingerprint)
- Configurable upstream Sentry Baggage release (`proxy.fingerprint.sentry_release`)
- Login brute-force lockout (5 failures → 15 min) for admin/webui
- Security headers on panel surfaces: `nosniff`, `DENY`, `no-referrer`, `no-store`

### 🎨 Anime-fresh Control Panel (redesigned)
- Sakura gradient background with falling-petal animation (pure CSS, `prefers-reduced-motion` aware)
- Glassmorphism cards, gradient buttons, soft pastel palette
- New Subscriptions page; 6-language i18n across all pages

<br>

## Quick Start

```bash
git clone <your-repo-url> grok2api
cd grok2api
cp .env.example .env      # review settings before first run
uv sync
uv run granian --interface asgi --host 0.0.0.0 --port 8000 --workers 1 app.main:app
```

Or with Docker Compose:

```bash
cp .env.example .env
docker compose up -d
```

First-run checklist:
1. Change `app.app_key` (admin password — never keep defaults on a public host).
2. Set a strong random `app.api_key`.
3. Set `app.app_url` or proxied media links may 403.

<br>

## Subscription Egress Guide

1. Open `/admin/subscriptions`, paste an airport subscription URL, click **Add** — nodes are fetched immediately.
2. In `/admin/config` set proxy mode to **subscription**, or edit `${DATA_DIR}/config.toml`:
   ```toml
   [proxy.egress]
   mode = "subscription"
   ```
3. Trigger **Speed Test** on the subscriptions page; probing then runs automatically every `speedtest_interval_sec`.

All upstream traffic now egresses through the best-scored nodes, with each account pinned to one stable line.

Key tuning knobs:

```toml
[proxy.subscription]
urls = []                       # subscription URLs (or manage from the panel)
refresh_interval_sec = 3600     # re-fetch cycle
speedtest_interval_sec = 1800   # probe cycle
affinity_spread = 3             # per-account candidates among top-N nodes
max_nodes = 64                  # routing pool cap
core_path = ""                  # optional sing-box binary path
```

<br>

## Pages

| Page | Path |
| :-- | :-- |
| Admin login | `/admin/login` |
| Accounts | `/admin/account` |
| Subscriptions | `/admin/subscriptions` |
| Configuration | `/admin/config` |
| Cache | `/admin/cache` |
| WebUI login | `/webui/login` |
| Web Chat | `/webui/chat` |
| Masonry | `/webui/masonry` |
| ChatKit | `/webui/chatkit` |

<br>

## Security Notes

- One account = one stable IP + one stable device fingerprint. Avoid toggling egress modes frequently or sharing accounts across deployments.
- Never commit `.env` / credential files; if any secret ever entered git history, rotate it immediately.
- Put the panel behind a reverse proxy with access control when exposing beyond localhost.

See the [Chinese README](../README.md) for the complete configuration reference, API examples, and architecture notes.
