"""Module that facilitates the downloading of entire Bunkr albums.

This module provides features for managing progress, handling failed downloads, and
integrating with live task displays.
"""

import asyncio
from asyncio import Semaphore

from bs4 import BeautifulSoup

from src.config import MAX_RETRIES, MAX_WORKERS, AlbumInfo, DownloadInfo, SessionInfo
from src.crawlers.crawler_utils import get_download_info
from src.general_utils import fetch_page
from src.managers.live_manager import LiveManager

from .media_downloader import MediaDownloader

# Hard wall-clock ceiling for a single album gather pass (seconds).
# Prevents a permanently hung item page from blocking the orchestrator indefinitely
# (fixes §4.2: no top-level timeout on aggregate gather).
_ALBUM_GATHER_TIMEOUT = 7200  # 2 hours


class AlbumDownloader:
    """Manage the downloading of entire Bunkr albums."""

    def __init__(
        self,
        session_info: SessionInfo,
        album_info: AlbumInfo,
        live_manager: LiveManager,
    ) -> None:
        """Initialize the AlbumDownloader instance."""
        self.session_info = session_info
        self.album_info = album_info
        self.live_manager = live_manager
        self.failed_downloads = []

    async def execute_item_download(
        self,
        item_page: str,
        current_task: int,
        semaphore: Semaphore,
        max_retries: int,
    ) -> None:
        """Handle the download of an individual item in the album."""
        async with semaphore:
            task = self.live_manager.add_task(current_task=current_task)

            # Process the download of an item
            item_soup = await self._fetch_page_with_retries(item_page)
            item_download_link, item_filename = await get_download_info(
                item_page,
                item_soup,
            )

            # Download item
            if item_download_link:
                media_downloader = MediaDownloader(
                    session_info=self.session_info,
                    download_info=DownloadInfo(
                        item_url=item_page,
                        download_link=item_download_link,
                        filename=item_filename,
                        task=task,
                    ),
                    live_manager=self.live_manager,
                    retries=max_retries,
                )

                failed_download = await asyncio.to_thread(media_downloader.download)
                if failed_download:
                    self.failed_downloads.append(failed_download)

            else:
                # No download link could be resolved for this item: log and skip
                # instead of failing silently.
                self.live_manager.update_log(
                    event="Item skipped",
                    details=f"No download link could be resolved for: {item_page}",
                )

    async def download_album(
        self,
        max_workers: int = MAX_WORKERS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        """Handle the album download.

        Wraps the aggregate gather in asyncio.wait_for with a wall-clock ceiling
        (§4.2).  Uses return_exceptions=True so a single item failure is collected
        rather than propagating immediately and leaving sibling tasks unaccounted for;
        any non-None exception results are logged (§4.4 sibling cancellation is
        handled implicitly: the semaphore bounds concurrency, and wait_for cancels
        the gather future — and all pending tasks within it — on timeout).
        """
        num_tasks = len(self.album_info.item_pages)
        self.live_manager.add_overall_task(
            description=self.album_info.album_id,
            num_tasks=num_tasks,
        )

        semaphore = asyncio.Semaphore(max_workers)
        coros = [
            self.execute_item_download(item_page, current_task, semaphore, max_retries)
            for current_task, item_page in enumerate(self.album_info.item_pages)
        ]

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*coros, return_exceptions=True),
                timeout=_ALBUM_GATHER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self.live_manager.update_log(
                event="Album timeout",
                details=f"Album '{self.album_info.album_id}' exceeded the "
                f"{_ALBUM_GATHER_TIMEOUT}s wall-clock limit and was cancelled.",
            )
            return

        # Log any item-level exceptions that were collected rather than raised
        for result in results:
            if isinstance(result, BaseException):
                self.live_manager.update_log(
                    event="Item error",
                    details=str(result),
                )

        # If there are failed downloads, process them after all downloads are complete
        if self.failed_downloads:
            await self._process_failed_downloads()

    # Private methods
    async def _fetch_page_with_retries(
        self,
        item_page: str,
        max_retries: int = MAX_RETRIES,
        base_delay: float = 1.5,
    ) -> BeautifulSoup:
        """Try to fetch a page multiple times with progressive backoff."""
        item_soup = None
        for attempt in range(1, max_retries + 1):
            item_soup = await fetch_page(item_page)
            if item_soup is not None:
                return item_soup

            self.live_manager.update_log(
                event="Fetch retry",
                details=f"Attempt {attempt}/{max_retries} failed for: {item_page}",
            )
            if attempt < max_retries:
                await asyncio.sleep(base_delay * attempt)

        # All attempts failed
        self.live_manager.update_log(
            event="Fetch failed",
            details=f"Unable to load page after {max_retries} attempts: {item_page}",
        )
        error_message = f"Failed to load page: {item_page}"
        raise RuntimeError(error_message)

    async def _retry_failed_download(self, failed_download_info: DownloadInfo) -> None:
        """Handle failed downloads and retries them."""
        media_downloader = MediaDownloader(
            session_info=self.session_info,
            download_info=failed_download_info,
            live_manager=self.live_manager,
            retries=1,  # Retry once for failed downloads
        )
        # Run the synchronous download function in a separate thread
        await asyncio.to_thread(media_downloader.download)

    async def _process_failed_downloads(self) -> None:
        """Process any failed downloads after the initial attempt."""
        for data in self.failed_downloads:
            failed_download_info = DownloadInfo(
                item_url=data["item_url"],
                download_link=data["download_link"],
                filename=data["filename"],
                task=data["id"],
            )
            await self._retry_failed_download(failed_download_info)

        self.failed_downloads.clear()
