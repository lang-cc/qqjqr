# -*- coding: utf-8 -*-
"""联网底层：搜索引擎 + B站官方 API + 网页正文提取。

全程不需要任何 API key：

- 搜索：cn.bing.com 直连；结果太差（全是导航页/空）时自动退到搜狗、360。
- B站：走 api.bilibili.com 官方接口（视频详情 / 站内搜视频 / 热门 / 排行榜 / 热搜词 / UP主）。
  因为 bilibili.com 的网页对机房 IP 会返 412 风控，API 反而是通的。
- 普通网页：lxml 提正文（article → 段落最多的 div → p → body 逐级兜底）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

from lxml import html as LH

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
BILI_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
}
BILI_HEADERS_REF = {**BILI_HEADERS, "Referer": "https://www.bilibili.com/"}

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"(?:/video/)?av(\d+)", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _get(url: str, timeout: int = 15, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


_COOKIE: dict[str, str] = {}
_COOKIE_AT = [0.0]


def _bili_cookie(force: bool = False) -> str:
    """领一个 buvid3 cookie。不带它，B站的 view 类接口对机房 IP 会一直返 412。"""
    if _COOKIE and not force and time.time() - _COOKIE_AT[0] < 3600:
        return "; ".join(f"{k}={v}" for k, v in _COOKIE.items())
    try:
        raw = _get(
            "https://api.bilibili.com/x/frontend/finger/spi",
            timeout=10,
            headers=BILI_HEADERS,
        )
        d = (json.loads(raw.decode("utf-8", errors="ignore")) or {}).get("data") or {}
        if d.get("b_3"):
            _COOKIE.clear()
            _COOKIE["buvid3"] = d["b_3"]
            if d.get("b_4"):
                _COOKIE["buvid4"] = d["b_4"]
            _COOKIE_AT[0] = time.time()
    except Exception:
        pass
    return "; ".join(f"{k}={v}" for k, v in _COOKIE.items())


def _get_json(url: str, timeout: int = 12, headers: dict | None = None, retries: int = 3) -> dict | None:
    """拿 JSON。B站会甩风控 HTML（412）过来，所以带上 buvid cookie、换一组 header、退避重试。"""
    for i in range(retries + 1):
        base = dict(headers or BILI_HEADERS)
        ck = _bili_cookie(force=(i > 0))
        if ck:
            base["Cookie"] = ck
        for hdr in (base, {**base, **BILI_HEADERS_REF}):
            try:
                raw = _get(url, timeout=timeout, headers=hdr)
                j = json.loads(raw.decode("utf-8", errors="ignore"))
                if isinstance(j, dict):
                    return j
            except Exception:
                continue
        if i < retries:
            time.sleep(0.5 + 0.5 * i)
    return None


def _clean(s, limit: int = 240) -> str:
    s = TAG_RE.sub("", str(s or ""))
    return " ".join(s.split())[:limit]


# --------------------------------------------------------------------------- 搜索


def _bing(query: str, n: int) -> list[dict]:
    url = (
        "https://cn.bing.com/search?q="
        + urllib.parse.quote(query)
        + f"&form=QBRE&mkt=zh-CN&setlang=zh-CN&count={max(n, 8)}"
    )
    try:
        doc = LH.fromstring(_get(url).decode("utf-8", errors="ignore"))
    except Exception:
        return []
    out: list[dict] = []
    for it in doc.xpath('//li[contains(@class,"b_algo")]'):
        title = _clean("".join(it.xpath(".//h2//text()")), 120)
        href = it.xpath(".//h2//a/@href")
        snippet = _clean("".join(it.xpath(".//p//text()")), 240)
        if not title:
            continue
        if not snippet:
            snippet = _clean("".join(it.xpath(".//text()")), 200)
        out.append({"title": title, "url": (href[0] if href else ""), "snippet": snippet})
    return out


def _sogou(query: str, n: int) -> list[dict]:
    url = "https://www.sogou.com/web?query=" + urllib.parse.quote(query)
    try:
        doc = LH.fromstring(_get(url).decode("utf-8", errors="ignore"))
    except Exception:
        return []
    out: list[dict] = []
    for it in doc.xpath('//div[contains(@class,"vrwrap") or contains(@class,"rb")]'):
        title = _clean("".join(it.xpath(".//h3//text()")), 120)
        if not title:
            continue
        href = it.xpath(".//h3//a/@href")
        link = href[0] if href else ""
        if link.startswith("/"):
            link = "https://www.sogou.com" + link
        snippet = _clean(
            "".join(
                it.xpath(
                    './/div[contains(@class,"text-layout") or contains(@class,"fz-mid") '
                    'or contains(@class,"space-txt")]//text()'
                )
            ),
            240,
        )
        out.append({"title": title, "url": link, "snippet": snippet})
        if len(out) >= n:
            break
    return out


def _so360(query: str, n: int) -> list[dict]:
    url = "https://www.so.com/s?q=" + urllib.parse.quote(query)
    try:
        doc = LH.fromstring(_get(url).decode("utf-8", errors="ignore"))
    except Exception:
        return []
    out: list[dict] = []
    for it in doc.xpath('//li[contains(@class,"res-list")]'):
        title = _clean("".join(it.xpath(".//h3//text()")), 120)
        if not title:
            continue
        href = it.xpath(".//h3//a/@href")
        snippet = _clean(
            "".join(it.xpath('.//p//text() | .//div[contains(@class,"res-desc")]//text()')), 240
        )
        out.append({"title": title, "url": (href[0] if href else ""), "snippet": snippet})
        if len(out) >= n:
            break
    return out


def _is_nav(r: dict) -> bool:
    """「XX官网 / XX首页 / 贴吧站内导航」这类结果，信息量基本为零。"""
    path = urllib.parse.urlparse(r.get("url") or "").path
    if path in ("", "/", "/index.html", "/index.htm"):
        return True
    if path.startswith(("/f/index", "/index/")):
        return True
    return False


def _site_domain(q: str) -> str:
    m = re.search(r"site:([a-z0-9.\-]+\.[a-z]{2,})", q, re.I)
    return m.group(1).lower() if m else ""


def _strip_site(q: str) -> str:
    return re.sub(r"site:\S+", " ", q, flags=re.I).strip()


def _filter_domain(rows: list[dict], dom: str) -> list[dict]:
    if not dom:
        return rows
    return [r for r in rows if dom in urllib.parse.urlparse(r.get("url") or "").netloc.lower()]


def _dedupe(rows: list[dict], n: int, per_domain: int = 2) -> list[dict]:
    seen_domain: dict[str, int] = {}
    seen_url: set[str] = set()
    out: list[dict] = []
    for r in rows:
        u = r.get("url") or ""
        if not u or u in seen_url:
            continue
        host = urllib.parse.urlparse(u).netloc
        if seen_domain.get(host, 0) >= per_domain:
            continue
        seen_url.add(u)
        seen_domain[host] = seen_domain.get(host, 0) + 1
        out.append(r)
        if len(out) >= n:
            break
    return out


def bing_search(query: str, n: int = 6) -> list[dict]:
    """返回 [{title, url, snippet}]。

    Bing 优先；结果太差就退到搜狗、360 兜底。
    带 `site:域名` 的查询会严格按域名过滤，B站域名直接改走官方搜索。
    """
    q = (query or "").strip()
    if not q:
        return []

    dom = _site_domain(q)
    if dom and "bilibili.com" in dom:
        rows = bili_search(_strip_site(q) or q, n)
        if rows:
            return rows[:n]

    rows = _bing(q, n + 4)
    raw_rows = rows
    if dom:
        rows = _filter_domain(rows, dom)
    useful = [r for r in rows if not _is_nav(r)]

    need = n if dom else max(2, n // 2)
    if len(useful) < need:
        for fn in (_sogou, _so360):
            try:
                alt = [r for r in _filter_domain(fn(q, n + 2), dom) if not _is_nav(r)]
            except Exception:
                alt = []
            if len(alt) > len(useful):
                useful = alt
            if len(useful) >= need:
                break
    if dom and not useful:
        # 指定域名一条都拿不到（比如贴吧被 WAF 挡了），至少给个不带域名的结果，别交白卷
        useful = [r for r in raw_rows if not _is_nav(r)]
    return _dedupe(useful or rows, n)


# --------------------------------------------------------------------------- B站


def bili_key(text: str) -> tuple[str | None, str | None]:
    """从 BV号 / av号 / 完整链接里认出视频。"""
    s = text or ""
    m = BV_RE.search(s)
    if m:
        return "bvid", m.group(1)
    m = AV_RE.search(s)
    if m:
        return "aid", m.group(1)
    return None, None


def _fmt_dur(sec) -> str:
    try:
        sec = int(sec)
    except Exception:
        return "?"
    return f"{sec // 60}:{sec % 60:02d}"


def _fmt_num(v) -> str:
    try:
        v = int(v)
    except Exception:
        return "?"
    if v >= 100_000_000:
        return f"{v / 100_000_000:.1f}亿"
    if v >= 10_000:
        return f"{v / 10_000:.1f}万"
    return str(v)


_VIEW_CACHE: dict[str, tuple[float, dict | None]] = {}


def _fetch_view(qs: str) -> tuple[dict, list[str]]:
    """拿视频详情。优先 view/detail（限流没那么狠），退到 view 接口。"""
    j = _get_json("https://api.bilibili.com/x/web-interface/view/detail?" + qs)
    if j and j.get("code") == 0:
        data = j.get("data") or {}
        d = data.get("View") or {}
        if d:
            tags = []
            for t in (data.get("Tags") or [])[:6]:
                tag = _clean(t.get("tag_name"), 20)
                if tag:
                    tags.append(tag)
            return d, tags
    j = _get_json("https://api.bilibili.com/x/web-interface/view?" + qs)
    if j and j.get("code") == 0:
        d = j.get("data") or {}
        if d:
            return d, []
    return {}, []


def bili_view(key: str, ttl: int = 300) -> dict | None:
    """视频详情：标题 / UP主 / 播放 / 点赞 / 简介 / 发布时间。5 分钟内同一个视频走缓存。"""
    kind, val = bili_key(key)
    if not kind:
        return None
    ck = f"{kind}:{val}"
    hit = _VIEW_CACHE.get(ck)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    qs = ("bvid=" + val) if kind == "bvid" else ("aid=" + val)
    d, tags = _fetch_view(qs)
    if not d:
        return None
    owner = d.get("owner") or {}
    stat = d.get("stat") or {}
    try:
        pub = time.strftime("%Y-%m-%d", time.localtime(int(d.get("pubdate") or 0)))
    except Exception:
        pub = "?"
    out = {
        "bvid": d.get("bvid") or "",
        "title": _clean(d.get("title"), 120),
        "up": _clean(owner.get("name"), 40),
        "mid": owner.get("mid"),
        "tname": _clean(d.get("tname"), 20),
        "duration": _fmt_dur(d.get("duration")),
        "pubdate": pub,
        "views": stat.get("view"),
        "like": stat.get("like"),
        "coin": stat.get("coin"),
        "favorite": stat.get("favorite"),
        "danmaku": stat.get("danmaku"),
        "reply": stat.get("reply"),
        "tags": tags,
        "desc": _clean(d.get("desc"), 300),
        "url": "https://www.bilibili.com/video/" + (d.get("bvid") or ""),
    }
    _VIEW_CACHE[ck] = (time.time(), out)
    return out


def bili_view_text(key: str) -> str:
    v = bili_view(key)
    if not v:
        return ""
    tags = ("　标签：" + "、".join(v.get("tags") or [])) if v.get("tags") else ""
    return (
        f"【B站视频】{v['title']}\n"
        f"UP主：{v['up']}（UID {v['mid']}）　分区：{v['tname']}　"
        f"时长：{v['duration']}　发布：{v['pubdate']}{tags}\n"
        f"播放 {_fmt_num(v['views'])}　弹幕 {_fmt_num(v['danmaku'])}　"
        f"点赞 {_fmt_num(v['like'])}　投币 {_fmt_num(v['coin'])}　收藏 {_fmt_num(v['favorite'])}\n"
        f"简介：{v['desc'] or '（无）'}\n"
        f"链接：{v['url']}"
    )


def bili_search(keyword: str, n: int = 6) -> list[dict]:
    """B站站内搜视频，返回 [{title, url, snippet, bvid}]。"""
    kw = (keyword or "").strip()
    if not kw:
        return []
    url = (
        "https://api.bilibili.com/x/web-interface/search/type?"
        "search_type=video&page=1&keyword=" + urllib.parse.quote(kw)
    )
    j = _get_json(url)
    items = ((j or {}).get("data") or {}).get("result") or []
    out: list[dict] = []
    for it in items:
        bvid = it.get("bvid") or ""
        if not bvid:
            continue
        pub = it.get("pubdate")
        if isinstance(pub, (int, float)) and pub > 0:
            try:
                pub = time.strftime("%Y-%m-%d", time.localtime(int(pub)))
            except Exception:
                pub = ""
        out.append(
            {
                "title": _clean(it.get("title"), 90),
                "url": "https://www.bilibili.com/video/" + bvid,
                "snippet": (
                    f"UP：{_clean(it.get('author'), 24)}　播放：{_fmt_num(it.get('play'))}　"
                    f"弹幕：{_fmt_num(it.get('video_review'))}　时长：{_clean(it.get('duration'), 10)}　"
                    f"发布：{_clean(pub, 20)}　{bvid}"
                ),
                "bvid": bvid,
            }
        )
        if len(out) >= n:
            break
    return out


def bili_search_text(keyword: str, n: int = 6) -> str:
    rows = bili_search(keyword, n)
    if not rows:
        return ""
    lines = [f"【B站搜索结果】{keyword}"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['title']}\n   {r['snippet']}\n   {r['url']}")
    return "\n".join(lines)


def _bili_video_list(api: str, n: int, name: str) -> str:
    j = _get_json(api)
    items = ((j or {}).get("data") or {}).get("list") or []
    if not items:
        return ""
    lines = [f"【B站{name}】"]
    for i, it in enumerate(items[:n], 1):
        stat = it.get("stat") or {}
        lines.append(
            f"{i}. {_clean(it.get('title'), 80)}\n"
            f"   UP：{_clean((it.get('owner') or {}).get('name'), 24)}　"
            f"播放：{_fmt_num(stat.get('view'))}　时长：{_fmt_dur(it.get('duration'))}　"
            f"https://www.bilibili.com/video/{it.get('bvid')}"
        )
    return "\n".join(lines)


def bili_popular(n: int = 8) -> str:
    """B站综合热门。"""
    return _bili_video_list(
        "https://api.bilibili.com/x/web-interface/popular?ps=%d&pn=1" % max(n, 5), n, "热门榜"
    )


def bili_ranking(n: int = 8) -> str:
    """B站全站排行榜。"""
    return _bili_video_list(
        "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all", n, "排行榜"
    )


def bili_hot_search(n: int = 10) -> str:
    """B站热搜词。"""
    j = _get_json("https://api.bilibili.com/x/web-interface/search/square?limit=%d" % max(n, 10))
    items = (((j or {}).get("data") or {}).get("trending") or {}).get("list") or []
    words = []
    for it in items[:n]:
        w = _clean(it.get("keyword") or it.get("show_name"), 30)
        if w:
            words.append(w)
    if not words:
        return ""
    return "【B站热搜】" + "　".join(f"{i}. {w}" for i, w in enumerate(words, 1))


def bili_user(mid, n: int = 5) -> str:
    """UP主名片 + 最近投稿（space 接口有频率限制，拿不到就只给名片）。"""
    try:
        mid = str(int(mid))
    except Exception:
        return ""
    j = _get_json("https://api.bilibili.com/x/web-interface/card?mid=" + mid)
    d = (j or {}).get("data") or {}
    card = d.get("card") or {}
    if not card:
        return ""
    fans = d.get("follower") if d.get("follower") is not None else card.get("fans")
    lines = [
        f"【B站UP主】{_clean(card.get('name'), 30)}（UID {mid}）",
        f"签名：{_clean(card.get('sign'), 120) or '（无）'}",
        f"粉丝：{_fmt_num(fans)}　投稿视频：{_fmt_num(d.get('archive_count'))}",
    ]
    j2 = _get_json(
        f"https://api.bilibili.com/x/space/arc/search?mid={mid}&ps={max(n, 5)}&pn=1"
    )
    vlist = ((((j2 or {}).get("data") or {}).get("list") or {}).get("vlist")) or []
    if vlist:
        lines.append("最近投稿：")
        for i, it in enumerate(vlist[:n], 1):
            lines.append(
                f"{i}. {_clean(it.get('title'), 70)}　播放 {_fmt_num(it.get('play'))}　"
                f"https://www.bilibili.com/video/{it.get('bvid')}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- 网页正文

_DROP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form", "noscript", "iframe")


def _html_to_text(raw: bytes, max_chars: int) -> str:
    try:
        doc = LH.fromstring(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return ""

    for tag in _DROP_TAGS:
        for node in doc.xpath(f"//{tag}"):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)

    blocks = doc.xpath("//article//text()")

    # 没有 article 就找「段落文字最多的那个 div」——比按 class 猜准得多
    if not blocks:
        best_node, best_len = None, 0
        for div in doc.xpath("//div|//section"):
            ps = div.xpath("./p//text()")
            ln = sum(len(str(x).strip()) for x in ps)
            if ln > best_len:
                best_len, best_node = ln, div
        if best_node is not None and best_len >= 150:
            blocks = best_node.xpath(".//text()")

    if not blocks:
        blocks = doc.xpath("//p//text()")
    if not blocks:
        blocks = doc.xpath("//body//text()")

    # 只保留有实质内容的片段（砍掉导航项那种两三个字的）
    chunks = []
    for b in blocks:
        s = " ".join(str(b).split())
        if len(s) >= 8:
            chunks.append(s)
    text = " ".join(chunks) if chunks else " ".join(
        " ".join(str(b).split()) for b in blocks if str(b).strip()
    )
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:max_chars]


def _bili_route(url: str, max_chars: int) -> str:
    """B站链接走官方 API（网页会被 412 风控，API 反而通）。"""
    low = url.lower()

    if "search.bilibili.com" in low:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        kw = (qs.get("keyword") or qs.get("q") or [""])[0]
        return bili_search_text(kw, 6)[:max_chars]

    if "space.bilibili.com" in low:
        m = re.search(r"space\.bilibili\.com/(\d+)", low)
        if m:
            return bili_user(m.group(1))[:max_chars]

    if "video/" in low or BV_RE.search(url) or AV_RE.search(low):
        t = bili_view_text(url)
        if t:
            return t[:max_chars]

    if "b23.tv" in low:
        # 短链：跟随跳转后再按上面的规则走一次
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                real = r.geturl()
            if real and real != url:
                return fetch_text(real, max_chars)
        except Exception:
            return ""

    return ""


def fetch_text(url: str, max_chars: int = 2500) -> str:
    """抓网页正文并压成一段纯文本；B站链接自动改走官方 API。"""
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return ""

    if "bilibili.com" in u.lower() or "b23.tv" in u.lower():
        t = _bili_route(u, max_chars)
        if t:
            return t

    try:
        raw = _get(u, timeout=20)
    except Exception:
        return ""
    return _html_to_text(raw, max_chars)
