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

echo "== 1/4 init history =="
$PYTHON hunter.py init

# Discovery. The agentic sweep is the primary path: MercadoLibre's search API is closed
# to third-party apps (a valid user token is still refused by their PolicyAgent), so the
# ML client is kept only for the --probe/--auth-check diagnostics and for future
# enrichment if that ever changes.
echo "== 2/4 discover (public-web sweep) =="
DISCOVERY="$($PYTHON sweep.py ${SWEEP_ARGS:-})"
echo "$DISCOVERY"
PACKET="$($PYTHON -c "import json,sys; print(json.loads(sys.argv[1])['packet'])" "$DISCOVERY")"

echo "== 3/4 ingest =="
if [ -s "$PACKET" ]; then
  $PYTHON hunter.py ingest --input "$PACKET"
else
  echo "empty packet; nothing to ingest"
fi

echo "== 4/4 report and deliver =="
mkdir -p reports
$PYTHON notify.py --save "reports/${TODAY}.md" $DRY_RUN

echo
echo "== report =="
cat "reports/${TODAY}.md"
