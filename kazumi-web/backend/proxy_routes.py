import base64
import json
from typing import Optional

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse

router = APIRouter(prefix="/api")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            verify=False,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=15.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


class FetchBody:
    def __init__(self, data: dict):
        self.url: str = data.get("url", "")
        self.method: str = (data.get("method") or "GET").upper()
        self.headers: dict = dict(data.get("headers") or {})
        self.body: Optional[bytes] = None
        raw_body = data.get("body")
        if raw_body is not None:
            if data.get("bodyIsBase64"):
                try:
                    self.body = base64.b64decode(raw_body)
                except Exception:
                    self.body = str(raw_body).encode()
            else:
                self.body = str(raw_body).encode()


@router.post("/fetch")
async def fetch(request: Request) -> Response:
    data = await request.json()
    spec = FetchBody(data)
    if not spec.url:
        return Response(status_code=400, content="missing url")

    headers = {k: v for k, v in spec.headers.items() if k.lower() not in HOP_BY_HOP and v is not None}
    try:
        resp = await get_client().request(
            spec.method,
            spec.url,
            headers=headers,
            content=spec.body,
        )
    except httpx.HTTPError as e:
        payload = json.dumps({"type": "network", "message": str(e)}).encode()
        return Response(
            content=payload,
            status_code=599,
            headers={"X-Kazumi-Status": "0", "X-Kazumi-Error": "network"},
        )

    out_headers = {
        k: v
        for k, v in resp.headers.multi_items()
        if k.lower() not in HOP_BY_HOP
    }
    out_headers["X-Kazumi-Status"] = str(resp.status_code)
    content = resp.content
    if resp.history:
        chain = [str(r.url) for r in resp.history] + [str(resp.url)]
        out_headers["X-Kazumi-Redirect-Chain"] = " <- ".join(chain)
    return Response(
        content=content,
        status_code=200,
        headers=out_headers,
    )


@router.get("/img")
async def img_proxy(
    url: str = Query(...),
    referer: Optional[str] = Query(None),
) -> Response:
    headers = {}
    if referer:
        headers["referer"] = referer
    try:
        resp = await get_client().get(url, headers=headers)
    except httpx.HTTPError:
        return Response(status_code=502)
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Kazumi-Status": str(resp.status_code),
        },
    )


@router.api_route("/video", methods=["GET", "HEAD"])
async def video_proxy(request: Request, url: str = Query(...)) -> Response:
    headers = {}
    for key in ("range", "referer", "user-agent", "accept", "origin"):
        value = request.headers.get(key)
        if value and key not in ("origin",):
            headers[key] = value
    if "origin" in request.headers:
        headers.pop("origin", None)

    client = get_client()
    req = client.build_request("GET" if request.method == "GET" else "HEAD", url, headers=headers)
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError:
        return Response(status_code=502)

    out_headers = {
        k: v
        for k, v in resp.headers.multi_items()
        if k.lower() not in HOP_BY_HOP
    }
    status = resp.status_code
    if request.method == "HEAD":
        await resp.aclose()
        return Response(status_code=status, headers=out_headers)

    async def stream_bytes():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        stream_bytes(),
        status_code=status,
        headers=out_headers,
    )
