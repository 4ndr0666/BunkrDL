#!/usr/bin/env bash
# Deterministic GUP evidence generator for the WO-001 round (BunkrDL v1.2.3).
# Compares the round's baseline (a git ref, default: the round base pin passed
# as $1) with the current working tree via golden_unit_hash.py and writes the
# comparison manifest to gup/comparison.json. Idempotent: re-running against
# the same tree yields byte-identical evidence. Hand-authored review records
# (gup/semantic-review.json, gup/integration-closure.json) are NOT touched.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUP_HASH="${GUP_HASH:-/home/z/my-project/skills-extracted/golden-unit-protocol/standard/golden_unit_hash.py}"
BASELINE_REF="${1:-HEAD}"
OUT_DIR="$REPO_DIR/gup"

if [ ! -f "$GUP_HASH" ]; then
    echo "[evidence] golden_unit_hash.py not found at $GUP_HASH" >&2
    echo "[evidence] set GUP_HASH=<path> and re-run" >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM

mkdir -p "$WORK/baseline" "$OUT_DIR"
git -C "$REPO_DIR" archive "$BASELINE_REF" | tar -x -C "$WORK/baseline"

python3 - "$WORK" "$REPO_DIR" "$GUP_HASH" "$BASELINE_REF" \
    main.py downloader.py \
    src/config.py src/version.py src/__init__.py src/bunkr_utils.py \
    src/file_utils.py src/general_utils.py src/url_utils.py \
    src/crawlers/__init__.py src/crawlers/api_utils.py src/crawlers/crawler_utils.py \
    src/downloaders/__init__.py src/downloaders/album_downloader.py \
    src/downloaders/download_utils.py src/downloaders/media_downloader.py \
    src/managers/__init__.py src/managers/live_manager.py \
    src/managers/log_manager.py src/managers/progress_manager.py \
    src/managers/summary_manager.py \
    > "$OUT_DIR/comparison.json" <<'PY'
import json, subprocess, sys
from pathlib import Path

tmp, repo, gup_hash, baseline_ref = (
    sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4],
)
files = sys.argv[5:]

all_diff, baseline_manifest, candidate_manifest = [], [], []

for f in files:
    # NOTE: golden_unit_hash.py exits 1 when MISSING units are detected (the
    # expected signal for authorized removals); the JSON manifest is still
    # emitted on stdout, so check=False is the correct invocation here.
    out = subprocess.run(
        ["python3", gup_hash,
         "--baseline", str(Path(tmp) / "baseline" / f),
         "--candidate", str(repo / f)],
        capture_output=True, text=True, check=False,
    ).stdout
    data = json.loads(out)
    for u in data["baseline_manifest"]:
        baseline_manifest.append({"file": f, **u})
    for u in data["candidate_manifest"]:
        candidate_manifest.append({"file": f, **u})
    for d in data["diff"]:
        all_diff.append({"file": f, **d})

baseline_manifest.sort(key=lambda r: (r["file"], r["unit"]))
candidate_manifest.sort(key=lambda r: (r["file"], r["unit"]))
all_diff.sort(key=lambda r: (r["file"], r["unit"]))

counts = {}
for d in all_diff:
    counts[d["verdict"]] = counts.get(d["verdict"], 0) + 1

result = {
    "protocol": "GUP v7.0.1 / atomizer v2.2.0",
    "baseline_ref": subprocess.run(
        ["git", "-C", str(repo), "rev-parse", baseline_ref],
        capture_output=True, text=True, check=True,
    ).stdout.strip(),
    "files_compared": files,
    "unit_counts": counts,
    "diff": all_diff,
    "baseline_manifest": baseline_manifest,
    "candidate_manifest": candidate_manifest,
}
print(json.dumps(result, indent=2, sort_keys=False))
PY

echo "[evidence] wrote $OUT_DIR/comparison.json"
python3 -c "
import json
with open('$OUT_DIR/comparison.json') as fh:
    counts = json.load(fh)['unit_counts']
print('[evidence] unit counts:', counts)"
