#!/usr/bin/env bash
# Install the Observe update helper on the Docker host (README "Updating").
#
#   sudo scripts/install-updater.sh /opt/observe
#
# Run once as root with the deployment directory (the git checkout that holds
# docker-compose.yml) as the argument. It copies observe-updater.sh to /usr/local/sbin, writes
# the systemd path and service units with that directory filled in, makes data/update writable
# by the container's user, and starts the path unit. Run it again after a pull that changed
# scripts/observe-updater.sh: the installed copy is the one that runs.

set -euo pipefail

DEPLOY="${1:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET=/usr/local/sbin/observe-updater
UNIT_DIR="${OBSERVE_UNIT_DIR:-/etc/systemd/system}"

die() { printf 'install-updater: %s\n' "$*" >&2; exit 1; }

[ -n "$DEPLOY" ] || die "usage: install-updater.sh <deployment directory>"
[ "$(id -u)" = 0 ] || die "run this as root (sudo)"
case "$DEPLOY" in
  /*) ;;
  *) die "the deployment directory must be an absolute path" ;;
esac
# The path goes into unit files verbatim; keep it to characters systemd needs no escaping for.
printf '%s' "$DEPLOY" | grep -Eq '^/[A-Za-z0-9._/-]+$' || die "the deployment directory path may hold only letters, digits, . _ - and /"
[ -d "$DEPLOY/.git" ] || die "$DEPLOY is not a git checkout"
[ -f "$DEPLOY/docker-compose.yml" ] || die "$DEPLOY has no docker-compose.yml"
for tool in git docker python3 systemctl; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is needed and was not found"
done
for f in observe-updater.sh observe-updater.path observe-updater.service; do
  [ -f "$HERE/$f" ] || die "$HERE/$f is missing"
done

install -o root -g root -m 0755 "$HERE/observe-updater.sh" "$TARGET"
# The container (UID 10001) writes the request here and reads the state back; root writes the
# state. 0750 keeps other local users out.
install -d -o 10001 -g 10001 -m 0750 "$DEPLOY/data/update"
install -d -o root -g root -m 0750 "$DEPLOY/data/backups"
for unit in observe-updater.path observe-updater.service; do
  sed "s#@@DEPLOY@@#$DEPLOY#g" "$HERE/$unit" >"$UNIT_DIR/$unit.new"
  chmod 0644 "$UNIT_DIR/$unit.new"
  mv -f "$UNIT_DIR/$unit.new" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable --now observe-updater.path >/dev/null
printf 'install-updater: installed %s, watching %s/data/update/request.json\n' "$TARGET" "$DEPLOY"
printf 'install-updater: progress is in %s/data/update/state.json and journalctl -u observe-updater.service\n' "$DEPLOY"
