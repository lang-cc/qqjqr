# -*- coding: utf-8 -*-
"""联网底层：Bing 搜索 + 网页正文提取。

不依赖任何 API key，走 cn.bing.com 直连（国内可达）。
"""

from __future__ import annotations

import re
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


def _get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def bing_search(query: str, n: int = 6) -> list[dict]:
    """返回 [{title, url, snippet}, ...]"""
    q = (query or "").strip()
    if not q:
        return []
    url = (
        "https://cn.bing.com/search?q="
        + urllib.parse.quote(q)
        + f"&setlang=zh-CN&count={max(n, 8)}"
    )
    try:
        raw = _get(url)
    except Exception:
        return []
    try:
        doc = LH.fromstring(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return []

    out: list[dict] = []
    for it in doc.xpath('//li[contains(@class,"b_algo")]'):
        title = "".join(it.xpath(".//h2//text()")).strip()
        href = it.xpath(".//h2//a/@href")
        snippet = "".join(it.xpath(".//p//text()")).strip()
        if not title:
            continue
        link = href[0] if href else ""
        if not snippet:
            snippet = " ".join("".join(it.xpath(".//text()")).split())[:200]
        out.append({"title": title, "url": link, "snippet": snippet[:240]})
        if len(out) >= n:
            break
    return out


_DROP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form", "noscript", "iframe")


def fetch_text(url: str, max_chars: int = 2500) -> str:
    """抓网页正文并压成一段纯文本。"""
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return ""
    try:
        raw = _get(u, timeout=20)
    except Exception:
        return ""
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
