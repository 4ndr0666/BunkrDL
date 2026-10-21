"""Certification test suite for BunkrDL v1.2.3 (WO-001 bug-fix round).

Runs fully offline: every network touchpoint is mocked. Covers the units
changed by the round plus regression coverage of the baseline guarantees
they must preserve (GUP superset rule).

Run from the repository root:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup  # noqa: E402

from src.config import (  # noqa: E402
    AlbumInfo,
    DownloadInfo,
    HTTPStatus,
    SessionInfo,
    SkippedReason,
    TaskResult,
)
from src.crawlers.api_utils import extract_file_id, get_api_response  # noqa: E402
from src.crawlers.crawler_utils import (  # noqa: E402
    decrypt_cf_email,
    format_item_filename,
    get_download_info,
    get_item_filename,
)
from src.downloaders.album_downloader import AlbumDownloader  # noqa: E402
from src.downloaders.download_utils import (  # noqa: E402
    get_chunk_size,
    save_file_with_progress,
)
from src.downloaders.media_downloader import MediaDownloader  # noqa: E402
from src.file_utils import (  # noqa: E402
    format_directory_name,
    remove_invalid_characters,
    sanitize_directory_name,
    truncate_filename,
)
from src.general_utils import fetch_page  # noqa: E402
from src.managers.live_manager import initialize_managers  # noqa: E402
from src.managers.summary_manager import SummaryManager  # noqa: E402
from src.url_utils import (  # noqa: E402
    check_url_type,
    get_album_id,
    get_url_based_filename,
    replace_domain_with_fallback,
)
from src.version import __version__  # noqa: E402

logging.disable(logging.CRITICAL)


def run_async(coro):
    """Drive a coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


def cf_obfuscate(email: str, key: int = 0x75) -> str:
    """Produce a Cloudflare-style data-cfemail hex payload (byte-level XOR)."""
    raw = email.encode("utf-8")
    return bytes([key] + [byte ^ key for byte in raw]).hex()


class FakeAiohttpResponse:
    """Minimal stand-in for aiohttp.ClientResponse."""

    def __init__(self, status=200, body=b"", json_data=None):
        self.status = status
        self._body = body
        self._json = json_data

    async def text(self):
        return self._body.decode("utf-8", errors="replace")

    async def read(self):
        return self._body

    async def json(self):
        if self._json is None:
            raise ValueError("No JSON payload")
        return self._json

    def raise_for_status(self):
        if self.status >= 400:
            import aiohttp

            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status,
            )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeAiohttpSession:
    """Minimal stand-in for aiohttp.ClientSession with scripted responses."""

    def __init__(self, responses):
        # responses: list of FakeAiohttpResponse or Exception instances
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append(("GET", url, params))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, **kwargs):
        self.calls.append(("POST", url, json))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeRequestsResponse:
    """Minimal stand-in for a streaming requests.Response."""

    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {}

    def iter_content(self, chunk_size=None):
        yield from self._chunks


class TestFormatDirectoryName(unittest.TestCase):
    """Round fix: the (name, None) case previously returned None."""

    def test_truth_table(self):
        self.assertIsNone(format_directory_name(None, None))
        self.assertEqual(format_directory_name(None, "xyz"), "xyz")
        self.assertEqual(format_directory_name("name", None), "name")
        self.assertEqual(format_directory_name("name", "id"), "name (id)")


class TestCheckUrlType(unittest.TestCase):
    """Round fix: IndexError previously fell through to UnboundLocalError."""

    def test_album_url(self):
        self.assertTrue(check_url_type("https://bunkr.si/a/ABC123"))

    def test_file_url(self):
        self.assertFalse(check_url_type("https://bunkr.si/f/ABC123"))

    def test_too_short_url_exits_cleanly(self):
        with self.assertRaises(SystemExit) as ctx:
            check_url_type("https:")
        self.assertEqual(ctx.exception.code, 1)

    def test_unknown_type_segment_exits_cleanly(self):
        with self.assertRaises(SystemExit) as ctx:
            check_url_type("https://bunkr.si/z/ABC123")
        self.assertEqual(ctx.exception.code, 1)


