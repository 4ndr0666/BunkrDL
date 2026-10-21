"""Main module to read Bunkr URLs from a file, and download from them.

This module manages the entire download process by leveraging asynchronous operations,
allowing for efficient handling of multiple URLs.

Usage:
    To run the module, execute the script directly. It will process URLs listed in
    'URLs.txt' and log the session activities in 'session_log.txt'.
"""

import asyncio
import logging
import sys
from argparse import Namespace

from downloader import parse_arguments, validate_and_download
from src.bunkr_utils import get_bunkr_status
from src.config import SESSION_LOG, URLS_FILE
from src.file_utils import (
    create_urls_file_backup,
    read_file,
    write_file,
    write_on_session_log,
)
from src.general_utils import check_python_version, clear_terminal
from src.managers.live_manager import initialize_managers


async def process_urls(urls: list[str], args: Namespace) -> None:
    """Validate and downloads items for a list of URLs.

    A failure on one URL is contained and logged (session log + live display)
    so the remaining URLs in the batch still run, matching the documented
    behavior of logging URLs that encounter errors.
    """
    # Fetch the status page off the event loop (one-shot, async-pure call).
    bunkr_status = await asyncio.to_thread(get_bunkr_status)
    live_manager = initialize_managers(disable_ui=args.disable_ui)

    with live_manager.live:
        for url in urls:
            try:
                await validate_and_download(bunkr_status, url, live_manager, args=args)

            # Contain per-URL failures: log with full traceback and continue
            # the batch. KeyboardInterrupt/SystemExit are not caught.
            except Exception as err:  # noqa: BLE001
                logging.exception("Error downloading from %s", url)
                write_on_session_log(
                    f"Error downloading from {url}: {err}",
                    outcome="BatchError",
                )
                live_manager.update_log(
                    event="URL error",
                    details=f"Error downloading from {url}: {err}. "
                    "Continuing with the remaining URLs.",
                )

        live_manager.stop()


async def main() -> None:
    """Run the script and process URLs."""
    # Clear the terminal and session log file
    clear_terminal()
    write_file(SESSION_LOG)

    # Check Python version and parse arguments
    check_python_version()
    args = parse_arguments(common_only=True)

    # Backup the URLs file
    create_urls_file_backup()

    # Read and process URLs, ignoring empty lines
    urls = [url.strip() for url in read_file(URLS_FILE) if url.strip()]
    await process_urls(urls, args)

    # Clear URLs file
    write_file(URLS_FILE)


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        sys.exit(1)
