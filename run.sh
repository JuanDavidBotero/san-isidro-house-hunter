#!/usr/bin/env bash
# One local radar pass: discover -> ingest -> report -> deliver.
#
# Mirrors .github/workflows/daily.yml so that what you debug locally is what runs on
# the schedule. Safe to run repeatedly: history makes a second pass in the same day
# classify everything OLD and fall through to "NO CHANGE".
#
# Usage:
#   ./run.sh                 full pass, delivers to whatever channels are configured
#   ./run.sh --dry-run       discover and report, resolve routing, send nothing
#
# Credentials are read from the environment. Put them in a local .env (gitignored)
# and `set -a; source .env; set +a` before running.

set -euo pipefail
cd "$(dirname "$0")"

# Load local configuration. A launchd job inherits almost no environment, so this is
# the only place credentials come from on a scheduled run.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# Pick an interpreter that can actually run this project.
# hunter.py needs zoneinfo (Python 3.9+). A machine with Anaconda on the PATH often
# resolves `python3` to an older base env, so probe rather than assume. Override with
# PYTHON=/path/to/python3 ./run.sh
pick_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import zoneinfo" 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  echo "error: no Python 3.9+ with zoneinfo found. Install one (brew install python@3.13)" >&2
  return 1
}
PYTHON="$(pick_python)" || exit 1
echo "using $PYTHON ($("$PYTHON" -c 'import sys;print(sys.version.split()[0])'))"
DRY_RUN=""
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN="--dry-run"
  echo "== dry run: nothing will be sent =="
fi

# If this is an Apple-managed machine, the Claude Code proxy hijacks all outbound
# traffic and every portal request dies with a connection error. Drop it for this run.
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy 2>/dev/null || true

TODAY="$($PYTHON -c 'import hunter; print(hunter.local_now().date().isoformat())')"

# A failed run must never look like a quiet market. `set -e` would abort silently from a
# launchd job whose output nobody reads, so any failure is announced on the urgent
# channel before exiting non-zero.
on_failure() {
  local stage="$1"
  echo "FAILED at: ${stage}" >&2
  if [ -z "$DRY_RUN" ]; then
    $PYTHON notify.py --force-telegram --text "⚠️ San Isidro radar FAILED on ${TODAY} at: ${stage}.
Today's silence is NOT evidence that the market was quiet.
Log: $(pwd)/logs/radar.log" >/dev/null 2>&1 || true
  fi
  exit 1
}

echo "== 1/4 init history =="
$PYTHON hunter.py init || on_failure "history init"

# Discovery. The agentic sweep is the primary path: MercadoLibre's API is closed to
# third-party apps for listing data (403 PA_UNAUTHORIZED_RESULT_FROM_POLICIES on search
# AND on single-item lookup, with both app- and user-context tokens), so fetch_ml.py is
# kept only for its OAuth diagnostics and the shared packet helpers.
echo "== 2/4 discover (public-web sweep) =="
DISCOVERY="$($PYTHON sweep.py ${SWEEP_ARGS:-})" || on_failure "discovery sweep"
echo "$DISCOVERY"
PACKET="$($PYTHON -c "import json,sys; print(json.loads(sys.argv[1])['packet'])" "$DISCOVERY")" \
  || on_failure "reading the packet path from the sweep result"

echo "== 3/4 ingest =="
if [ -s "$PACKET" ]; then
  # An ingest abort is the dangerous case: hunter.py rejects the whole packet on one
  # malformed record, so without this guard the run would continue to the report stage
  # and cheerfully deliver "NO CHANGE".
  $PYTHON hunter.py ingest --input "$PACKET" || on_failure "ingest of ${PACKET}"
else
  echo "empty packet; nothing to ingest"
fi

echo "== 4/4 report and deliver =="
mkdir -p reports
$PYTHON notify.py --save "reports/${TODAY}.md" $DRY_RUN || on_failure "report delivery"

echo
echo "== report =="
cat "reports/${TODAY}.md"
