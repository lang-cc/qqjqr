#!/usr/bin/env python3
"""肥鱼控制台后端（只监听 127.0.0.1:8790，由 nginx 加密码对外）

功能：
  GET  /api/status            总览（服务/端口/网关/QQ/资源）
  GET  /api/logs?src=&n=      看日志（astrbot / gwproxy / snowluma）
  POST /api/action            重启/停止/启动（astrbot|gwproxy|snowluma|all）
  POST /api/send              用机器人给管理员发一条消息
  POST /api/selftest          一条龙自检
  GET  /api/bili/qr           B站扫码登录：出二维码（base64）
  GET  /api/bili/qr/status    B站扫码登录：查扫码状态，成功即存 cookie 并重启 AstrBot
  GET  /api/bili/info         B站当前登录态
  POST /api/bili/logout       B站退出登录
CLI：
  python3 botconsole.py --selftest
"""
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP = "/opt/astrbot"
ADMIN_QQ = "2966763558"
BOT_QQ = "1126449714"
GROUP = "414858556"
GW_PROXY = "http://127.0.0.1:8878"
NAPCAT = "http://127.0.0.1:3000"
TOKEN_FILE = "/opt/astrbot/tools/napcat_http_token.txt"
BILI_PLUGIN_DIR = "/opt/astrbot/data/plugins"
BILI_COOKIE = "/opt/astrbot/data/plugin_data/astrbot_plugin_xiaonian_web/bili_cookie.json"
BILI_QR_PNG = "/opt/botconsole/bili_qr.png"
LOG_FILES = {
    "astrbot": "/opt/astrbot/logs/astrbot.log",
    "gwproxy": "/opt/astrbot/logs/gwproxy.log",
}
SERVICES = ["astrbot", "gwproxy", "botconsole"]
PORTS = {6185: "AstrBot 面板", 6199: "OneBot 反向WS", 8878: "排队代理", 3000: "OneBot HTTP", 5099: "SnowLuma WebUI"}


# ---------------- 工具函数 ----------------
def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, "%s: %s" % (type(e).__name__, e)


def snowluma_token():
    for p in ("/opt/snowluma/data/config/onebot.json",
              "/opt/snowluma/data/config/onebot_1126449714.json"):
        try:
            d = json.load(open(p, encoding="utf-8"))
            nets = d.get("networks") or {}
            for kind in ("httpServers", "wsServers"):
                for it in nets.get(kind) or []:
                    if it.get("accessToken"):
                        return it["accessToken"]
        except Exception:
            continue
    try:
        return open(TOKEN_FILE, encoding="utf-8").read().strip()
    except Exception:
        return ""


def napcat_token():
    try:
        return open(TOKEN_FILE, encoding="utf-8").read().strip()
    except Exception:
        return ""


def astrbot_token():
    """AstrBot 里 aiocqhttp 平台配的 access token（用来跟协议端比对）。"""
    try:
        d = json.load(open(APP + "/data/cmd_config.json", encoding="utf-8-sig"))
        for p in d.get("platform") or []:
            for k in ("ws_reverse_token", "token", "access_token"):
                t = (p.get(k) or "")
                if isinstance(t, str) and t:
                    return t
    except Exception:
        pass
    return ""


def onebot(action, payload=None, timeout=10):
    """调 NapCat HTTP API"""
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        NAPCAT + "/" + action, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + snowluma_token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception as e:
        return {"status": "error", "message": "%s: %s" % (type(e).__name__, e)}


def http_json(url, payload=None, headers=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers=headers or {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


def port_open(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(1.5)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def sysinfo():
    info = {}
    try:
        info["load"] = os.getloadavg()[0]
    except Exception:
        info["load"] = 0
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k] = int(v.strip().split()[0])
        info["mem_total_mb"] = mem["MemTotal"] // 1024
        info["mem_used_mb"] = (mem["MemTotal"] - mem["MemAvailable"]) // 1024
    except Exception:
        pass
    try:
        st = os.statvfs("/")
        info["disk_total_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 1)
        info["disk_free_gb"] = round(st.f_bavail * st.f_frsize / 1e9, 1)
    except Exception:
        pass
    try:
        info["uptime_h"] = round(float(open("/proc/uptime").read().split()[0]) / 3600, 1)
    except Exception:
        pass
    return info


def tail(path, n=80):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200000))
            data = f.read().decode("utf-8", "replace")
        lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in data.splitlines()]
        return lines[-n:]
    except Exception as e:
        return ["<读取失败 %s: %s>" % (path, e)]


