"""browser_search — 优先 SearXNG，回退 CDP 浏览器搜索。"""
from __future__ import annotations
import json, logging, os, re, subprocess, urllib.request
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)
_AB = os.path.expanduser("~/.hermes/node/bin/agent-browser")

SKIP_TITLES = {"Web results", "Search Results", "AI Overview", "相关问题",
               "网页搜索结果", "网页导航", "页脚链接", "分享", "相关链接",
               "用户还搜索了"}
SKIP_LINES = {"·", "›", "翻译此页", "转为简体网页", "Read more",
              "关于这条结果的详细信息", "缺少字词："}
NOISE = {"How They Work", "下一页", "上一页", "AI 概览", "相关问题",
         "Videos for", "Images for", "News for"}

ENGINES = {
    "google":  {"url": "https://www.google.com/search?q={q}&hl={lang}&num=15",
                "container": "#rso", "wait_sel": "#rso"},
    "bing":    {"url": "https://www.bing.com/search?q={q}&count=15",
                "container": "#b_results", "wait_sel": "#b_results"},
    "duckduckgo": {"url": "https://duckduckgo.com/?q={q}&ia=web",
                   "container": "article", "wait_sel": "article"},
}


def _ab(args: list[str], timeout: int = 20) -> str:
    try:
        r = subprocess.run([_AB, "--session", "bs"] + args,
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or r.stderr).strip()
    except:
        return ""


def _dom(url: str) -> str:
    u = url.replace("\xa0", " ").split(" ›")[0].strip()
    try:
        return urlparse(u).netloc.replace("www.", "")
    except:
        return u.replace("https://", "").split("/")[0] if "http" in u else u.split("/")[0]


# ---------------------------------------------------------------------------
# 主入口：优先 Hermes 配置的 search_backend → 回退浏览器
# ---------------------------------------------------------------------------

def _get_hermes_search() -> tuple[Any, str] | None:
    """从 Hermes registry 获取配置的 search_backend。"""
    try:
        from agent.web_search_registry import _providers
        if not _providers:
            return None
        # 按 hermes 自己的 resolve 逻辑：web.search_backend → web.backend → 偏好序
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        preferred = cfg.get("web", {}).get("search_backend") or cfg.get("web", {}).get("backend", "")
        candidates = list(_providers.values())
        # 按配置偏好排序
        if preferred:
            candidates.sort(key=lambda p: 0 if p.name == preferred else 1)
        for p in candidates:
            if p.supports_search() and p.is_available():
                return p, p.name
    except Exception:
        pass
    return None


def _search_hermes_backend(query: str, limit: int) -> list[dict]:
    """调 Hermes 配置的 search_backend。"""
    result = _get_hermes_search()
    if not result:
        return []
    p, _ = result
    try:
        raw = p.search(query, limit=limit)
        items = []
        seen: set[str] = set()
        for r in raw.get("data", {}).get("web", []):
            u = r.get("url", "")
            if u and u not in seen:
                seen.add(u)
                items.append({
                    "title": r.get("title", ""),
                    "url": u,
                    "desc": r.get("description", ""),
                    "provider": p.name,
                })
        return items
    except Exception as e:
        logger.warning("hermes search error: %s", e)
        return []


# ---------------------------------------------------------------------------
# 浏览器搜索（回退）
# ---------------------------------------------------------------------------

