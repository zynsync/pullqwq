import json
import re
from typing import Optional
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from proxy_routes import get_client

router = APIRouter(prefix="/api")

AD_PATTERNS = (
    "googleads",
    "googlesyndication",
    "adtrafficquality",
    "doubleclick",
    "union",
    "popads",
)

M3U8_RE = re.compile(
    r"""(?:(?:https?:)?//|[\'"]\.?\/)(?:[^\s\'"<>\\]{1,2048}?)\.m3u8(?![\w])"""
    r"""(?:\?[^\s\'"<>\\]{0,512})?""",
    re.IGNORECASE,
)

MP4_RE = re.compile(
    r"""(?:https?:)?\/\/[^\s\'"<>\\]{1,2048}?\.mp4(?:\?[^\s\'"<>\\]{0,512})?""",
    re.IGNORECASE,
)

PLAYER_JSON_RE = re.compile(
    r"player_aaaa\s*=\s*(\{.*?\})\s*[;<]",
    re.DOTALL,
)

VIDEO_TAG_RE = re.compile(
    r"<(?:video|source)[^>]+src=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

IFRAME_RE = re.compile(
    r"<iframe[^>]+src=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

EXT_RE = re.compile(r"\.m3u8(\?|$)|\.mp4(\?|$)", re.IGNORECASE)


def _is_ad(url: str) -> bool:
    low = url.lower()
    return any(p in low for p in AD_PATTERNS)


def _find_urls_in_text(text: str) -> list:
    found = []
    for m in M3U8_RE.finditer(text):
        raw = m.group(0).lstrip("'\"./")
        if raw:
            found.append(raw)
    for m in MP4_RE.finditer(text):
        found.append(m.group(0))
    return found


def _resolve(base: str, url: str) -> Optional[str]:
    if url.startswith("//"):
        url = "https:" + url
    absolute = urljoin(base, url)
    if not absolute.startswith(("http://", "https://")):
        return None
    if _is_ad(absolute):
        return None
    return absolute


def _classify(url: str) -> str:
    low = url.lower().split("?")[0]
    if low.endswith(".m3u8"):
        return "m3u8"
    if low.endswith(".mp4"):
        return "mp4"
    return "auto"


async def _fetch_text(url: str, referer: Optional[str], ua: Optional[str]) -> str:
    headers = {
        "user-agent": ua
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "zh-CN,zh;q=0.9",
    }
    if referer:
        headers["referer"] = referer
    client = get_client()
    current = url
    for _ in range(6):
        resp = await client.get(current, headers=headers)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                break
            current = urljoin(current, location)
            continue
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 403:
            headers.pop("referer", None)
        return resp.text if resp.text else ""
    return ""


def _scan_page(html: str, base_url: str) -> list:
    results = []
    seen = set()

    for m in PLAYER_JSON_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
            for key in ("url", "source", "src", "file"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    resolved = _resolve(base_url, value)
                    if resolved and resolved not in seen and EXT_RE.search(resolved):
                        seen.add(resolved)
                        results.append(resolved)
        except (json.JSONDecodeError, ValueError):
            pass

    for raw in _find_urls_in_text(html):
        resolved = _resolve(base_url, raw)
        if resolved and resolved not in seen:
            seen.add(resolved)
            results.append(resolved)

    for m in VIDEO_TAG_RE.finditer(html):
        src = m.group(1)
        if src.startswith("blob:") or not src.strip():
            continue
        resolved = _resolve(base_url, src)
        if resolved and resolved not in seen:
            seen.add(resolved)
            results.append(resolved)

    return [r for r in results if EXT_RE.search(r)]


@router.get("/sniff")
async def sniff(
    url: str = Query(...),
    referer: Optional[str] = Query(None),
    ua: Optional[str] = Query(None),
    timeout: int = Query(20),
) -> JSONResponse:
    html = await _fetch_text(url, referer or url, ua)
    if not html:
        return JSONResponse(
            status_code=404,
            content={"error": "empty_page", "message": "page fetch failed"},
        )

    candidates = _scan_page(html, url)
    if not candidates:
        iframes = []
        for m in IFRAME_RE.finditer(html):
            src = m.group(1)
            if src.startswith(("javascript:", "blob:")):
                continue
            resolved = _resolve(url, src)
            if resolved and not _is_ad(resolved):
                iframes.append(resolved)
        for iframe in iframes[:3]:
            try:
                sub_html = await _fetch_text(iframe, iframe, ua)
            except httpx.HTTPError:
                continue
            candidates = _scan_page(sub_html, iframe)
            if candidates:
                break

    if not candidates:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "message": "no video source found"},
        )

    best = candidates[0]
    return JSONResponse(
        content={
            "url": best,
            "format": _classify(best),
            "offset": 0,
            "candidates": candidates[:8],
        }
    )
