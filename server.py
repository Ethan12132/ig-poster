"""Small, private Instagram posting server for Claude (MCP over HTTP).

Uses the official Instagram Graph API on graph.facebook.com with a
never-expiring system-user token (starts with "EAA").

Environment variables (set them in Render, never in this file):
  META_ACCESS_TOKEN  your EAA... token
  IG_USER_ID         your numeric Instagram account ID (17841...)
  MCP_SECRET         a long random string; it becomes part of the URL,
                     so only someone who knows it can use this server
"""

import asyncio
import functools
import os
import re

import httpx2 as httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

GRAPH = "https://graph.facebook.com/v23.0"
TOKEN = os.environ["META_ACCESS_TOKEN"].strip()
IG_USER_ID = os.environ["IG_USER_ID"].strip()
SECRET = re.sub(r"[^A-Za-z0-9]", "", os.environ["MCP_SECRET"])
if len(SECRET) < 20:
    raise SystemExit("MCP_SECRET must be at least 20 letters/numbers long.")

server = MCPServer(
    name="instagram-poster",
    instructions=(
        "Publishes to the owner's Instagram account. Images and videos must be "
        "public HTTPS links (JPEG for images). Post only what the owner asked for."
    ),
)


class GraphError(Exception):
    pass


async def _call(method: str, path: str, **params):
    params["access_token"] = TOKEN
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if method == "GET":
                r = await client.get(f"{GRAPH}/{path}", params=params)
            else:
                r = await client.post(f"{GRAPH}/{path}", data=params)
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise GraphError(f"Could not reach Instagram: {type(e).__name__}") from None
    if "error" in data:
        err = data["error"]
        raise GraphError(f"Instagram API error {err.get('code')}: {err.get('message')}")
    return data


async def _wait_until_ready(container_id: str, max_seconds: int = 300):
    """Videos and carousels are processed before they can be published."""
    waited = 0
    while waited <= max_seconds:
        info = await _call("GET", container_id, fields="status_code,status")
        code = info.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return
        if code in ("ERROR", "EXPIRED"):
            raise GraphError(f"Media processing failed: {info.get('status')}")
        await asyncio.sleep(5)
        waited += 5
    raise GraphError("Media is still processing after 5 minutes; try again later.")


async def _publish(container_id: str):
    await _wait_until_ready(container_id)
    result = await _call("POST", f"{IG_USER_ID}/media_publish", creation_id=container_id)
    media_id = result["id"]
    info = await _call("GET", media_id, fields="permalink")
    return {"published": True, "media_id": media_id, "permalink": info.get("permalink")}


def _safe(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except GraphError as e:
            return {"published": False, "error": str(e)}
    return wrapper


@server.tool()
async def get_account_info() -> dict:
    """Show the connected Instagram account's username, followers and post count."""
    try:
        return await _call(
            "GET", IG_USER_ID,
            fields="id,username,name,followers_count,media_count",
        )
    except GraphError as e:
        return {"error": str(e)}


@server.tool()
async def get_recent_posts(limit: int = 10) -> dict:
    """List the most recent posts with captions, links and dates."""
    try:
        return await _call(
            "GET", f"{IG_USER_ID}/media",
            fields="id,caption,media_type,permalink,timestamp,like_count,comments_count",
            limit=max(1, min(limit, 50)),
        )
    except GraphError as e:
        return {"error": str(e)}


@server.tool()
async def get_publishing_limit() -> dict:
    """How many posts were published in the last 24 hours (Instagram allows a limited number)."""
    try:
        return await _call(
            "GET", f"{IG_USER_ID}/content_publishing_limit",
            fields="quota_usage,config",
        )
    except GraphError as e:
        return {"error": str(e)}


@server.tool()
@_safe
async def publish_image(image_url: str, caption: str = "") -> dict:
    """Publish one photo to the feed. image_url must be a public HTTPS link to a JPEG."""
    c = await _call("POST", f"{IG_USER_ID}/media", image_url=image_url, caption=caption)
    return await _publish(c["id"])


@server.tool()
@_safe
async def publish_carousel(image_urls: list[str], caption: str = "") -> dict:
    """Publish a carousel of 2-10 photos. Each link must be a public HTTPS JPEG."""
    if not 2 <= len(image_urls) <= 10:
        return {"published": False, "error": "A carousel needs 2 to 10 images."}
    children = []
    for url in image_urls:
        child = await _call(
            "POST", f"{IG_USER_ID}/media", image_url=url, is_carousel_item="true"
        )
        await _wait_until_ready(child["id"])
        children.append(child["id"])
    c = await _call(
        "POST", f"{IG_USER_ID}/media",
        media_type="CAROUSEL", children=",".join(children), caption=caption,
    )
    return await _publish(c["id"])


@server.tool()
@_safe
async def publish_reel(video_url: str, caption: str = "", share_to_feed: bool = True) -> dict:
    """Publish a Reel. video_url must be a public HTTPS link to an MP4 (H.264, up to 90 s)."""
    c = await _call(
        "POST", f"{IG_USER_ID}/media",
        media_type="REELS", video_url=video_url, caption=caption,
        share_to_feed="true" if share_to_feed else "false",
    )
    return await _publish(c["id"])


@server.tool()
@_safe
async def publish_story(media_url: str, is_video: bool = False) -> dict:
    """Publish a Story from a public HTTPS image (JPEG) or video (MP4) link."""
    key = "video_url" if is_video else "image_url"
    c = await _call(
        "POST", f"{IG_USER_ID}/media", media_type="STORIES", **{key: media_url}
    )
    return await _publish(c["id"])


if __name__ == "__main__":
    server.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        streamable_http_path=f"/{SECRET}/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
