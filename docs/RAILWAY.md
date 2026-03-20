# Railway 部署说明

当前项目已经改成 Railway 优先部署。

## 推荐部署方式

1. 在 Railway 中基于当前仓库创建一个服务。
2. 添加一个 Volume，并挂载到 `/app/data`。
3. Railway 会自动注入 `PORT`，容器已经监听 `0.0.0.0:$PORT`。
4. 将项目中的 `.env.railway.example` 内容复制到 Railway Variables。

## 可直接复制的完整 env

下面这一份就是推荐的 Railway 单实例完整环境变量模板：

```env
TZ=Asia/Shanghai
LOG_LEVEL=INFO
LOG_FILE_ENABLED=true

DATA_DIR=/app/data
SERVER_HOST=0.0.0.0
SERVER_WORKERS=1
SERVER_STORAGE_TYPE=local
SERVER_STORAGE_URL=

APP_URL=
APP_KEY=change-me-admin-password
API_KEY=
FUNCTION_ENABLED=true
FUNCTION_KEY=
IMAGE_FORMAT=url
VIDEO_FORMAT=html
APP_TEMPORARY=true
DISABLE_MEMORY=true
APP_STREAM=true
APP_THINKING=true
DYNAMIC_STATSIG=true
CUSTOM_INSTRUCTION=
FILTER_TAGS=xaiartifact,xai:tool_usage_card,grok:render

BASE_PROXY_URL=
ASSET_PROXY_URL=
PROXY_ENABLED=false
FLARESOLVERR_URL=
CF_REFRESH_INTERVAL=600
CF_TIMEOUT=60
CF_CLEARANCE=
PROXY_BROWSER=chrome136
PROXY_USER_AGENT=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36
```

## 现在哪些配置支持 env

以下配置现在支持通过 env 在启动时自动写入运行时配置：

- `APP_URL`
- `APP_KEY`
- `API_KEY`
- `FUNCTION_ENABLED`
- `FUNCTION_KEY`
- `IMAGE_FORMAT`
- `VIDEO_FORMAT`
- `APP_TEMPORARY`
- `DISABLE_MEMORY`
- `APP_STREAM`
- `APP_THINKING`
- `DYNAMIC_STATSIG`
- `CUSTOM_INSTRUCTION`
- `FILTER_TAGS`
- `BASE_PROXY_URL`
- `ASSET_PROXY_URL`
- `PROXY_ENABLED`
- `FLARESOLVERR_URL`
- `CF_REFRESH_INTERVAL`
- `CF_TIMEOUT`
- `CF_CLEARANCE`
- `PROXY_BROWSER`
- `PROXY_USER_AGENT`

也就是说，Railway 上现在不需要你先手动改 `data/config.toml`，只填 env 也能完成初始化。

## `config.runtime.example.toml` 还有没有用

还有用。文件在 [config.runtime.example.toml](C:/Users/林龙/Desktop/GitHub仓库修改/grok2api+railway/config.runtime.example.toml)。

适合这两种场景：

- 你想把一份完整运行时配置保存在项目里做参考。
- 你不想把业务配置放在 Railway Variables，而是想在 `/app/data/config.toml` 里维护。

## 存储选择

- 推荐：`SERVER_STORAGE_TYPE=local`，并把 Volume 挂载到 `/app/data`
- 单实例 Railway 不需要 Redis
- 未来如果要多副本，再改成 `pgsql` 或 `mysql`
- Redis 更适合做共享缓存或临时状态，不适合这里当主配置和主 token 存储

## 其他说明

- `vercel.json` 已经删除，当前使用 `railway.toml` + `Dockerfile`
- Supabase 相关部署假设已经移除
- 健康检查地址是 `/health`
