#!/usr/bin/env bash
#
# Go back to a previously built pair of images, on the NUC. No rebuild.
#
#   deploy/rollback.sh          what is running, and what exists
#   deploy/rollback.sh <tag>    switch to it (refused while watering; FORCE=1 overrides)
#
# The database is untouched (it lives on the bind mount), so rolling code back
# does not roll a schema change back. Columns are only ever added, and older
# code ignores them.

set -euo pipefail

cd "$(dirname "$0")/.."

current=""
[[ -f .env ]] && current=$(grep -oP '(?<=^IMAGE_TAG=).*' .env || true)

if [[ $# -eq 0 ]]; then
  echo "running: ${current:-unknown}"
  echo
  echo "available:"
  docker images rainbird-service --format '{{.Tag}}\t{{.CreatedAt}}\t{{.Size}}' \
    | grep -v '^latest' | sort -k2 -r \
    | while IFS=$'\t' read -r tag created size; do
        marker=" "
        [[ "$tag" == "$current" ]] && marker="*"
        printf '  %s %-24s %s  %s\n' "$marker" "$tag" "${created%% *}" "$size"
      done
  echo
  echo "usage: $0 <tag>"
  exit 0
fi

TAG="$1"
for repo in rainbird-service rainbird-web; do
  if ! docker image inspect "$repo:$TAG" >/dev/null 2>&1; then
    echo "No image $repo:$TAG. Run '$0' with no arguments to see what exists." >&2
    exit 1
  fi
done
if [[ "$TAG" == "$current" ]]; then
  echo "Already running $TAG."
  exit 0
fi
if [[ "${FORCE:-}" != 1 ]] && ! bash deploy/idle-check.sh; then
  echo "Not switching while watering; try again when idle, or FORCE=1." >&2
  exit 1
fi

echo "switching from ${current:-unknown} to $TAG"
echo "IMAGE_TAG=$TAG" > .env
docker compose up -d

for _ in $(seq 1 30); do
  if curl -fs -o /dev/null http://127.0.0.1:8710/api/status && curl -fs -o /dev/null http://127.0.0.1:3004/; then
    echo "serving $TAG"
    exit 0
  fi
  sleep 1
done
echo "WARNING: not answering within 30 s. Check 'docker compose logs'." >&2
exit 1