class TestExtractFileId(unittest.TestCase):
    """Round fix: missing attribute previously raised KeyError."""

    def test_with_attribute(self):
        soup = BeautifulSoup(
            '<script data-file-id="abc123"></script>', "html.parser",
        )
        self.assertEqual(extract_file_id(soup), "abc123")

    def test_without_attribute_returns_none(self):
        soup = BeautifulSoup("<script>var x = 1;</script>", "html.parser")
        self.assertIsNone(extract_file_id(soup))

    def test_without_script_returns_none(self):
        soup = BeautifulSoup("<p>nothing</p>", "html.parser")
        self.assertIsNone(extract_file_id(soup))


class TestGetItemFilename(unittest.TestCase):
    """Round fix: missing container previously raised AttributeError."""

    H1_HTML = (
        '<h1 class="text-subs font-semibold text-base sm:text-lg truncate">'
        "video.mp4</h1>"
    )

    def test_extracts_filename(self):
        soup = BeautifulSoup(self.H1_HTML, "html.parser")
        self.assertEqual(get_item_filename(soup), "video.mp4")

    def test_missing_container_returns_none(self):
        soup = BeautifulSoup("<p>no heading</p>", "html.parser")
        self.assertIsNone(get_item_filename(soup))

    def test_decrypts_cf_email(self):
        # Cloudflare-style obfuscation of "user@example.com" (byte XOR, key 0x75)
        cf_hex = cf_obfuscate("user@example.com")
        html = (
            '<h1 class="text-subs font-semibold text-base sm:text-lg truncate">'
            f'<a class="__cf_email__" data-cfemail="{cf_hex}">[email]</a></h1>'
        )
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(get_item_filename(soup), "user@example.com")

    def test_decrypt_cf_email_multibyte(self):
        self.assertEqual(decrypt_cf_email(cf_obfuscate("andr\u00e9")), "andr\u00e9")

    def test_decrypt_cf_email_ascii(self):
        self.assertEqual(decrypt_cf_email(cf_obfuscate("user@example.com")), "user@example.com")


class TestGetApiResponse(unittest.TestCase):
    """Round fix: no-base token path previously returned 'None?token=...'."""

    def test_signed_url_from_cdn(self):
        session = FakeAiohttpSession(
            [FakeAiohttpResponse(200, json_data={"token": "tok", "ex": "42"})],
        )
        soup = BeautifulSoup(
            '<script>var jsCDN = "https://cdn.example.com/path/file.mp4";</script>',
            "html.parser",
        )
        result = run_async(get_api_response(session, "https://bunkr.si/f/x", soup))
        self.assertEqual(
            result, "https://cdn.example.com/path/file.mp4?token=tok&ex=42",
        )

    def test_no_base_no_garbage_none_url(self):
        session = FakeAiohttpSession(
            [FakeAiohttpResponse(200, json_data={"token": "tok", "ex": "42"})],
        )
        soup = BeautifulSoup("<script>var n = 1;</script>", "html.parser")
        with patch("src.crawlers.api_utils.extract_file_id", return_value=None):
            result = run_async(get_api_response(session, "https://bunkr.si/f/x", soup))
        self.assertIsNone(result)

    def test_unsigned_fallback_returned_when_no_token(self):
        session = FakeAiohttpSession([
            FakeAiohttpResponse(200, json_data={
                "mediafiles": "https://m.example.com", "path": "/f/abc",
            }),
            FakeAiohttpResponse(200, json_data={}),
        ])
        soup = BeautifulSoup(
            '<script data-file-id="abc"></script>', "html.parser",
        )
        result = run_async(get_api_response(session, "https://bunkr.si/f/x", soup))
        self.assertEqual(result, "https://m.example.com/f/abc")


