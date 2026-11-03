#!/usr/bin/env bash
#
# Build, test and switch to new images, on the NUC. Run from ~/rainbird
# (ship.sh does). Modelled on the weather app's remote-deploy.sh.
#
#   FORCE=1 deploy/remote-deploy.sh    switch even while a zone is watering
#
# Order matters: both images are built (the service image runs the whole test
# suite against the simulator) while the running version carries on, so a
# failed build changes nothing. Only then is the switch made.

set -euo pipefail

cd "$(dirname "$0")/.."

KEEP=10   # image pairs to retain; older ones are pruned

SHA=$(head -1 VERSION 2>/dev/null | tr -d '[:space:]')
if [[ -z "$SHA" ]]; then
  echo "No VERSION. It is written when the source is shipped; refusing to build an image with no identity." >&2
  exit 1
fi
if [[ ! -f controller-pin.txt ]]; then
  echo "No controller-pin.txt: the service can't sign in to the controller without it." >&2
  exit 1
fi
# Created by Docker, the mount point would be root-owned and every write fail.
mkdir -p data

echo "== building rainbird-service:$SHA (runs the tests)"
docker build -t "rainbird-service:$SHA" service/
echo "== building rainbird-web:$SHA"
docker build -t "rainbird-web:$SHA" web/
docker run --rm "rainbird-service:$SHA" python -c "import zoneinfo; zoneinfo.ZoneInfo('America/Phoenix')"

if [[ "${FORCE:-}" != 1 ]] && ! bash deploy/idle-check.sh; then
  echo "Not switching while watering. The images are built: run 'bash deploy/remote-deploy.sh' again" >&2
  echo "on the NUC when it's idle (the build is cached), or with FORCE=1 to stop the run." >&2
  exit 1
fi

# The service was first deployed as systemd user services. With linger on,
# those start at boot too, and two clients would fight over a module that
# serves one: retire them before the containers start. Their unit files are
# removed only once the containers are healthy, so until then they are the
# way back.
legacy=false
if systemctl --user cat rainbird-service.service >/dev/null 2>&1; then
  legacy=true
  echo "== retiring the systemd user services"
  systemctl --user disable --now rainbird-service.service rainbird-web.service
fi

docker tag "rainbird-service:$SHA" rainbird-service:latest
docker tag "rainbird-web:$SHA" rainbird-web:latest
echo "IMAGE_TAG=$SHA" > .env
docker compose up -d

echo "== waiting for the service and the page"
healthy=false
for _ in $(seq 1 60); do
  if curl -fs -o /dev/null http://127.0.0.1:8710/api/status && curl -fs -o /dev/null http://127.0.0.1:3004/; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  echo "WARNING: not answering within 60 s. Check 'docker compose logs'." >&2
  if [[ "$legacy" == true ]]; then
    echo "To go back to the systemd services:" >&2
    echo "  docker compose down && systemctl --user enable --now rainbird-service rainbird-web" >&2
  fi
  exit 1
fi

if [[ "$legacy" == true ]]; then
  rm -f ~/.config/systemd/user/rainbird-service.service ~/.config/systemd/user/rainbird-web.service
  echo "removed the old unit files"
fi

# The weekly backup stays a systemd timer: it only reads the database file.
mkdir -p ~/.config/systemd/user
cp deploy/rainbird-backup.service deploy/rainbird-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload

docker compose ps
curl -s http://127.0.0.1:8710/api/status | python3 -c '
import json, sys
s = json.load(sys.stdin)
print("controller connected:", s["link"]["connected"], "| hold:", s["hold"])'

for repo in rainbird-service rainbird-web; do
  mapfile -t old < <(docker images "$repo" --format '{{.Tag}}\t{{.CreatedAt}}' \
    | grep -v '^latest' | sort -k2 -r | tail -n +$((KEEP + 1)) | cut -f1)
  for tag in "${old[@]:-}"; do
    [[ -z "$tag" || "$tag" == "$SHA" ]] && continue
    echo "pruning $repo:$tag"
    docker rmi "$repo:$tag" >/dev/null 2>&1 || true
  done
done

echo
echo "deployed $SHA"
