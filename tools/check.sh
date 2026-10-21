#!/usr/bin/env bash
# BunkrDL certification gate (WO-001 round).
# Compiles every source file, runs the full offline test suite, and prints
# the certification line the update kit asserts (G4). Exits non-zero on any
# failure. Requires: bash, python3. No network access needed.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

FAILURES=0

# 1. Byte-compilation of every Python file (syntax gate)
if ! python3 -m compileall -q main.py downloader.py src/ tests/; then
    echo "[GATE] compileall FAILED"
    FAILURES=$((FAILURES + 1))
fi

# 2. Import gate: every module must import cleanly
if ! python3 - <<'PY'
import sys
sys.path.insert(0, ".")
import main  # noqa: F401
import downloader  # noqa: F401
import src.bunkr_utils  # noqa: F401
import src.config  # noqa: F401
import src.file_utils  # noqa: F401
import src.general_utils  # noqa: F401
import src.url_utils  # noqa: F401
import src.version  # noqa: F401
import src.crawlers.api_utils  # noqa: F401
import src.crawlers.crawler_utils  # noqa: F401
import src.downloaders.album_downloader  # noqa: F401
import src.downloaders.download_utils  # noqa: F401
import src.downloaders.media_downloader  # noqa: F401
import src.managers.live_manager  # noqa: F401
import src.managers.log_manager  # noqa: F401
import src.managers.progress_manager  # noqa: F401
import src.managers.summary_manager  # noqa: F401
print("[GATE] imports OK")
PY
then
    echo "[GATE] import gate FAILED"
    FAILURES=$((FAILURES + 1))
fi

# 3. Full offline test suite
if python3 -m unittest discover -s tests -v 2>&1 | tee /tmp/bunkrdl-gate-test.log; then
    :
else
    echo "[GATE] test suite FAILED (see /tmp/bunkrdl-gate-test.log)"
    FAILURES=$((FAILURES + 1))
fi

# 4. Version identity
python3 - <<'PY'
from src.version import __version__
print(f"[GATE] version: {__version__}")
PY

if [ "$FAILURES" -eq 0 ]; then
    echo "GUP VERIFICATION: CERTIFIED"
    exit 0
fi

echo "GUP VERIFICATION: FAILED ($FAILURES gate failure(s))"
exit 1