class TestGetDownloadInfo(unittest.TestCase):
    """Round fix: unresolvable items now return (None, None) gracefully."""

    def test_unresolvable_link_returns_none_pair(self):
        soup = BeautifulSoup("<script>var n = 1;</script>", "html.parser")
        link, name = run_async(get_download_info("https://bunkr.si/f/x", soup))
        self.assertIsNone(link)
        self.assertIsNone(name)

    def test_unparsable_filename_returns_none_pair(self):
        # Link resolves via jsCDN, but the page carries no filename container.
        html = (
            '<script>var jsCDN = "https://cdn.example.com/p/file.mp4";</script>'
            "<p>no h1</p>"
        )
        soup = BeautifulSoup(html, "html.parser")
        link, name = run_async(get_download_info("https://bunkr.si/f/x", soup))
        self.assertIsNone(link)
        self.assertIsNone(name)

    def test_resolvable_item(self):
        with patch("src.crawlers.crawler_utils.get_item_download_link") as link_mock:
            link_mock.return_value = "https://cdn.example.com/p/file.mp4?t=1"
            soup = BeautifulSoup(TestGetItemFilename.H1_HTML, "html.parser")
            link, name = run_async(get_download_info("https://bunkr.si/f/x", soup))
        self.assertEqual(link, "https://cdn.example.com/p/file.mp4?t=1")
        # video.mp4 (h1) + file.mp4 (URL) -> combined base with hyphen
        self.assertEqual(name, "video-file.mp4")


class TestFetchPage(unittest.TestCase):
    """Behavioral proof that the inlined handle_response logic is preserved."""

    def _page(self, status, body=b"<html><body>ok</body></html>"):
        return FakeAiohttpResponse(status, body)

    def test_success_returns_soup(self):
        with patch("src.general_utils.aiohttp.ClientSession") as session_cls:
            session_cls.return_value = FakeAiohttpSession([self._page(200)])
            soup = run_async(fetch_page("https://bunkr.si/a/x"))
        self.assertIsNotNone(soup)
        self.assertEqual(soup.find("body").get_text(), "ok")

    def test_fetch_error_status_returns_none(self):
        for status in (HTTPStatus.INTERNAL_ERROR, HTTPStatus.BAD_GATEWAY):
            with patch("src.general_utils.aiohttp.ClientSession") as session_cls:
                session_cls.return_value = FakeAiohttpSession([self._page(status)])
                soup = run_async(fetch_page("https://bunkr.si/a/x"))
            self.assertIsNone(soup)

    def test_forbidden_falls_back_domain_then_none(self):
        responses = [
            self._page(HTTPStatus.FORBIDDEN),
            self._page(HTTPStatus.FORBIDDEN),
        ]
        with patch("src.general_utils.aiohttp.ClientSession") as session_cls:
            session_cls.return_value = FakeAiohttpSession(responses)
            soup = run_async(fetch_page("https://bunkr.si/a/x"))
        self.assertIsNone(soup)
        session = session_cls.return_value
        second_url = session.calls[1][1]
        self.assertIn("bunkr.cr", second_url)

    def test_client_error_returns_none(self):
        import aiohttp

        with patch("src.general_utils.aiohttp.ClientSession") as session_cls:
            session_cls.return_value = FakeAiohttpSession(
                [aiohttp.ClientConnectionError("boom")],
            )
            soup = run_async(fetch_page("https://bunkr.si/a/x"))
        self.assertIsNone(soup)

    def test_server_disconnect_retries_then_none(self):
        import aiohttp

        responses = [aiohttp.ServerDisconnectedError()] * 5
        with patch("src.general_utils.aiohttp.ClientSession") as session_cls:
            session_cls.return_value = FakeAiohttpSession(responses)
            with patch("src.general_utils.asyncio.sleep", new=AsyncMock()):
                soup = run_async(fetch_page("https://bunkr.si/a/x"))
        self.assertIsNone(soup)