def _google(query: str, seen: set) -> list[dict]:
    url = ENGINES["google"]["url"].format(q=quote(query), lang="zh-CN")
    _ab(["open", url], timeout=10)
    _ab(["wait", "#rso"], timeout=10)
    for _ in range(5):
        _ab(["scroll", "down"], timeout=5)

    raw = _ab(["get", "text", "#rso"], timeout=15)
    texts = []
    if raw:
        for block in re.split(r"\n\n+", raw):
            lines = [l.strip() for l in block.split("\n") if l.strip()]
            if not lines or lines[0] in SKIP_TITLES | NOISE:
                continue
            if len(lines) < 2 or not lines[1].startswith("http"):
                continue
            u, d, f = "", [], False
            for ln in lines[1:]:
                if ln.startswith("http"):
                    u = ln; f = True; continue
                if not f or ln in SKIP_LINES: continue
                d.append(ln)
            desc = re.sub(r"…\s*$", "", " ".join(d).replace("\u00a0", " ")).strip()
            if u and desc:
                texts.append({"site": lines[0], "url": u, "desc": desc})

    raw2 = _ab(["snapshot", "-u"], timeout=15)
    snaps = []
    if raw2:
        lines, idx = raw2.split("\n"), 0
        while idx < len(lines) - 1:
            u = re.search(r"\[[^\]]*url=([^\]]+)\]", lines[idx])
            h = re.search(r'heading "([^"]*)" \[level=([23])', lines[idx + 1])
            if h and (t := h.group(1).strip()) and t not in SKIP_TITLES and t not in NOISE:
                snaps.append({"title": t, "url": u.group(1).strip() if u else ""})
                idx += 1
            idx += 1

    avail = list(snaps)
    merged = []
    for t in texts:
        tc = t["url"].rstrip(".").strip()
        best = None
        for idx, s in enumerate(avail):
            su = s["url"]
            if tc in su or su in tc or (tc.replace(" › ", "/") in su or su[:len(tc.replace(" › ", "/"))] == tc.replace(" › ", "/")):
                best = (s, idx); break
        if not best:
            for idx, s in enumerate(avail):
                if _dom(s["url"]) and _dom(s["url"]) == _dom(t["url"]):
                    best = (s, idx); break
        if best:
            s, idx = best
            url = s["url"] or t["url"]
            k = url or s["title"]
            if k and k not in seen:
                seen.add(k)
                merged.append({"title": s["title"], "url": url, "desc": t["desc"]})
                avail.pop(idx)
    _ab(["close"], timeout=5)
    return merged


def _bing(query: str, seen: set) -> list[dict]:
    url = ENGINES["bing"]["url"].format(q=quote(query))
    _ab(["open", url], timeout=10)
    _ab(["wait", "#b_results"], timeout=10)
    for _ in range(3):
        _ab(["scroll", "down"], timeout=5)

    raw = _ab(["get", "text", "#b_results"], timeout=15)
    texts = []
    if raw:
        blocks = [lines for b in re.split(r"\n\n+", raw) if (lines := [l.strip() for l in b.split("\n") if l.strip()]) and lines[0] not in SKIP_TITLES]
        i = 0
        while i < len(blocks):
            lines = blocks[i]
            if len(lines) >= 2 and lines[1].startswith("http"):
                desc = ""
                if i + 1 < len(blocks):
                    nd = re.sub(r"…\s*$", "", " ".join(blocks[i + 1]).replace("\u00a0", " ")).strip()
                    if nd and len(nd) > 5:
                        desc = nd; i += 1
                if desc:
                    texts.append({"site": lines[0], "url": lines[1], "desc": desc})
            i += 1

    raw2 = _ab(["snapshot", "-u"], timeout=15)
    snaps = []
    if raw2:
        lines = raw2.split("\n")
        for i in range(len(lines) - 1):
            u = re.search(r"\[[^\]]*url=([^\]]+)\]", lines[i])
            h = re.search(r'heading "([^"]*)" \[level=2', lines[i + 1])
            if h and (t := h.group(1).strip()) and t not in SKIP_TITLES and t not in NOISE:
                snaps.append({"title": t, "url": u.group(1).strip() if u else ""})

    avail = list(snaps)
    merged = []
    for t in texts:
        tc = t["url"].rstrip(".").strip()
        best = None
        for idx, s in enumerate(snaps):
            su = s["url"]
            if tc in su or su in tc:
                best = (s, idx); break
            tws = tc.replace(" › ", "/")
            if tws in su or su[:len(tws)] == tws:
                best = (s, idx); break
        if not best:
            for idx, s in enumerate(snaps):
                if _dom(s["url"]) == _dom(t["url"]):
                    best = (s, idx); break
        if best:
            s, idx = best
            url = s["url"] or t["url"]
            k = url or s["title"]
            if k and k not in seen:
                seen.add(k)
                merged.append({"title": s["title"], "url": url, "desc": t["desc"]})
                snaps.pop(idx)
    _ab(["close"], timeout=5)
    return merged


