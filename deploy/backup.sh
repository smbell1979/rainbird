#!/usr/bin/env bash
#
# Weekly backup of the sprinkler database to git. Modelled on the pool app's
# deploy/backup.sh; the timer (rainbird-backup.timer) runs it with --apply.
#
#   deploy/backup.sh            dry run: snapshot, check, report; commit nothing
#   deploy/backup.sh --apply    also commit and push to smbell1979/rainbird-backup
#
# A plain-text SQL dump rather than the .db file: git diffs text, so each
# commit shows exactly what changed (a plan edited, runs added).
#
# `.backup`, never `cp`: the service has the database open in WAL mode, and a
# copied file mid-write gives a torn snapshot that restores without complaint
# and is missing recent rows.
#
# Restore: stop rainbird-service, then  sqlite3 rainbird.db < rainbird.sql

set -euo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

DB="${DB:-/home/scott/rainbird/data/rainbird.db}"
REPO="${REPO:-/home/scott/rainbird-backup}"
DUMP="$REPO/rainbird.sql"

# Its own key: a GitHub deploy key belongs to exactly one repository, so the
# pool or CG-Stuff key would authenticate as the wrong repo and be refused.
export GIT_SSH_COMMAND="ssh -i /home/scott/.ssh/id_ed25519_rainbird_backup -o IdentitiesOnly=yes -o BatchMode=yes"

if [[ ! -f "$DB" ]]; then
  echo "database not found at $DB" >&2
  exit 1
fi

snapshot=$(mktemp --suffix=.db)
trap 'rm -f "$snapshot"' EXIT

sqlite3 "$DB" ".backup '$snapshot'"

# Checked on the snapshot: a corrupt backup that commits cleanly displaces the
# last good one.
integrity=$(sqlite3 "$snapshot" "pragma integrity_check;")
if [[ "$integrity" != "ok" ]]; then
  echo "integrity check failed on the snapshot: $integrity" >&2
  exit 1
fi

count() { sqlite3 "$snapshot" "select count(*) from $1;"; }
plans=$(count plans)
zones=$(count zones)
runs=$(count runs)
summary="$plans plans, $zones zones, $runs runs"

# Plans can all be deleted on purpose, but zone names are only ever set, and
# nothing here deletes runs: losing every zone row, or most of the history, is
# a bad migration or a wrong database, not a change worth committing.
if [[ "$zones" -eq 0 && "$plans" -eq 0 ]]; then
  echo "snapshot has no plans and no zones ($summary): refusing to commit over the previous backup" >&2
  exit 1
fi
if [[ -f "$DUMP" ]]; then
  previous=$(grep -c '^INSERT INTO runs ' "$DUMP" || true)
  if [[ "$previous" -gt 0 ]] && (( runs * 2 < previous )); then
    echo "runs fell from $previous to $runs (more than half): refusing." >&2
    echo "If intentional, commit once by hand and the next run resumes." >&2
    exit 1
  fi
fi

if [[ "$APPLY" -eq 0 ]]; then
  echo "dry run: snapshot ok ($summary, integrity $integrity); would write $DUMP, commit and push. Add --apply."
  exit 0
fi

sqlite3 "$snapshot" .dump > "$DUMP"

cd "$REPO"

# `status --porcelain`, not `diff --quiet`: diff ignores untracked files, so the
# very first run would report "no changes" and back up nothing.
if [[ -z "$(git status --porcelain -- rainbird.sql)" ]]; then
  echo "no changes since the last backup ($summary)"
  exit 0
fi

git add rainbird.sql
git commit -q -m "Backup $(date +%Y-%m-%d) — $summary"
git push -q origin main

echo "backed up: $summary"