class TestSaveFileWithProgress(unittest.TestCase):
    """Round fix: unknown Content-Length no longer breaks completion."""

    def setUp(self):
        self.manager = initialize_managers(disable_ui=True)
        self.pm = self.manager.progress_manager
        # Register real tasks so progress updates target valid task IDs,
        # mirroring the production wiring (add_overall_task -> add_task).
        self.pm.add_overall_task("album", num_tasks=1)
        self.task_id = self.pm.add_task()

    def tearDown(self):
        for stem in (
            "bunkr-test-file",
            "bunkr-test-partial",
            "bunkr-test-unknown",
            "bunkr-test-garbage",
            "bunkr-test-crash",
            "bunkr-test-chunked",
        ):
            # with_suffix(".temp") REPLACES the final suffix: file.bin -> file.temp
            Path(f"/tmp/{stem}.bin").unlink(missing_ok=True)
            Path(f"/tmp/{stem}.temp").unlink(missing_ok=True)
            Path(f"/tmp/{stem}.bin.temp").unlink(missing_ok=True)

    def test_known_size_complete(self):
        response = FakeRequestsResponse([b"ab", b"cd"], {"Content-Length": "4"})
        with patch.object(self.pm, "update_task", wraps=self.pm.update_task) as update:
            incomplete = save_file_with_progress(
                response, "/tmp/bunkr-test-file.bin", self.task_id, self.pm,
            )
        self.assertFalse(incomplete)
        self.assertTrue(Path("/tmp/bunkr-test-file.bin").exists())
        self.assertEqual(Path("/tmp/bunkr-test-file.bin").read_bytes(), b"abcd")
        self.assertFalse(Path("/tmp/bunkr-test-file.temp").exists())
        self.assertEqual(update.call_count, 2)

    def test_known_size_partial_keeps_temp(self):
        response = FakeRequestsResponse([b"ab"], {"Content-Length": "4"})
        incomplete = save_file_with_progress(
            response, "/tmp/bunkr-test-partial.bin", self.task_id, self.pm,
        )
        self.assertTrue(incomplete)
        # with_suffix(".temp") replaces the ".bin" suffix
        self.assertTrue(Path("/tmp/bunkr-test-partial.temp").exists())
        self.assertFalse(Path("/tmp/bunkr-test-partial.bin").exists())

    def test_unknown_size_clean_stream_is_complete(self):
        response = FakeRequestsResponse([b"ab", b"cd"], {})
        with patch.object(self.pm, "update_task") as update:
            incomplete = save_file_with_progress(
                response, "/tmp/bunkr-test-unknown.bin", self.task_id, self.pm,
            )
        self.assertFalse(incomplete)
        self.assertTrue(Path("/tmp/bunkr-test-unknown.bin").exists())
        self.assertEqual(Path("/tmp/bunkr-test-unknown.bin").read_bytes(), b"abcd")
        update.assert_not_called()

    def test_invalid_content_length_treated_as_unknown(self):
        response = FakeRequestsResponse([b"ab"], {"Content-Length": "garbage"})
        incomplete = save_file_with_progress(
            response, "/tmp/bunkr-test-garbage.bin", self.task_id, self.pm,
        )
        self.assertFalse(incomplete)
        self.assertEqual(Path("/tmp/bunkr-test-garbage.bin").read_bytes(), b"ab")

    def test_unexpected_exception_cleans_temp_file(self):
        response = FakeRequestsResponse([b"ab"], {"Content-Length": "4"})

        def exploding_iter(chunk_size=None):
            yield b"ab"
            raise OSError("disk exploded")

        response.iter_content = exploding_iter
        with self.assertRaises(OSError):
            save_file_with_progress(
                response, "/tmp/bunkr-test-crash.bin", self.task_id, self.pm,
            )
        self.assertFalse(Path("/tmp/bunkr-test-crash.temp").exists())

    def test_chunked_encoding_error_marks_partial(self):
        from requests.exceptions import ChunkedEncodingError

        response = FakeRequestsResponse([b"ab"], {"Content-Length": "4"})

        def broken_iter(chunk_size=None):
            yield b"ab"
            raise ChunkedEncodingError("connection reset")

        response.iter_content = broken_iter
        incomplete = save_file_with_progress(
            response, "/tmp/bunkr-test-chunked.bin", self.task_id, self.pm,
        )
        self.assertTrue(incomplete)

    def test_get_chunk_size_thresholds(self):
        from src.config import KB, MB

        self.assertEqual(get_chunk_size(100 * KB), 32 * KB)
        self.assertEqual(get_chunk_size(5 * MB), 128 * KB)
        self.assertEqual(get_chunk_size(2 * 1024 * MB), 16 * 1024 * 1024)