def counters():
    """从 astrbot 日志统计：今日入站 / 发送成功 / 429 / 最近一条入站"""
    out = {"inbound": 0, "sent": 0, "rate429": 0, "last_inbound": "", "last_inbound_time": ""}
    try:
        with open(LOG_FILES["astrbot"], "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 5_000_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return out
    for ln in lines:
        ln = re.sub(r"\x1b\[[0-9;]*m", "", ln)
        if "[default] [napcat-onebot(aiocqhttp)]" in ln and "event_bus" in ln:
            out["inbound"] += 1
            m = re.match(r"\[(\d\d:\d\d:\d\d)", ln)
            body = ln.split("[napcat-onebot(aiocqhttp)]", 1)[-1].strip()
            out["last_inbound"] = body[:60]
            out["last_inbound_time"] = m.group(1) if m else ""
        elif "Prepare to send" in ln or "发送成功" in ln:
            out["sent"] += 1
        elif re.match(r"\[\d\d:\d\d:\d\d\.\d+\].*429", ln):
            out["rate429"] += 1
    return out


def status():
    st = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "services": {}, "ports": {},
          "gateway": {}, "qq": {}, "system": sysinfo(), "config": {}}
    for s in SERVICES:
        code, out = run(["systemctl", "is-active", s])
        st["services"][s] = out.strip() or "unknown"
    code, out = run("docker inspect -f '{{.State.Status}}' snowluma")
    st["services"]["snowluma"] = out.strip() or "missing"

    for p, name in PORTS.items():
        st["ports"]["%s %s" % (p, name)] = port_open(p)

    code, out = http_json(GW_PROXY + "/health", timeout=5)
    if code == 200:
        try:
            st["gateway"] = json.loads(out)
        except Exception:
            st["gateway"] = {"raw": out}
        st["gateway"]["up"] = True
    else:
        st["gateway"] = {"up": False, "err": out[:200]}

    li = onebot("get_login_info", timeout=6)
    if li.get("status") == "ok":
        d = li.get("data") or {}
        st["qq"] = {"online": True, "uin": d.get("user_id"), "nickname": d.get("nickname")}
    else:
        st["qq"] = {"online": False, "err": li.get("message", "")[:120]}

    # B站登录态（60 秒缓存，不拖慢状态刷新）
    try:
        st["bili"] = bili_status_cached()
    except Exception as e:  # noqa: BLE001
        st["bili"] = {"login": False, "error": str(e)}

    logs = tail(LOG_FILES["astrbot"], 400)
    st["config"]["onebot_connected"] = any("适配器已连接" in ln for ln in logs)
    st["config"]["admin"] = ADMIN_QQ
    st["config"]["group"] = GROUP
    cfg = {}
    try:
        d = json.load(open(APP + "/data/cmd_config.json", encoding="utf-8-sig"))
        ps = d.get("provider_settings", {})
        cfg["whitelist"] = d.get("platform_settings", {}).get("id_whitelist")
        cfg["admins_id"] = d.get("admins_id")
        cfg["computer_use"] = ps.get("computer_use_runtime")
        cfg["perm"] = ps.get("computer_use_local_permissions")
        cfg["require_admin"] = ps.get("computer_use_require_admin")
        cfg["persona"] = d.get("agent_runner", {}).get("config", {}).get("persona", {}).get("persona_id")
        cfg["model"] = d.get("provider", [{}])[0].get("model") if d.get("provider") else None
        cfg["api_base"] = d.get("provider_sources", [{}])[0].get("api_base") if d.get("provider_sources") else None
        cfg["tts"] = d.get("provider_tts_settings", {}).get("enable")
        cfg["compression"] = d.get("agent_runner", {}).get("config", {}).get("compression", {}).get("max_turns")
    except Exception as e:
        cfg["error"] = str(e)
    st["config"].update(cfg)

    # 只统计「本次启动之后」的错误，历史错误不算
    start_idx = 0
    for i, ln in enumerate(logs):
        if "AstrBot started" in ln:
            start_idx = i
    current = logs[start_idx:]
    errs = [ln for ln in current if ("ERROR" in ln or "Traceback" in ln or "Error code:" in ln)]
    st["recent_errors"] = errs[-5:]
    st["since_start_lines"] = len(current)
    st.update(counters())
    st["tokens"] = {"onebot": snowluma_token(), "astrbot": astrbot_token()}
    return st


