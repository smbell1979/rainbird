#!/usr/bin/env bash
# Deploy to the NUC (amethyst) as systemd user services. Run from Git Bash on this PC.
#
#   deploy/deploy.sh                      dry run: print what would happen, touch nothing
#   deploy/deploy.sh --apply --tz ZONE    copy, build, run tests on the NUC; does NOT start anything
#   deploy/deploy.sh --start              enable and start both services (this begins controlling
#                                         the sprinklers: the rain-delay fallback engages at once)
#
# --apply and --start are separate on purpose: starting the service takes over
# from the controller's own programs, so it should be a deliberate step.
set -euo pipefail

HOST=amethyst
REMOTE='~/rainbird'
HERE="$(cd "$(dirname "$0")/.." && pwd)"
APPLY=0; START=0; TZ_NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --start) START=1 ;;
    --tz) TZ_NAME="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$START" = 1 ]; then
  echo "Enabling and starting rainbird-service and rainbird-web on $HOST..."
  ssh "$HOST" 'systemctl --user enable --now rainbird-service rainbird-web && sleep 5 &&
               systemctl --user --no-pager status rainbird-service rainbird-web | head -20 &&
               curl -s http://127.0.0.1:8710/api/status | head -c 400; echo'
  exit 0
fi

cat <<EOF
Plan for $HOST:$REMOTE
  1. copy service/ (code, tests, sim cert), web/ (source, no node_modules/.next), deploy/ units
  2. copy controller-password.txt to $REMOTE/controller-pin.txt (mode 600)
  3. write $REMOTE/rainbird.env (time zone: ${TZ_NAME:-<not given>}, PIN file, database $REMOTE/data/rainbird.db)
  4. create $REMOTE/venv with python3, pip install service/requirements.txt
  5. run the service tests on the NUC (simulated controller only)
  6. npm ci and next build in web/
  7. install the units (service, web, weekly backup) into ~/.config/systemd/user and daemon-reload (none newly enabled or started)
Nothing talks to the real controller until deploy.sh --start.
EOF

if [ "$APPLY" = 0 ]; then
  echo; echo "Dry run: nothing done. Add --apply --tz <Area/City> to do it."
  exit 0
fi
if [ -z "$TZ_NAME" ]; then
  echo "--apply needs --tz (the sprinklers' time zone, e.g. America/Phoenix)" >&2; exit 2
fi
if [ ! -f "$HERE/controller-password.txt" ]; then
  echo "missing $HERE/controller-password.txt" >&2; exit 2
fi

echo; echo "== 1. copy"
ssh "$HOST" "mkdir -p $REMOTE/data"
tar -C "$HERE" -cf - \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='node_modules' --exclude='.next' \
  service web/src web/public web/package.json web/package-lock.json web/next.config.ts \
  web/tsconfig.json web/postcss.config.mjs web/eslint.config.mjs web/next-env.d.ts deploy \
  | ssh "$HOST" "tar -C $REMOTE -xf -"

echo "== 2. PIN"
ssh "$HOST" "umask 077 && cat > $REMOTE/controller-pin.txt" < "$HERE/controller-password.txt"

echo "== 3. env"
ssh "$HOST" "cat > $REMOTE/rainbird.env" <<EOF
RAINBIRD_HOST=192.168.0.105
RAINBIRD_TZ=$TZ_NAME
RAINBIRD_PASSWORD_FILE=/home/scott/rainbird/controller-pin.txt
RAINBIRD_DB=/home/scott/rainbird/data/rainbird.db
EOF
ssh "$HOST" "python3 -c 'import zoneinfo,sys; zoneinfo.ZoneInfo(sys.argv[1])' '$TZ_NAME'" \
  || { echo "time zone '$TZ_NAME' not recognised on $HOST" >&2; exit 1; }

echo "== 4. python"
ssh "$HOST" "cd $REMOTE && [ -d venv ] || python3 -m venv venv; venv/bin/pip install -q -r service/requirements.txt"

echo "== 5. tests (simulated controller)"
# Stops the deploy on any failure, naming the test (a pipe to tail used to hide it).
ssh "$HOST" "cd $REMOTE/service && ../venv/bin/python -m pytest -q -p no:cacheprovider -rf tests/ > /tmp/rainbird-tests.txt 2>&1; rc=\$?; grep -E '^FAILED|passed|failed' /tmp/rainbird-tests.txt; exit \$rc" \
  || { echo "tests failed on $HOST; full output in $HOST:/tmp/rainbird-tests.txt. Stopping." >&2; exit 1; }

echo "== 6. web build"
ssh "$HOST" "cd $REMOTE/web && npm ci --no-audit --no-fund --loglevel=error && npm run build 2>&1 | tail -12"

echo "== 7. units"
ssh "$HOST" "mkdir -p ~/.config/systemd/user && cp $REMOTE/deploy/rainbird-*.service $REMOTE/deploy/rainbird-*.timer ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user --no-pager list-unit-files 'rainbird-*'"

echo; echo "Installed, not started. When ready: deploy/deploy.sh --start"
