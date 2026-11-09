#!/bin/zsh
# Deploy this branch onto a running oMLX install and restart the server.
#
# Run from the branch checkout on the machine that hosts oMLX:
#   zsh scripts/deploy-dest-move.sh
#
# DMG installs keep the app bundle under /Applications, which macOS guards
# with App Management (writes fail with EPERM, sudo included). The branch
# tree is therefore staged in ~/omlx-src and launchd is pointed at it.
set -euo pipefail

OMLX_KEY=${OMLX_KEY:-sk-omlx-local}
PORT=${OMLX_PORT:-8258}

SCRIPT_DIR=${0:a:h}
REPO_ROOT=${SCRIPT_DIR:h}

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
REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$ARG")
RES=$(dirname "$(dirname "$REAL")")/Resources

restart() {
  sudo /bin/launchctl bootout system/com.omlx.server 2>/dev/null || true
  sleep 2
  sudo /bin/launchctl bootstrap system "$PLIST"
}

verify() {
  local up=""
  for _ in {1..60}; do
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      up=1
      break
    fi
    sleep 2
  done
  if [[ -z $up ]]; then
    echo "error: server did not come back" >&2
    return 1
  fi
  curl -sS "http://127.0.0.1:$PORT/health"
  echo
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $OMLX_KEY" -H 'Content-Type: application/json' \
    -d '{}' "http://127.0.0.1:$PORT/admin/api/hf/models/move")
  case "$code" in
    401|422) echo "move endpoint: present (HTTP $code)" ;;
    *) echo "error: move endpoint missing (HTTP $code)" >&2; return 1 ;;
  esac
}

if [[ -f $RES/omlx/__init__.py ]]; then
  DST="$HOME/omlx-src"
  mkdir -p "$DST"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' \
    "$REPO_ROOT/omlx/" "$DST/omlx/"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' \
    "$REPO_ROOT/apps/omlx-web/omlx_web/" "$DST/omlx_web/"
  cat > "$DST/run-omlx.sh" <<'RUN'
#!/bin/sh
APP="/Applications/oMLX.app/Contents"
PYROOT="$APP/Resources/Python"
CPYTHON="$PYROOT/cpython-3.11"
export PYTHONHOME="$CPYTHON"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$HOME/omlx-src:$PYROOT/framework-mlx-base/lib/python3.11/site-packages"
exec "$CPYTHON/bin/python3" -m omlx.cli "$@"
RUN
  chmod +x "$DST/run-omlx.sh"
  echo "staged at: $DST"
  if [[ "$ARG" != "$DST/run-omlx.sh" ]]; then
    TS=$(date +%Y%m%d-%H%M%S)
    cp "$PLIST" "$HOME/com.omlx.server.plist.bak-$TS"
    sudo python3 - "$PLIST" "$DST/run-omlx.sh" <<'PY'
import plistlib, sys
path, launcher = sys.argv[1], sys.argv[2]
with open(path, "rb") as f:
    data = plistlib.load(f)
data["ProgramArguments"] = [launcher, "serve", "--port", "8258", "--host", "0.0.0.0"]
with open(path, "wb") as f:
    plistlib.dump(data, f, fmt=plistlib.FMT_XML)
PY
    echo "plist backup: $HOME/com.omlx.server.plist.bak-$TS"
  fi
else
  PY=$ARG
  if [[ $(head -c 2 "$PY" 2>/dev/null) == '#!' ]]; then
    PY=$(head -1 "$PY" | sed 's/^#!//; s/^[[:space:]]*//')
    PY=${PY#/usr/bin/env }
  fi
  ROOT=$("$PY" -c 'import omlx, os; print(os.path.dirname(omlx.__file__))')
  WEB=$("$PY" -c 'import omlx_web, os; print(os.path.dirname(omlx_web.__file__))')
  TS=$(date +%Y%m%d-%H%M%S)
  tar czf "$HOME/omlx-deploy-backup-$TS.tgz" -C / "${ROOT#/}" "${WEB#/}"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' \
    "$REPO_ROOT/omlx/" "$ROOT/"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' \
    "$REPO_ROOT/apps/omlx-web/omlx_web/" "$WEB/"
  echo "backup: $HOME/omlx-deploy-backup-$TS.tgz"
fi

restart
echo "restart requested"
verify
echo "done"