# ---------------- 一条龙自检 ----------------
def selftest():
    steps = []

    def add(name, ok, detail=""):
        steps.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})
        return ok

    # 1 systemd
    for s in ["astrbot", "gwproxy"]:
        code, out = run(["systemctl", "is-active", s])
        add("服务 %s 运行中" % s, out.strip() == "active", out.strip())

    # 2 容器
    code, out = run("docker inspect -f '{{.State.Status}}' snowluma")
    add("容器 snowluma 运行中", out.strip() == "running", out.strip())

    # 3 端口
    for p in [6185, 6199, 8878, 3000, 5099]:
        add("端口 %d 在监听" % p, port_open(p))

    # 4 排队代理健康
    code, out = http_json(GW_PROXY + "/health", timeout=5)
    ok = False
    detail = out[:200]
    if code == 200:
        try:
            h = json.loads(out)
            ok = isinstance(h.get("err"), int) and h.get("err") == 0
            detail = json.dumps(h, ensure_ascii=False)
        except Exception:
            pass
    add("排队代理 8878 /health 正常（err=0）", ok, detail)

    # 5 网关真实对话（走代理，验证 key/上游/模型）
    api_key, model = "", "deepseek-v4.1-flash"
    try:
        d = json.load(open(APP + "/data/cmd_config.json", encoding="utf-8-sig"))
        api_key = (d.get("provider_sources") or [{}])[0].get("key", [""])[0]
        model = (d.get("provider") or [{}])[0].get("model", model)
    except Exception:
        pass
    status_code, body = http_json(
        GW_PROXY + "/v1/chat/completions",
        {"model": model,
         "messages": [{"role": "user", "content": "只回两个字：收到"}],
         "max_tokens": 16, "stream": False},
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key},
        timeout=120)
    ok = status_code == 200 and ("收到" in body or "choices" in body)
    add("模型网关可用（一条最小对话）", ok, "HTTP %s | %s" % (status_code, body[:180]))

    # 6 QQ 在线
    li = onebot("get_login_info", timeout=8)
    ok = li.get("status") == "ok" and str((li.get("data") or {}).get("user_id")) == BOT_QQ
    add("QQ %s 在线" % BOT_QQ, ok, json.dumps(li, ensure_ascii=False)[:200])

    # 7 OneBot 适配器
    logs = tail(LOG_FILES["astrbot"], 600)
    add("AstrBot OneBot 适配器已连接", any("适配器已连接" in ln for ln in logs), "")

    # 8 人格
    code, out = run([APP + "/.venv/bin/python", "-c",
                     "import sqlite3;c=sqlite3.connect('%s/data/data_v4.db');"
                     "r=c.execute(\"select persona_id,length(system_prompt) from personas where persona_id='feiyu'\").fetchone();"
                     "print(r)" % APP], timeout=20)
    add("人格 feiyu 已写入数据库", "feiyu" in out and "None" not in out, out.strip()[:120])

    # 9 白名单/权限/配置
    try:
        d = json.load(open(APP + "/data/cmd_config.json", encoding="utf-8-sig"))
        wl = d["platform_settings"]["id_whitelist"]
        add("白名单含管理员私聊 UMO", any(ADMIN_QQ in x and "FriendMessage" in x for x in wl), wl)
        add("白名单含群 %s" % GROUP, GROUP in wl, wl)
        add("管理员 admins_id", ADMIN_QQ in (d.get("admins_id") or []), d.get("admins_id"))
        ps = d["provider_settings"]
        perm = ps.get("computer_use_local_permissions", {})
        maxed = all(perm.get(k, {}).get("allow_execution") and perm.get(k, {}).get("allow_network")
                    and perm.get(k, {}).get("filesystem_scope") == "host" for k in ("member", "admin"))
        add("最高权限（执行+联网+全盘，member/admin 全开）",
            ps.get("computer_use_runtime") == "local" and maxed and ps.get("computer_use_require_admin") is False,
            json.dumps(perm, ensure_ascii=False))
        add("模型 base_url 指向本地代理 8878",
            "127.0.0.1:8878" in str(d.get("provider_sources", [{}])[0].get("api_base")), "")
        add("上下文压缩已开", d["agent_runner"]["config"]["compression"].get("max_turns", -1) > 0,
            d["agent_runner"]["config"]["compression"].get("max_turns"))
    except Exception as e:
        add("配置读取", False, e)

    # 10 ffmpeg
    add("ffmpeg 已安装（语音需要）", shutil.which("ffmpeg") is not None, shutil.which("ffmpeg") or "")

    # 10.5 TTS 真实合成（等密钥期间允许关闭，不算失败）
    try:
        d0 = json.load(open(APP + "/data/cmd_config.json", encoding="utf-8-sig"))
        tts_on = bool((d0.get("provider_tts_settings") or {}).get("enable"))
        voice = ""
        for s in d0.get("provider_sources", []):
            if s.get("type") == "dashscope_tts":
                voice = s.get("dashscope_tts_voice", "")
        if not tts_on:
            add("语音 TTS", True, "已关闭（等老大给密钥）")
        else:
            code, out = run([APP + "/.venv/bin/python", "/opt/astrbot/tools/tts_smoke.py"],
                            timeout=120)
            ok = code == 0 and "TTS_OK" in out
            add("语音 TTS 真实合成", ok,
                (out.strip().splitlines() or [""])[-1][:110] + " | voice=..." + voice[-14:])
    except Exception as e:
        add("语音 TTS", False, e)

    # 11 磁盘/内存
    si = sysinfo()
    add("磁盘剩余 >3G", si.get("disk_free_gb", 0) > 3, si.get("disk_free_gb"))
    add("内存可用（用了 %s/%s MB）" % (si.get("mem_used_mb"), si.get("mem_total_mb")),
        si.get("mem_used_mb", 9999) < si.get("mem_total_mb", 0) * 0.95)

    # 12 端到端发消息（真的发一条给管理员）
    sent = onebot("send_private_msg", {"user_id": int(ADMIN_QQ),
                                       "message": "【自检】本鱼在线，链路全通 🐟"}, timeout=15)
    ok = sent.get("status") == "ok" and (sent.get("data") or {}).get("message_id")
    add("端到端发消息给管理员 %s" % ADMIN_QQ, ok, json.dumps(sent, ensure_ascii=False)[:200])

    passed = sum(1 for s in steps if s["ok"])
    return {"total": len(steps), "passed": passed,
            "failed": len(steps) - passed,
            "ok": passed == len(steps), "steps": steps,
            "time": time.strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- HTTP ----------------
# ---------------- B站扫码登录 ----------------
def _bili_auth():
    if BILI_PLUGIN_DIR not in sys.path:
        sys.path.insert(0, BILI_PLUGIN_DIR)
    from astrbot_plugin_xiaonian_web import bili_auth  # 纯标准库，不会拉 astrbot

    return bili_auth


def _restart_async():
    def _do():
        time.sleep(1)
        run("chown -R astrbot:astrbot /opt/astrbot/data/plugin_data", timeout=30)
        run("systemctl restart astrbot", timeout=120)

    threading.Thread(target=_do, daemon=True).start()


def bili_qr() -> dict:
    try:
        a = _bili_auth()
        key, png = a.gen_qr(BILI_QR_PNG)
        if not key or not os.path.exists(png):
            return {"ok": False, "error": "二维码没生成出来（B站通行证接口不通）"}
        with open(png, "rb") as f:
            img = base64.b64encode(f.read()).decode()
        return {"ok": True, "key": key, "img": "data:image/png;base64," + img}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "出码失败：%s" % e}


