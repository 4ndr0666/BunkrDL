"""Module that provides utilities for interacting with the Bunkr API."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

from src.config import BUNKR_API, DOWNLOAD_API, JS_VARS_COMP

if TYPE_CHECKING:
    import aiohttp
    from bs4 import BeautifulSoup


def unescape_js_path(value: str) -> str:
    """Unescape common JavaScript-escaped URL fragments."""
    return value.replace(r"\/", "/")


def extract_page_vars(soup: BeautifulSoup) -> dict[str, str]:
    """Extract runtime variables embedded in Bunkr inline JavaScript."""
    for script in soup.find_all("script"):
        if script.string and "var jsCDN" in script.string:
            matches = JS_VARS_COMP.findall(script.string)
            return {key: unescape_js_path(value).strip("\"'") for key, value in matches}
    return {}


def extract_file_id(soup: BeautifulSoup) -> str | None:
    """Extract the file identifier from the page's script tag."""
    script = soup.find("script")
    return script["data-file-id"] if script else None


async def get_download_response(session: aiohttp.ClientSession, file_id: str) -> str:
    """Retrieve the unsigned download URL for a file without landing page."""
    async with session.post(DOWNLOAD_API, json={"id": file_id}) as response:
        download_data = await response.json()

    media_base_url = download_data["mediafiles"]
    media_path = download_data["path"]
    parsed_url = urlparse(media_base_url)
    return urlunparse(parsed_url._replace(path=media_path))


async def get_api_response(
    session: aiohttp.ClientSession, item_url: str, soup: BeautifulSoup | None = None,
) -> str | None:
    """Fetch encryption data from the Bunkr API."""
    page_vars = extract_page_vars(soup)
    cdn_url = page_vars.get("jsCDN") if page_vars else None
    file_id = extract_file_id(soup) if not page_vars else None

    # Some assets (e.g. .zip, .wmv) are served without a landing page.
    # The direct download endpoint can be used as fallback.
    unsigned_media_url = (
        await get_download_response(session, file_id) if file_id else None
    )
    source_url = unsigned_media_url or item_url
    media_slug = PurePosixPath(urlparse(source_url).path).name
    media_path = urlparse(cdn_url).path if cdn_url else f"/storage/media/{media_slug}"

    async with session.get(BUNKR_API, params={"path": media_path}) as response:
        signature_data = await response.json()

    base_url = cdn_url if cdn_url else unsigned_media_url
    token = signature_data.get("token")
    expires_at = signature_data.get("ex")

    if token and expires_at:
        return f"{base_url}?token={token}&ex={expires_at}"

    return cdn_url
