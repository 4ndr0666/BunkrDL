"""Utilities for fetching pages, managing directories, and clearing the terminal.

It includes functions to handle common tasks such as sending HTTP requests, parsing
HTML, creating download directories, and clearing the terminal, making it reusable
across projects.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import random
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from bs4 import BeautifulSoup

from .config import (
    FETCH_ERROR_MESSAGES,
    GB,
    HEADERS,
    MIN_DISK_SPACE_GB,
    HTTPStatus,
)
from .url_utils import replace_domain_with_fallback

if TYPE_CHECKING:
    from src.managers.live_manager import LiveManager

# Per-session aiohttp timeout: 40 s connect + 40 s read, matching prior requests timeout.
_AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=40, sock_read=40)


async def fetch_page(url: str, retries: int = 5) -> BeautifulSoup | None:
    """Fetch the HTML content of a page at the given URL, with retry logic.

    Uses aiohttp so the async event loop is never blocked by a synchronous
    network call (fixes §4.5 chimera: blocking requests.get inside async def).
    A hard per-request timeout of 40 s is enforced via aiohttp.ClientTimeout
    (fixes §4.2: no infinite waits on hung connections).
    """
    tried_fallback = False

    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession(
                headers=HEADERS,
                timeout=_AIOHTTP_TIMEOUT,
            ) as session:
                async with session.get(url) as response:
                    status = response.status

                    if status == HTTPStatus.FORBIDDEN and not tried_fallback:
                        tried_fallback = True
                        url = replace_domain_with_fallback(url)
                        continue  # Retry immediately with .cr fallback domain

                    if status in FETCH_ERROR_MESSAGES:
                        log_message = FETCH_ERROR_MESSAGES[status].format(url=url)
                        logging.warning(log_message)
                        return None

                    response.raise_for_status()
                    content = await response.read()
                    # Use raw bytes to let BS4 detect encoding
                    return BeautifulSoup(content, "html.parser")

        # asyncio / aiohttp timeout — retryable with backoff
        except asyncio.TimeoutError:
            logging.warning("Fetch timed out (attempt %d/%d): %s", attempt + 1, retries, url)
            if attempt < retries - 1:
                delay = 2 ** (attempt + 1) + random.uniform(1, 2)  # noqa: S311
                await asyncio.sleep(delay)

        # Server closed connection before sending a response — retryable
        except aiohttp.ServerDisconnectedError:
            logging.warning(
                "Remote end closed connection without response (attempt %d/%d).",
                attempt + 1,
                retries,
            )
            if attempt < retries - 1:
                delay = 2 ** (attempt + 1) + random.uniform(1, 2)  # noqa: S311
                await asyncio.sleep(delay)

        # Catch-all for non-retryable aiohttp / network errors
        except aiohttp.ClientError:
            return None

    return None


def clear_terminal() -> None:
    """Clear the terminal screen based on the operating system."""
    commands = {
        "nt": "cls",       # Windows
        "posix": "clear",  # macOS and Linux
    }

    command = commands.get(os.name)
    if command:
        os.system(command)  # noqa: S605


def check_python_version(min_version: tuple[int, int] = (3, 10)) -> None:
    """Check if the current Python version meets the minimum requirement."""
    current_version = sys.version_info
    if current_version < min_version:
        log_message = (
            f"Python {current_version.major}.{current_version.minor} is not supported. "
            f" Python {min_version[0]}.{min_version[1]} or higher is required.",
        )
        logging.warning(log_message)
        sys.exit(1)


def get_root_path() -> str:
    """Return the filesystem root for the current working directory."""
    cwd = Path.cwd()
    if platform.system() == "Windows":
        return os.path.splitdrive(cwd)[0] + "\\"

    # Use actual working directory
    return str(cwd)


def check_disk_space(live_manager: LiveManager, custom_path: str | None = None) -> None:
    """Check if the available disk space is greater than or equal to `min_space` GB."""
    root_path = get_root_path() if custom_path is None else custom_path
    _, _, free_space = shutil.disk_usage(root_path)
    free_space_gb = free_space / GB

    if free_space_gb < MIN_DISK_SPACE_GB:
        live_manager.update_log(
            event="Insufficient disk space",
            details=f"Only {free_space_gb:.2f} GB available on {root_path}. "
            "The program has been stopped to prevent data loss.",
        )
        sys.exit(1)