def bili_qr_status(key: str) -> dict:
    if not key:
        return {"code": -9, "msg": "缺少 qrcode_key"}
    try:
        a = _bili_auth()
        code, cookies = a.poll_once(key)
        out = {"code": code, "msg": a.STATUS.get(code, str(code))}
        if code == 0:
            info = a.nav(cookies) or {}
            a.save_session(BILI_COOKIE, cookies,
                           {"uname": info.get("uname"), "mid": info.get("mid")})
            out.update({"ok": True, "uname": info.get("uname"),
                        "mid": info.get("mid"), "coins": info.get("coins"),
                        "restarting": True})
            _BILI_CACHE["at"] = 0.0  # 让「服务状态」立刻反映新登录态
            _BILI_CACHE["val"] = {}
            _restart_async()
        return out
    except Exception as e:  # noqa: BLE001
        return {"code": -8, "msg": "查询失败：%s" % e}


def bili_info() -> dict:
    try:
        a = _bili_auth()
        sess = a.load_session(BILI_COOKIE)
        if not sess:
            return {"login": False}
        info = a.nav(sess.get("cookies") or {})
        if not info:
            return {"login": False, "expired": True,
                    "saved_at": int(sess.get("at") or 0)}
        return {"login": True, "uname": info.get("uname"), "mid": info.get("mid"),
                "coins": info.get("coins"), "level": info.get("level"),
                "saved_at": int(sess.get("at") or 0)}
    except Exception as e:  # noqa: BLE001
        return {"login": False, "error": str(e)}


