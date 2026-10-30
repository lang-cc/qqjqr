# -*- coding: utf-8 -*-
"""B站扫码登录：把二维码发到群里，用户用手机B站 App 扫一下，就把 cookie 存在本地。

只用官方 passport 接口，不需要密码：
- generate → 拿 qrcode_key + 二维码内容
- poll     → 扫码状态（86101 未扫 / 86090 已扫未确认 / 86038 过期 / 0 成功）
登录成功后保存 SESSDATA / bili_jct / DedeUserID 等，后续请求带上它们即可。
"""

from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=%s"
NAV = "https://api.bilibili.com/x/web-interface/nav"
SPI = "https://api.bilibili.com/x/frontend/finger/spi"

# 要保存的 cookie
WANT = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")

STATUS = {
    0: "登录成功",
    86101: "还没扫码",
    86090: "扫了，等你在手机上点确认",
    86038: "二维码过期了",
    -1: "网络抖了一下（继续等）",
    -2: "返回不是 JSON（继续等）",
    -3: "拿到了成功状态但没拿到 cookie（继续等）",
}


def _opener():
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", UA), ("Referer", "https://www.bilibili.com/")]
    return op, jar


def _jar_cookies(jar: http.cookiejar.CookieJar) -> dict:
    return {c.name: c.value for c in jar if c.value}


def gen_qr(png_path: str | Path) -> tuple[str, str]:
    """生成二维码，返回 (qrcode_key, 二维码图片路径)。"""
    op, _ = _opener()
    with op.open(QR_GEN, timeout=15) as r:
        j = json.loads(r.read().decode("utf-8", errors="ignore"))
    data = j.get("data") or {}
    url = data.get("url") or ""
    key = data.get("qrcode_key") or ""
    if not url or not key:
        return "", ""

    import qrcode  # 服务器 venv 里已有

    img = qrcode.make(url)
    p = Path(png_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(p))
    return key, str(p)


def poll_once(key: str) -> tuple[int, dict]:
    """查一次扫码状态。返回 (code, cookies)。code=0 表示成功。

    成功的标志有两个来源，两边都收：
    1. 响应里的 Set-Cookie（由 cookie jar 收下）
    2. data.url 里的 query（SESSDATA=…&bili_jct=…），有些情况下只有这个
    """
    op, jar = _opener()
    raw = b""
    try:
        with op.open(QR_POLL % urllib.parse.quote(key), timeout=15) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:  # 412/429 之类也尽量把 body 读出来
        try:
            raw = e.read()
        except Exception:
            return -1, {}
    except Exception:
        return -1, {}

    try:
        j = json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return -2, {}

    d = j.get("data") or {}
    code = int(d.get("code") if d.get("code") is not None else (j.get("code") if j.get("code") is not None else -1))
    if code != 0:
        return code, {}

    cookies = {k: v for k, v in _jar_cookies(jar).items() if k in WANT}

    # data.url 里带着 SESSDATA 等参数，jar 没收到就用它
    link = d.get("url") or ""
    if link:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        for k in WANT:
            if not cookies.get(k) and qs.get(k):
                cookies[k] = urllib.parse.unquote(qs[k][0])

    if not cookies.get("SESSDATA"):
        return -3, {}

    # 补 buvid3（没有它部分接口会 412）
    try:
        with op.open(SPI, timeout=10) as r:
            spi = json.loads(r.read().decode("utf-8", errors="ignore"))
        b3 = ((spi.get("data") or {}).get("b_3")) or ""
        if b3:
            cookies["buvid3"] = b3
    except Exception:
        pass
    return 0, cookies


def save_session(path: str | Path, cookies: dict, extra: dict | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"at": time.time(), "cookies": cookies, **(extra or {})}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(path: str | Path) -> dict:
    p = Path(path)
    try:
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8") or "{}")
            if isinstance(d.get("cookies"), dict) and d["cookies"].get("SESSDATA"):
                return d
    except Exception:
        pass
    return {}


def nav(cookies: dict) -> dict:
    """用 cookie 问一下自己是谁；没登录会返回 isLogin=False。"""
    ck = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req = urllib.request.Request(
        NAV,
        headers={"User-Agent": UA, "Cookie": ck, "Referer": "https://www.bilibili.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            j = json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception:
        return {}
    d = j.get("data") or {}
    if not d.get("isLogin"):
        return {}
    return {
        "mid": d.get("mid"),
        "uname": d.get("uname"),
        "vip": (d.get("vipStatus") == 1),
        "level": d.get("level_info", {}).get("current_level"),
        "coins": d.get("money"),
    }
