"""本地排队代理：把对老王网关的并发压到 2，避免 429。
AstrBot 的 api_base 指向 http://127.0.0.1:8878/v1 即可。
"""
import asyncio, os, sys
import aiohttp
from aiohttp import web, ClientSession, ClientTimeout

def _load_upstream() -> str:
    """上游网关地址：优先环境变量 GW_UPSTREAM，其次同目录的 gw.local.json（不进仓库）。"""
    v = (os.environ.get("GW_UPSTREAM") or "").strip()
    if v:
        return v
    try:
        import json as _json
        cfg = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "gw.local.json"), encoding="utf-8"))
        return str(cfg.get("upstream", "")).strip()
    except Exception:
        return ""


UPSTREAM = _load_upstream()   # 部署时填：环境变量 GW_UPSTREAM 或 tools/gw.local.json
PORT = int(os.environ.get("GW_PORT", "8878"))
LIMIT = int(os.environ.get("GW_CONCURRENCY", "2"))

sem = asyncio.Semaphore(LIMIT)
stats = {"req": 0, "queued": 0, "err": 0, "cancel": 0}

async def proxy(request: web.Request):
    path = request.rel_url.path_qs
    body = await request.read()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
    stats["req"] += 1
    if sem.locked():
        stats["queued"] += 1
        print(f"[proxy] 排队中 (并发已满{LIMIT}) path={path} 累计排队={stats['queued']}", flush=True)
    async with sem:
        try:
            timeout = ClientTimeout(total=300, sock_read=120)
            async with ClientSession(timeout=timeout) as s:
                async with s.request(request.method, UPSTREAM + path, data=body, headers=headers) as r:
                    resp = web.StreamResponse(status=r.status, headers={
                        k: v for k, v in r.headers.items()
                        if k.lower() in ("content-type", "cache-control", "x-request-id")
                    })
                    await resp.prepare(request)
                    async for chunk in r.content.iter_any():
                        await resp.write(chunk)
                    await resp.write_eof()
                    return resp
        except (asyncio.CancelledError, ConnectionResetError) as e:
            # 客户端（AstrBot）主动取消/断开：正常现象，不算故障
            stats["cancel"] += 1
            print(f"[proxy] 客户端断开 {type(e).__name__}（已计入 cancel，不算错误）", flush=True)
            raise
        except aiohttp.ClientConnectionResetError as e:
            stats["cancel"] += 1
            print(f"[proxy] 客户端断开 {type(e).__name__}（已计入 cancel，不算错误）", flush=True)
            return web.json_response({"error": {"message": "client gone", "type": "cancelled"}}, status=499)
        except Exception as e:
            stats["err"] += 1
            print(f"[proxy] 上游错误 {type(e).__name__}: {e}", flush=True)
            return web.json_response({"error": {"message": f"proxy upstream error: {e}", "type": "proxy_error"}}, status=502)

async def models(request: web.Request):
    return await proxy(request)

async def health(request: web.Request):
    return web.json_response({"limit": LIMIT, **stats})

app = web.Application(client_max_size=64 * 1024 * 1024)
app.router.add_route("GET", "/health", health)
app.router.add_route("POST", "/v1/{tail:.*}", proxy)
app.router.add_route("GET", "/v1/{tail:.*}", proxy)

if __name__ == "__main__":
    print(f"[proxy] 监听 127.0.0.1:{PORT}  上游={UPSTREAM}  最大并发={LIMIT}", flush=True)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None, access_log=None)