_BILI_CACHE = {"at": 0.0, "val": {}}


def bili_status_cached(ttl: int = 60) -> dict:
    """给「服务状态」用的B站在线监测，60 秒缓存一次，别每 8 秒刷一遍接口。"""
    now = time.time()
    if _BILI_CACHE["val"] and now - _BILI_CACHE["at"] < ttl:
        return _BILI_CACHE["val"]
    val = bili_info()
    _BILI_CACHE["at"] = now
    _BILI_CACHE["val"] = val
    return val


def bili_logout() -> dict:
    try:
        if os.path.exists(BILI_COOKIE):
            os.remove(BILI_COOKIE)
    except Exception:
        pass
    _BILI_CACHE["at"] = 0.0
    _BILI_CACHE["val"] = {}
    _restart_async()
    return {"ok": True, "restarting": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "FeiyuConsole/1.0"
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                html = open("/opt/botconsole/index.html", "rb").read()
                return self._send(200, html, "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if u.path == "/api/status":
            return self._send(200, status())
        if u.path == "/api/logs":
            q = parse_qs(u.query)
            src = (q.get("src") or ["astrbot"])[0]
            n = int((q.get("n") or ["80"])[0])
            if src in ("snowluma", "napcat"):
                code, out = run("docker logs --tail %d snowluma" % n, timeout=25)
                lines = [ln for ln in out.splitlines() if not re.fullmatch(r"[0-9a-f]{64}", ln)]
                return self._send(200, {"src": src, "lines": lines[-n:]})
            return self._send(200, {"src": src, "lines": tail(LOG_FILES.get(src, LOG_FILES["astrbot"]), n)})
        if u.path == "/api/selftest":
            return self._send(200, selftest())
        if u.path == "/api/bili/qr":
            return self._send(200, bili_qr())
        if u.path == "/api/bili/qr/status":
            q = parse_qs(u.query)
            return self._send(200, bili_qr_status((q.get("key") or [""])[0]))
        if u.path == "/api/bili/info":
            return self._send(200, bili_info())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/api/action":
            act = body.get("action", "")
            mapping = {
                "restart_astrbot": "systemctl restart astrbot",
                "start_astrbot": "systemctl start astrbot",
                "stop_astrbot": "systemctl stop astrbot",
                "restart_gwproxy": "systemctl restart gwproxy",
                "restart_snowluma": "docker restart snowluma",
                "restart_napcat": "docker restart snowluma",
                "restart_all": "systemctl restart gwproxy astrbot wsbridge && docker restart snowluma",
                "start_all": "systemctl start gwproxy wsbridge astrbot; docker start snowluma",
                "stop_all": "systemctl stop astrbot gwproxy wsbridge; docker stop snowluma",
            }
            if act not in mapping:
                return self._send(400, {"error": "unknown action"})
            code, out = run(mapping[act], timeout=120)
            return self._send(200, {"action": act, "exit": code, "output": out[-800:]})
        if u.path == "/api/send":
            text = (body.get("text") or "").strip() or "【控制台】测试消息"
            r = onebot("send_private_msg", {"user_id": int(ADMIN_QQ), "message": text})
            return self._send(200, r)
        if u.path == "/api/bili/logout":
            return self._send(200, bili_logout())
        if u.path == "/api/selftest":
            return self._send(200, selftest())
        return self._send(404, {"error": "not found"})


def main():
    if "--selftest" in os.sys.argv:
        r = selftest()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1
    srv = ThreadingHTTPServer(("127.0.0.1", 8790), Handler)
    print("botconsole listening on 127.0.0.1:8790")
    srv.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