def _ddg(query: str, seen: set) -> list[dict]:
    url = ENGINES["duckduckgo"]["url"].format(q=quote(query))
    _ab(["open", url], timeout=10)
    _ab(["wait", "article"], timeout=10)
    for _ in range(3):
        _ab(["scroll", "down"], timeout=5)

    raw = _ab(["snapshot", "-u"], timeout=15)
    if not raw:
        return []
    lines = raw.split("\n")
    merged, i = [], 0
    while i < len(lines):
        if re.match(r"^\s*- article", lines[i]):
            url, title, desc_parts = "", "", []
            i += 1
            while i < len(lines) and not re.match(r"^\s*- article", lines[i]):
                u = re.search(r"\[[^\]]*url=([^\]]+)\]", lines[i])
                if u and "duckduckgo" not in u.group(1) and "search" not in u.group(1):
                    url = u.group(1).strip()
                h = re.search(r'heading "([^"]*)" \[level=2', lines[i])
                if h:
                    title = h.group(1).strip()
                st = re.search(r'- StaticText "([^"]*)"', lines[i])
                if st and title:
                    t = st.group(1).strip()
                    if t and len(t) > 5:
                        desc_parts.append(t)
                i += 1
            desc = " ".join(desc_parts).replace("\u00a0", " ").strip()
            if title and title not in SKIP_TITLES and title not in NOISE:
                k = url or title
                if k and k not in seen:
                    seen.add(k)
                    merged.append({"title": title, "url": url, "desc": desc})
            continue
        i += 1
    _ab(["close"], timeout=5)
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def multi_search(query: str, limit: int = 10,
                 skip_backend: bool = False) -> Dict[str, Any]:
    """优先 Hermes search_backend → 回退浏览器搜索。
    
    Args:
        skip_backend: 设为 True 则直接走浏览器，跳过 Hermes 配置的后端。
    """
    all_items: list[dict] = []

    # Step 1: 调 Hermes 配置的 search_backend（除非跳过）
    if not skip_backend:
        hx_items = _search_hermes_backend(query, limit)
        logger.info("hermes_backend: %d items", len(hx_items))
        all_items.extend(hx_items)

    # Step 2: 如果不够，补浏览器搜索
    if len(all_items) < limit:
        try:
            ws = json.loads(
                urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=3).read()
            )["webSocketDebuggerUrl"]
            _ab(["connect", ws], timeout=10)
        except:
            pass

        seen = set(it["url"] for it in all_items)
        for fn in [_google, _bing, _ddg]:
            if len(all_items) >= limit:
                break
            try:
                all_items.extend(fn(query, seen))
            except Exception as e:
                logger.warning("browser engine error: %s", e)

    for i, it in enumerate(all_items):
        it["position"] = i + 1
    return {"success": True, "data": {"web": all_items[:limit]}}


def browser_search(query: str, limit: int = 5, pages: Optional[int] = None,
                   engine: Optional[str] = None) -> Dict[str, Any]:
    if engine is None:
        engine = "google"
    try:
        ws = json.loads(
            urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=3).read()
        )["webSocketDebuggerUrl"]
        _ab(["connect", ws], timeout=10)
    except:
        pass
    seen: set[str] = set()
    if engine == "google":
        items = _google(query, seen)
    elif engine == "bing":
        items = _bing(query, seen)
    elif engine == "duckduckgo":
        items = _ddg(query, seen)
    else:
        items = []
    for i, it in enumerate(items):
        it["position"] = i + 1
    return {"success": True, "data": {"web": items[:limit]}}
