#!/usr/bin/env bash
#
# Ship the working tree to the NUC and deploy it as containers. Run from Git
# Bash on the PC. Modelled on the weather and pool apps' ship.sh.
#
#   deploy/ship.sh            committed work only
#   deploy/ship.sh --dirty    a throwaway build of uncommitted changes
#
# The images are tagged with the commit they came from, and that tag is the
# only record of what a container holds, so a build of a dirty tree can't be
# recovered from the repository and is refused unless asked for.
#
# remote-deploy.sh builds and tests both images while the running version
# carries on, and only then switches; it refuses to switch while a zone waters.

set -euo pipefail

cd "$(dirname "$0")/.."

HOST="${HOST:-amethyst}"
REMOTE='~/rainbird'
SSH=(ssh -o BatchMode=yes "$HOST")

dirty=false
[[ "${1:-}" == "--dirty" ]] && dirty=true

if ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
  if [[ "$dirty" == false ]]; then
    echo "Uncommitted changes. Commit them, or pass --dirty for a throwaway build." >&2
    git status --short >&2
    exit 1
  fi
fi
if [[ ! -f controller-password.txt ]]; then
  echo "missing controller-password.txt (the controller PIN)" >&2
  exit 1
fi

SHA=$(git rev-parse --short=12 HEAD)
[[ "$dirty" == true ]] && SHA="${SHA}-dirty"
printf '%s\n%s\n' "$SHA" "$(git log -1 --format=%s)" > VERSION
echo "shipping $SHA"

# The source directories are replaced, not copied over: copying never deletes,
# so a removed file used to live on in the NUC's tree. Only directories that
# are pure copies of the repo are removed; data/, the PIN, .env and the old
# venv are left alone. One remote command, so a failed transfer can't leave
# the tree half removed for long (the backup timer runs deploy/backup.sh).
#
# The PIN is never in the tar: it would land world-readable. It goes over
# separately below, written in place (umask 077) so the file keeps its inode
# and the container's single-file mount doesn't go stale.
tar czf - \
  --exclude=./.git --exclude=./.claude --exclude=./.venv --exclude=./.venv313 \
  --exclude=./controller-password.txt --exclude=./.probe-password-failures.json \
  --exclude=./probe-logs --exclude=./data \
  --exclude=./web/node_modules --exclude=./web/.next --exclude='*.tsbuildinfo' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  . | "${SSH[@]}" "mkdir -p $REMOTE && cd $REMOTE && rm -rf service deploy web/src web/public && tar xzf -"

"${SSH[@]}" "umask 077 && cat > $REMOTE/controller-pin.txt" < controller-password.txt

"${SSH[@]}" "cd $REMOTE && bash deploy/remote-deploy.sh"
