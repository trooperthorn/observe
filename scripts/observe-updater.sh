#!/usr/bin/env bash
# Observe update helper (README "Updating", THREAT-MODEL.md "Updates").
#
# Runs as root on the Docker host, started by the observe-updater.path unit when the Observe
# container writes <deploy>/data/update/request.json. It validates the request, claims it by
# moving it to request.<id>.json so it can never fire twice, and then, writing state.json at
# each phase: backs up data/observe.db (keeping the last 5), pulls origin/main with a
# fast-forward only (refusing a dirty tree), builds the image with the commit baked in,
# validates the config with the new image, and restarts the container. A failed build or
# validate leaves the running container untouched. A failed restart checks the old commit out
# again, rebuilds and starts it, so the previous version is back.
#
# It never reads .env or anything under secrets/ (docker compose reads .env itself, as it does
# for any `up`), logs only to state.json and to its own stdout (journald under systemd), and
# treats the request file as data: every field is checked before anything runs.
#
# Usage: observe-updater.sh <deployment directory>

set -euo pipefail

DEPLOY="${1:-}"
OWNER_UID=10001          # the container's user: a request from anyone else is ignored
MAX_AGE_S=600            # a request older than this is refused
KEEP_BACKUPS=5
SERVICE=observe
TARGET=origin/main
CONFIG_IN_CONTAINER=/config/observe.yaml

