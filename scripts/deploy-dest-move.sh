#!/bin/zsh
# Deploy this branch onto a running oMLX install and restart the server.
#
# Run on the machine that hosts oMLX (e.g. the M5 Max at .33):
#   git clone -q --depth 1 -b feat/download-dest-and-model-move \
#     https://github.com/kingwap99/omlx /tmp/omlx-deploy
#   zsh /tmp/omlx-deploy/scripts/deploy-dest-move.sh
#
# Finds the interpreter through the launchd job, backs up the two packages,
# copies the branch tree over them (compiled custom kernels stay untouched),
# restarts via pkill (KeepAlive brings it back) and smoke-tests the new route.
set -euo pipefail

OMLX_KEY=${OMLX_KEY:-sk-omlx-local}
PORT=${OMLX_PORT:-8258}

SCRIPT_DIR=${0:a:h}
REPO_ROOT=${SCRIPT_DIR:h}

# The job may live in the system directory or the user's LaunchAgents.
PLIST=""
for cand in /Library/LaunchDaemons/com.omlx.server.plist \
            "$HOME/Library/LaunchAgents/com.omlx.server.plist" \
            /Library/LaunchAgents/com.omlx.server.plist; do
  if [[ -f $cand ]]; then
    PLIST=$cand
    break
  fi
done
if [[ -z $PLIST ]]; then
  echo "error: com.omlx.server.plist not found" >&2
  exit 1
fi

ARG=$(plutil -convert json -o - "$PLIST" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["ProgramArguments"][0])')
if [[ ! -e $ARG ]]; then
  echo "error: oMLX launcher not found: $ARG" >&2
  exit 1
fi

# The DMG build runs a shim (often a symlink into oMLX.app) whose Resources
# directory holds the vendored omlx and omlx_web packages.
REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$ARG")
RES=$(dirname "$(dirname "$REAL")")/Resources
if [[ -f $RES/omlx/__init__.py ]]; then
  ROOT=$RES/omlx
  WEB=$RES/omlx_web
else
  # pip installs: the launcher is a console script or the interpreter itself.
  PY=$ARG
  if [[ $(head -c 2 "$PY" 2>/dev/null) == '#!' ]]; then
    PY=$(head -1 "$PY" | sed 's/^#!//; s/^[[:space:]]*//')
    PY=${PY#/usr/bin/env }
  fi
  if [[ ! -x $PY ]]; then
    echo "error: oMLX interpreter not executable: $PY" >&2
    exit 1
  fi
  ROOT=$("$PY" -c 'import omlx, os; print(os.path.dirname(omlx.__file__))')
  WEB=$("$PY" -c 'import omlx_web, os; print(os.path.dirname(omlx_web.__file__))')
fi
echo "installed at: $ROOT"
echo "web package:  $WEB"

TS=$(date +%Y%m%d-%H%M%S)
BACKUP="$HOME/omlx-deploy-backup-$TS.tgz"
tar czf "$BACKUP" -C / "${ROOT#/}" "${WEB#/}"
echo "backup: $BACKUP"

rsync -a --exclude '__pycache__' --exclude '*.pyc' \
  "$REPO_ROOT/omlx/" "$ROOT/"
rsync -a --exclude '__pycache__' --exclude '*.pyc' \
  "$REPO_ROOT/apps/omlx-web/omlx_web/" "$WEB/"
echo "files copied"

pkill -f omlx-server || true
echo "restart requested"

UP=""
for _ in {1..45}; do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    UP=1
    break
  fi
  sleep 2
done
if [[ -z $UP ]]; then
  echo "error: server did not come back; restore with: tar xzf $BACKUP -C /" >&2
  exit 1
fi

curl -sS "http://127.0.0.1:$PORT/health"
echo

CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $OMLX_KEY" -H 'Content-Type: application/json' \
  -d '{}' "http://127.0.0.1:$PORT/admin/api/hf/models/move")
case "$CODE" in
  401|422) echo "move endpoint: present (HTTP $CODE)" ;;
  *) echo "error: move endpoint missing (HTTP $CODE)" >&2; exit 1 ;;
esac
echo "done"