class TestHandleRequestException(unittest.TestCase):
    """Round fix: response-less errors retry instead of marking offline."""

    def _downloader(self, retries=3):
        manager = initialize_managers(disable_ui=True)
        download_info = DownloadInfo(
            item_url="https://bunkr.si/f/x",
            download_link="https://kev.bunkr.example/f/file.mp4",
            filename="file.mp4",
            task=0,
        )
        session_info = SessionInfo(
            args=Namespace(ignore=None, include=None),
            bunkr_status={},
            download_path="/tmp",
        )
        return MediaDownloader(
            session_info=session_info,
            download_info=download_info,
            live_manager=manager,
            retries=retries,
        )

    def _response_error(self, status=None):
        error = MagicMock()
        error.response = None if status is None else MagicMock(status_code=status)
        return error

    def test_network_error_is_retried_not_offline(self):
        downloader = self._downloader()
        with patch.object(
            downloader, "_retry_with_backoff", return_value=True,
        ) as retry:
            should_retry = downloader._handle_request_exception(
                self._response_error(None), 0,
            )
        self.assertTrue(should_retry)
        retry.assert_called_once_with(0, event="Network error")
        self.assertEqual(downloader.session_info.bunkr_status, {})

    def test_server_down_marks_offline(self):
        downloader = self._downloader()
        should_retry = downloader._handle_request_exception(
            self._response_error(HTTPStatus.SERVER_DOWN), 0,
        )
        self.assertFalse(should_retry)
        self.assertIn("kev", downloader.session_info.bunkr_status)

    def test_service_unavailable_marks_offline(self):
        downloader = self._downloader()
        should_retry = downloader._handle_request_exception(
            self._response_error(HTTPStatus.SERVICE_UNAVAILABLE), 0,
        )
        self.assertFalse(should_retry)
        self.assertIn("kev", downloader.session_info.bunkr_status)

    def test_too_many_requests_retries(self):
        downloader = self._downloader()
        with patch.object(
            downloader, "_retry_with_backoff", return_value=True,
        ) as retry:
            should_retry = downloader._handle_request_exception(
                self._response_error(HTTPStatus.TOO_MANY_REQUESTS), 0,
            )
        self.assertTrue(should_retry)
        retry.assert_called_once_with(0, event="Retrying download")

    def test_bad_gateway_stops_retrying(self):
        downloader = self._downloader()
        should_retry = downloader._handle_request_exception(
            self._response_error(HTTPStatus.BAD_GATEWAY), 0,
        )
        self.assertFalse(should_retry)
        self.assertEqual(downloader.retries, 1)

    def test_other_status_stops(self):
        downloader = self._downloader()
        should_retry = downloader._handle_request_exception(
            self._response_error(HTTPStatus.FORBIDDEN), 0,
        )
        self.assertFalse(should_retry)


class TestAlbumDownloaderGather(unittest.TestCase):
    """Round hardening: gather collects item exceptions and enforces a ceiling."""

    def _album_downloader(self, item_pages):
        # disable_ui=False so LoggerTable.log populates row_buffer, which the
        # assertions read; Live.update is safe pre-start (stores renderable).
        manager = initialize_managers(disable_ui=False)
        session_info = SessionInfo(
            args=Namespace(ignore=None, include=None),
            bunkr_status={},
            download_path="/tmp",
        )
        album_info = AlbumInfo(album_id="album", item_pages=item_pages)
        return AlbumDownloader(
            session_info=session_info,
            album_info=album_info,
            live_manager=manager,
        )

    def test_item_exception_is_collected_and_logged(self):
        downloader = self._album_downloader(
            ["https://bunkr.si/f/a", "https://bunkr.si/f/b"],
        )

        async def failing(item_page, current_task, semaphore, max_retries):
            if item_page.endswith("/a"):
                raise RuntimeError("boom on a")
            return None

        with patch.object(downloader, "execute_item_download", side_effect=failing):
            with patch.object(
                downloader, "_process_failed_downloads",
            ) as process_failed:
                run_async(downloader.download_album())

        events = [
            row[1] for row in downloader.live_manager.logger_table.row_buffer
        ]
        self.assertIn("Item error", events)
        details = [row[2] for row in downloader.live_manager.logger_table.row_buffer
                   if row[1] == "Item error"]
        self.assertTrue(any("boom on a" in detail for detail in details))
        process_failed.assert_not_called()

    def test_timeout_cancels_and_logs(self):
        import src.downloaders.album_downloader as album_module

        downloader = self._album_downloader(["https://bunkr.si/f/a"])
        original_timeout = album_module._ALBUM_GATHER_TIMEOUT

        async def hanging(item_page, current_task, semaphore, max_retries):
            await asyncio.sleep(60)

        try:
            album_module._ALBUM_GATHER_TIMEOUT = 0.1
            with patch.object(
                downloader, "execute_item_download", side_effect=hanging,
            ):
                run_async(downloader.download_album())
        finally:
            album_module._ALBUM_GATHER_TIMEOUT = original_timeout

        events = [
            row[1] for row in downloader.live_manager.logger_table.row_buffer
        ]
        self.assertIn("Album timeout", events)