log() { printf '%s observe-updater: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "$*"; exit 1; }

[ -n "$DEPLOY" ] || die "usage: observe-updater.sh <deployment directory>"
[ "$(id -u)" = 0 ] || die "must run as root"
[ -d "$DEPLOY/.git" ] || die "$DEPLOY is not a git checkout"
[ -f "$DEPLOY/docker-compose.yml" ] || die "$DEPLOY has no docker-compose.yml"
for tool in git docker python3 stat; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is needed and was not found"
done

UPDATE_DIR="$DEPLOY/data/update"
REQUEST="$UPDATE_DIR/request.json"
STATE="$UPDATE_DIR/state.json"
SEEN="$UPDATE_DIR/seen.ids"
BACKUPS="$DEPLOY/data/backups"
REQUEST_ID=""
REQUESTED_BY=""
OLD_COMMIT=""
NEW_COMMIT=""
STARTED_AT="$(date +%s)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

# One run at a time. The service is oneshot, so this only matters when the script is run by hand.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$UPDATE_DIR/.lock" 2>/dev/null || true
  flock -n 9 2>/dev/null || die "another update is running"
fi

# ---- state.json --------------------------------------------------------------------------------

# Messages name commits by their short form: the console's redactor hides any run of 40 or
# more letters and digits, which a full sha would trip. The old_commit and new_commit fields
# carry the full values.
short() {
  local text="$*"
  [ -n "$OLD_COMMIT" ] && text="${text//$OLD_COMMIT/${OLD_COMMIT:0:7}}"
  [ -n "$NEW_COMMIT" ] && text="${text//$NEW_COMMIT/${NEW_COMMIT:0:7}}"
  printf '%s' "$text"
}

# write_state PHASE MESSAGE [FAILED_IN] [FILE]: rewrite state.json atomically with the phase, the
# message appended to the log, and the lines of FILE (command output) appended after it.
write_state() {
  python3 -I - "$STATE" "$REQUEST_ID" "$1" "$(short "$2")" "${3:-}" "$OLD_COMMIT" "$NEW_COMMIT" \
    "$STARTED_AT" "${4:-}" <<'PY'
import json, os, sys, time
path, rid, phase, message, failed_in, old, new, started, extra = sys.argv[1:10]
data = {}
try:
    with open(path, "r", encoding="utf-8") as fh:
        loaded = json.load(fh)
    if isinstance(loaded, dict) and loaded.get("id") == rid:
        data = loaded
except (OSError, ValueError):
    pass
lines = data.get("log") if isinstance(data.get("log"), list) else []
lines.append(message)
if extra:
    try:
        with open(extra, "r", encoding="utf-8", errors="replace") as fh:
            lines += [l.rstrip("\n")[:500] for l in fh.readlines()[-20:]]
    except OSError:
        pass
out = {"v": 1, "id": rid, "phase": phase, "message": message, "started_at": int(started),
       "updated_at": int(time.time()), "old_commit": old, "new_commit": new,
       "failed_in": failed_in, "log": lines[-200:]}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(out, fh)
    fh.write("\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

fail_phase() { # PHASE REASON [FILE]
  log "failed in $1: $2"
  write_state failed "$2" "$1" "${3:-}"
  [ -n "${3:-}" ] && rm -f "$3"
  exit 1
}

# run_logged PHASE COMMAND...: run a command, keep its output for the state log, return its status.
run_logged() {
  local phase="$1"; shift
  local out
  out="$(mktemp)"
  log "$phase: $(short "$*")"
  local rc=0
  "$@" >"$out" 2>&1 || rc=$?
  if [ "$rc" = 0 ]; then
    write_state "$phase" "$phase: $*" "" "$out"
    rm -f "$out"
    return 0
  fi
  rm -f "$LAST_OUTPUT"
  LAST_OUTPUT="$out"
  return "$rc"
}
LAST_OUTPUT=""

# ---- the request -------------------------------------------------------------------------------

[ -e "$REQUEST" ] || { log "no request at $REQUEST"; exit 0; }
[ -L "$REQUEST" ] && { log "request is a symlink, ignored"; mv -f "$REQUEST" "$UPDATE_DIR/request.rejected.$STAMP.json"; exit 0; }
[ -f "$REQUEST" ] || { log "request is not a regular file, ignored"; mv -f "$REQUEST" "$UPDATE_DIR/request.rejected.$STAMP.json"; exit 0; }
owner="$(stat -c %u "$REQUEST")"
if [ "$owner" != "$OWNER_UID" ]; then
  log "request owned by uid $owner, not $OWNER_UID: ignored"
  mv -f "$REQUEST" "$UPDATE_DIR/request.rejected.$STAMP.json"
  exit 0
fi

touch "$SEEN"
# The checker prints "ok ID USER" or a reason and exits 1. Every field is validated; the file is
# read as data and nothing in it is ever evaluated.
if ! verdict="$(python3 -I - "$REQUEST" "$SEEN" "$MAX_AGE_S" "$TARGET" <<'PY'
import calendar, json, re, sys, time
path, seen_path, max_age, target = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
def refuse(reason):
    print(reason)
    sys.exit(1)
try:
    with open(path, "rb") as fh:
        raw = fh.read(65536)
    data = json.loads(raw.decode("utf-8"))
except (OSError, ValueError):
    refuse("request is not valid JSON")
fields = {"v", "id", "requested_by", "requested_at", "target", "nonce"}
if not isinstance(data, dict) or set(data) != fields:
    refuse("request does not have exactly the fields v, id, requested_by, requested_at, target, nonce")
if data["v"] != 1:
    refuse("request version is not 1")
rid = data["id"]
if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", rid):
    refuse("request id is not a uuid")
if data["target"] != target:
    refuse("request target is not " + target)
by = data["requested_by"]
if not isinstance(by, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", by):
    refuse("requested_by is not a user name")
nonce = data["nonce"]
if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
    refuse("nonce is not 32 hex characters")
at = data["requested_at"]
if not isinstance(at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", at):
    refuse("requested_at is not a UTC timestamp")
try:
    epoch = calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ"))
except (ValueError, OverflowError):
    refuse("requested_at is not a valid time")
age = time.time() - epoch
if age > max_age:
    refuse("request is older than %d seconds" % max_age)
if age < -max_age:
    refuse("request is from the future")
try:
    with open(seen_path, "r", encoding="utf-8") as fh:
        seen = fh.read().split()
except OSError:
    seen = []
if rid in seen:
    refuse("request id was already used")
print("ok", rid, by)
PY
)"; then
  REQUEST_ID=""
  log "request refused: $verdict"
  mv -f "$REQUEST" "$UPDATE_DIR/request.rejected.$STAMP.json"
  write_state failed "request refused: $verdict" received
  exit 1
fi
read -r _ok REQUEST_ID REQUESTED_BY <<<"$verdict"

# Claim it: the id is recorded and the file moves, so a second run finds nothing.
printf '%s\n' "$REQUEST_ID" >>"$SEEN"
mv -f "$REQUEST" "$UPDATE_DIR/request.$REQUEST_ID.json"
rm -f "$STATE"
write_state received "received request $REQUEST_ID from $REQUESTED_BY"
log "received request $REQUEST_ID from $REQUESTED_BY"

cd "$DEPLOY"

# ---- backup ------------------------------------------------------------------------------------

write_state backup "backing up the database"
mkdir -p "$BACKUPS"
if [ -f data/observe.db ]; then
  name="observe.db.$STAMP.$REQUEST_ID"
  cp -p data/observe.db "$BACKUPS/$name" || fail_phase backup "could not copy data/observe.db"
  [ -f data/observe.db-wal ] && { cp -p data/observe.db-wal "$BACKUPS/$name-wal" || fail_phase backup "could not copy the write-ahead log"; }
  write_state backup "backup data/observe.db to data/backups/$name"
  # Keep the newest KEEP_BACKUPS copies (and their -wal files); names sort by their timestamp.
  ls -1 "$BACKUPS" | grep -E '^observe\.db\.[0-9]{8}T[0-9]{6}Z\.[0-9a-f-]+$' | sort -r | tail -n +"$((KEEP_BACKUPS + 1))" |
    while read -r old; do rm -f "$BACKUPS/$old" "$BACKUPS/$old-wal"; done
else
  write_state backup "no data/observe.db to back up (another storage backend?)"
fi

# ---- fetch -------------------------------------------------------------------------------------

write_state fetch "checking the working tree"
dirty="$(git status --porcelain)"
[ -z "$dirty" ] || fail_phase fetch "the working tree has local changes; commit, stash or discard them first"
OLD_COMMIT="$(git rev-parse HEAD)"
run_logged fetch git fetch origin || fail_phase fetch "git fetch origin failed" "$LAST_OUTPUT"
run_logged fetch git pull --ff-only origin main || fail_phase fetch "git pull --ff-only origin main failed; the tree is not a fast-forward of $TARGET" "$LAST_OUTPUT"
NEW_COMMIT="$(git rev-parse HEAD)"
if [ "$OLD_COMMIT" = "$NEW_COMMIT" ]; then
  write_state fetch "already at $NEW_COMMIT; rebuilding to pick up a newer base image"
else
  write_state fetch "pulled $OLD_COMMIT to $NEW_COMMIT"
fi

# ---- build and validate ------------------------------------------------------------------------

build() { # COMMIT
  run_logged build docker compose build --pull --build-arg "OBSERVE_GIT_COMMIT=$1" "$SERVICE"
}

build "$NEW_COMMIT" || fail_phase build "docker compose build failed; the running container was not touched" "$LAST_OUTPUT"
run_logged validate docker compose run --rm "$SERVICE" --config "$CONFIG_IN_CONTAINER" --validate ||
  fail_phase validate "the new image refused the config (--validate); the running container was not touched" "$LAST_OUTPUT"

# ---- restart -----------------------------------------------------------------------------------

if ! run_logged restart docker compose up -d "$SERVICE"; then
  up_output="$LAST_OUTPUT"
  log "restart failed, putting $OLD_COMMIT back"
  write_state restart "docker compose up -d failed; putting $OLD_COMMIT back" "" "$up_output"
  if git checkout --quiet "$OLD_COMMIT" && build "$OLD_COMMIT" && run_logged restart docker compose up -d "$SERVICE"; then
    fail_phase restart "restart of $NEW_COMMIT failed; the previous version $OLD_COMMIT is running again"
  fi
  fail_phase restart "restart of $NEW_COMMIT failed and the rollback to $OLD_COMMIT failed too; check docker compose ps" "$LAST_OUTPUT"
fi

write_state done "updated $OLD_COMMIT to $NEW_COMMIT"
log "done: $(short "$OLD_COMMIT to $NEW_COMMIT")"
