# BunkrDL Capability Ledger — WO-001 Round (v1.2.2 → v1.2.3)

Baseline capability matrix with revision dispositions. Every baseline capability
is `Preserved` or `Upgraded`; none is `Regressed` or `Removed` (GUP superset rule).

| ID | Capability | Implementing unit(s) | Baseline status | Revision disposition |
|----|------------|----------------------|-----------------|----------------------|
| F-01 | Album download (multi-item, paginated) | `AlbumDownloader.download_album`, `execute_item_download`, `extract_all_album_item_pages` | Working | Upgraded: per-item exception containment + 7200s wall-clock ceiling; normal path identical |
| F-02 | Single-file download | `downloader.handle_download_process`, `MediaDownloader` | Working | Upgraded: unresolvable-link graceful skip; event-loop purity via `to_thread` |
| F-03 | Batch URL processing (URLs.txt) | `main.process_urls` | Working but aborts on first error | Upgraded: per-URL containment, batch continues, errors logged (README intent) |
| F-04 | Selective download (ignore/include lists) | `MediaDownloader._skip_file_download` | Working | Preserved (hash-identical logic) |
| F-05 | Retry with backoff (5 attempts) | `fetch_page`, `attempt_download`, `_retry_with_backoff`, `_fetch_page_with_retries` | Working | Preserved semantics; transport layer moved from requests to aiohttp with same retry/backoff shape |
| F-06 | Offline-subdomain marking | `_handle_request_exception`, `bunkr_utils` | Buggy: false positives on network errors | Upgraded: only confirmed HTTP 521/503 marks offline; network-layer errors retry |
| F-07 | Progress UI (rich Live) | `ProgressManager`, `LiveManager`, `LoggerTable` | Working | Preserved (hash-identical); negative-progress defect fixed in `save_file_with_progress` |
| F-08 | Session logging (session.log) | `write_on_session_log` | Working | Preserved; new skip/error events also logged |
| F-09 | Results summary | `SummaryManager`, `_log_results_summary` | Working | Preserved (hash-identical) |
| F-10 | Disk-space guard | `check_disk_space` | Working | Preserved |
| F-11 | Filename sanitization/truncation | `file_utils` | Working | Preserved; `format_directory_name` (name, None) cell fixed from crash to name |
| F-12 | Cloudflare email deobfuscation | `decrypt_cf_email` | Working | Preserved; now regression-tested (ascii + multibyte) |
| F-13 | Fallback domain retry (403 → bunkr.cr) | `fetch_page` + `replace_domain_with_fallback` | Working | Preserved (one-shot fallback, then FETCH_ERROR_MESSAGES handling) |
| F-14 | Direct-download fallback for landing-page-less assets | `get_api_response` + `get_download_response` | Working | Upgraded: no-base garbage URL eliminated; unsigned URL retained when unsigned path resolved |
| F-15 | Version/CLI surface | `config.parse_arguments`, `version.py` | Working | Preserved; version bumped 1.2.2 → 1.2.3 |
| F-16 | Crash resilience (malformed URLs/pages) | `check_url_type`, `extract_file_id`, `get_item_filename` | Buggy: 3 crash paths | Upgraded: clean exits / None contracts / graceful skips |
| F-17 | Partial-download `.temp` handling | `save_file_with_progress` | Working for known size; broken for unknown size | Upgraded: unknown-size completion, orphaned-temp cleanup on unexpected errors |
| F-18 | Certification/testing surface | none | ABSENT | NEW: 51-test offline suite + `tools/check.sh` gate printing `GUP VERIFICATION: CERTIFIED` |