class TestUrlUtils(unittest.TestCase):
    """Baseline regression coverage for URL handling."""

    def test_get_album_id(self):
        self.assertEqual(get_album_id("https://bunkr.si/a/ABC123/"), "ABC123")

    def test_get_url_based_filename(self):
        self.assertEqual(
            get_url_based_filename("https://cdn.example.com/d/file.mp4?x=1"),
            "file.mp4",
        )

    def test_replace_domain_with_fallback(self):
        self.assertEqual(
            replace_domain_with_fallback("https://bunkr.si/a/ABC"),
            "https://bunkr.cr/a/ABC",
        )

    def test_sanitize_directory_name_posix(self):
        with patch("src.file_utils.os.name", "posix"):
            self.assertEqual(sanitize_directory_name("a/b:c"), "a_b_c")

    def test_remove_invalid_characters(self):
        self.assertEqual(remove_invalid_characters('a<b>:"c/d\\e|f?g*h'), "abcdefgh")


class TestFileUtils(unittest.TestCase):
    """Baseline regression coverage for file name handling."""

    def test_truncate_filename(self):
        self.assertEqual(truncate_filename("file.mp4"), "file.mp4")
        long_name = "x" * 300 + ".mp4"
        result = truncate_filename(long_name)
        self.assertLessEqual(len(Path(result).name), 120)

    def test_format_item_filename_identical(self):
        self.assertEqual(format_item_filename("a.mp4", "a.mp4"), "a.mp4")

    def test_format_item_filename_nested_base(self):
        self.assertEqual(format_item_filename("base.mp4", "base-1.mp4"), "base-1.mp4")

    def test_format_item_filename_combined(self):
        self.assertEqual(
            format_item_filename("orig.mp4", "url.mp4"), "orig-url.mp4",
        )


class TestSummaryManager(unittest.TestCase):
    """Baseline regression coverage for the results summary."""

    def test_counters(self):
        manager = SummaryManager()
        manager.update_result(SkippedReason.ALREADY_DOWNLOADED)
        manager.update_result(SkippedReason.ALREADY_DOWNLOADED)
        manager.update_result(SkippedReason.IGNORE_LIST)
        self.assertEqual(
            manager.get_result_count(TaskResult.SKIPPED), 3,
        )
        self.assertEqual(
            manager.get_result_count(
                TaskResult.SKIPPED, SkippedReason.ALREADY_DOWNLOADED,
            ),
            2,
        )


class TestBatchContainment(unittest.TestCase):
    """Round fix: one bad URL no longer aborts the batch."""

    def test_remaining_urls_still_processed(self):
        import main as main_module

        processed = []

        async def fake_validate(bunkr_status, url, live_manager, args=None):
            if url == "bad":
                raise RuntimeError("boom")
            processed.append(url)

        manager = initialize_managers(disable_ui=True)
        urls = ["u1", "bad", "u2"]

        async def run():
            with patch.object(main_module, "validate_and_download", fake_validate):
                with patch.object(
                    main_module, "get_bunkr_status", return_value={},
                ):
                    with patch.object(
                        main_module, "initialize_managers", return_value=manager,
                    ):
                        await main_module.process_urls(
                            urls, Namespace(disable_ui=True),
                        )

        run_async(run())
        self.assertEqual(processed, ["u1", "u2"])


class TestVersion(unittest.TestCase):
    """The round is v1.2.3."""

    def test_version_bumped(self):
        self.assertEqual(__version__, "1.2.3")


if __name__ == "__main__":
    unittest.main()
