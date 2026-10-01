#!/bin/sh
set -eu

# One foreground process. PAG_MODE picks which:
#   serve     web UI + API on PAG_WEB_PORT, with the scheduler in-process
#   schedule  headless scheduler only, as v1 behaved
# Unset, the mode follows the environment: serve when a login is configured
# (PAG_WEB_PASSWORD or PAG_WEB_INSECURE), otherwise schedule -- so a container
# carried over from v1 keeps doing its nightly pass instead of refusing to
# expose an open API.
#
# Any argument instead turns the container into a one-shot CLI invocation:
#   docker run --rm ... plex-auto-genres doctor
#   docker run --rm --user 0 ... fix-permissions     (see below)
#
# Ownership. v1 ran as root, so an upgraded install's /config belongs to root
# while this image runs the app as PUID:PGID (default 1000:1000). When the
# container starts as root it hands that directory over before dropping
# privileges; when it starts as someone else (compose `user:`, Kubernetes
# runAsUser) it cannot, so it says exactly what to run instead.
# PUID=0 keeps everything as root, as v1 did.
#
# Everything that has to survive is in /config: config.json and the database
# beside it. Versions up to 2.4 kept the database in /logs; on such an install
# the app moves it to /config on the first start, and /logs can go after that.
# A /config that would not survive -- only config.json mounted into it, or a
# temporary filesystem -- is called out at every start, and stops the start
# while an older database is waiting in /logs to move there.

die() {
    for line in "$@"; do
        echo "$line" >&2
    done
    exit 1
}

case "${PUID:-1000}" in *[!0-9]*) die "PUID must be a number, not '${PUID}'." ;; esac
case "${PGID:-1000}" in *[!0-9]*) die "PGID must be a number, not '${PGID}'." ;; esac
# Arithmetic, so "00" and "0" are the same request: stay root.
APP_UID=$((${PUID:-1000}))
APP_GID=$((${PGID:-1000}))

I_AM=$(id -u)
RUN_AS=""
if [ "$I_AM" -eq 0 ] && [ "$APP_UID" -ne 0 ]; then
    RUN_AS="$APP_UID:$APP_GID"
fi

as_user() {
    if [ -n "$RUN_AS" ]; then
        su-exec "$RUN_AS" "$@"
    else
        "$@"
    fi
}

# Can the account the app will run as write here?
writable() {
    as_user test -w "$1"
}

# Hand one directory over: the directory itself and the files directly in it.
# Never recursive, never through a symlink -- /config holds a handful of flat
# files, and `chown -R` on a volume the host shares would be a way to rewrite
# the ownership of anything a symlink there points at.
hand_over() {
    chown "$APP_UID:$APP_GID" "$1" 2>/dev/null || true
    find "$1" -maxdepth 1 -type f -exec chown "$APP_UID:$APP_GID" {} + 2>/dev/null || true
}

owner_of() {
    stat -c '%u:%g' "$1" 2>/dev/null || echo "another user"
}

# Two names for one file: the same directory mounted at two places.
same_file() {
    [ -e "$1" ] && [ "$(stat -c '%d:%i' "$1" 2>/dev/null)" = "$(stat -c '%d:%i' "$2" 2>/dev/null)" ]
}

# A directory's real path, with ".", ".." and symlinks spelled out; empty when
# it cannot be entered.
real_dir() {
    (cd "$1" 2>/dev/null && pwd -P) || true
}

# Where versions up to 2.4 kept the database; the image names it.
LEGACY_DB="${PAG_LEGACY_DB:-/logs/plex-auto-genres.db}"
LEGACY_DIR=$(dirname "$LEGACY_DB")

# Absolute paths only: a relative one makes "." -- the root of the image --
# the directory handed over below.
case "$PAG_CONFIG" in /?*) ;; *) die "PAG_CONFIG must be an absolute path, not '$PAG_CONFIG'." ;; esac
# The database lives beside the config unless PAG_DB says otherwise. The old
# default, carried over by a tool that recreates containers from their former
# settings, is no such choice: it would keep the database where it gets lost.
if [ "${PAG_DB:-}" = "$LEGACY_DB" ]; then
    echo "PAG_DB=$PAG_DB is where versions up to 2.4 kept the database; it now lives beside the config. Remove PAG_DB from the container's settings." >&2
    PAG_DB=""
fi
PAG_DB="${PAG_DB:-$(dirname "$PAG_CONFIG")/plex-auto-genres.db}"
case "$PAG_DB" in /?*) ;; *) die "PAG_DB must be an absolute path, not '$PAG_DB'." ;; esac
CONFIG_DIR=$(dirname "$PAG_CONFIG")
DB_DIR=$(dirname "$PAG_DB")
for dir in "$CONFIG_DIR" "$DB_DIR"; do
    [ "$(real_dir "$dir")" != / ] || die "The config and the database need a directory of their own, mounted as a volume -- /config, say -- not / ($dir)."
done

# Something an older version left in /logs that still has to be carried over:
# its database, or v1's progress files. A directory this user cannot even
# read counts as well: nobody can tell that it holds nothing.
legacy_leftovers() {
    [ -d "$LEGACY_DIR" ] || return 1
    { [ -r "$LEGACY_DIR" ] && [ -x "$LEGACY_DIR" ]; } || return 0
    for leftover in "$LEGACY_DB" "$LEGACY_DIR"/plex-*-successful.txt "$LEGACY_DIR"/plex-*-failures.txt; do
        [ -e "$leftover" ] && return 0
    done
    return 1
}

# Whether what is written to a directory outlives the container, read from
# the mount that holds it in /proc/self/mountinfo: "volume", "layer" (nothing
# is mounted there -- only files inside it, or nothing) or "temporary" (a
# tmpfs, or a directory under the host's /tmp). The table escapes spaces and
# the like as \040; the directory comes in through the environment, which awk
# does not unescape the way it does -v.
durability() {
    DIR="$1" awk '
        function unescape(s) {
            gsub(/\\040/, " ", s); gsub(/\\011/, "\t", s); gsub(/\\012/, "\n", s)
            gsub(/\\134/, "\\", s)
            return s
        }
        {
            for (i = 7; i <= NF && $i != "-"; i++) ;
            point = unescape($5)
            if ((ENVIRON["DIR"] == point || point == "/" || index(ENVIRON["DIR"], point "/") == 1) \
                    && length(point) >= length(best)) {
                best = point; root = unescape($4); fstype = $(i + 1)
            }
        }
        END {
            if (best == "") print "volume"  # no mount table to read: nothing to tell
            else if (best == "/") print "layer"
            else if (fstype == "tmpfs" || fstype == "ramfs") print "temporary"
            else if (root == "/tmp" || index(root, "/tmp/") == 1 \
                     || root == "/private/tmp" || index(root, "/private/tmp/") == 1) print "temporary"
            else print "volume"
        }' /proc/self/mountinfo 2>/dev/null || echo volume
}

fix_permissions() {
    [ "$I_AM" -eq 0 ] || die "fix-permissions needs root: add --user 0 to this one command."
    handed="|"
    for dir in "$CONFIG_DIR" "$DB_DIR" "$LEGACY_DIR" "$PAG_POSTERS"; do
        [ -d "$dir" ] || continue
        [ "$(real_dir "$dir")" != / ] || continue  # never the image itself
        case "$handed" in *"|$dir|"*) continue ;; esac
        handed="$handed$dir|"
        echo "Handing $dir to $APP_UID:$APP_GID"
        hand_over "$dir"
    done
    echo "Done. Start the container normally again."
}

if [ "${1:-}" = "fix-permissions" ]; then
    fix_permissions
    exit 0
fi

# /posters is only ever read, so it is left alone; the config directory (and
# the database's, when PAG_DB points elsewhere) is written.
checked="|"
for dir in "$CONFIG_DIR" "$DB_DIR"; do
    case "$checked" in *"|$dir|"*) continue ;; esac
    checked="$checked$dir|"
    [ -d "$dir" ] || die "$dir does not exist. Mount it: -v /path/on/host:$dir"
    writable "$dir" && continue
    if [ -n "$RUN_AS" ]; then
        echo "Handing $dir to $APP_UID:$APP_GID (it belonged to $(owner_of "$dir"))" >&2
        hand_over "$dir"
        writable "$dir" || die \
            "$dir is still not writable by $APP_UID:$APP_GID." \
            "If it is a network share that refuses chown, mount it with uid=$APP_UID," \
            "or set PUID/PGID to the owner it already has."
    else
        die "$dir is not writable by uid $I_AM:$(id -g)." \
            "This container was started with a fixed user, so it cannot fix that itself." \
            "Either hand the volumes over once:" \
            "    docker compose run --rm --user 0 plex-auto-genres fix-permissions" \
            "or set PUID/PGID to the uid that owns them and start without 'user:'."
    fi
done

# An install from 2.4 or older: the app carries what it finds in /logs over to
# /config on this start, which needs the directory writable once. Once that is
# done, or on a fresh install, /logs is left alone.
if legacy_leftovers && ! writable "$LEGACY_DIR"; then
    if [ -n "$RUN_AS" ]; then
        echo "Handing $LEGACY_DIR to $APP_UID:$APP_GID (it belonged to $(owner_of "$LEGACY_DIR")) so what an older version left there can be carried over" >&2
        hand_over "$LEGACY_DIR"
    else
        echo "$LEGACY_DIR holds files from an older version that uid $I_AM cannot move." >&2
        echo "If the app says so below, run once: docker compose run --rm --user 0 plex-auto-genres fix-permissions" >&2
    fi
fi

if [ ! -f "$PAG_CONFIG" ]; then
    cat >&2 <<MSG
No config file at $PAG_CONFIG.

Mount a directory containing config.json:
    -v /path/to/config:/config

A starting point is config/config.json.example in the repository.
MSG
    exit 1
fi

case "$(durability "$(real_dir "$DB_DIR")")" in
    layer) fragile="is not a mounted volume (at most, files inside it are)" ;;
    temporary) fragile="is on a temporary filesystem (a tmpfs, or the host's /tmp)" ;;
    *) fragile="" ;;
esac
if [ -n "$fragile" ]; then
    # Only the database's default place is filled from /logs (see the app).
    if [ "$PAG_DB" = "$CONFIG_DIR/plex-auto-genres.db" ] && [ -e "$LEGACY_DB" ] \
            && ! same_file "$LEGACY_DB" "$PAG_DB"; then
        die "$DB_DIR $fragile, so the database waiting in $LEGACY_DIR would be lost once moved there." \
            "Mount a directory from lasting storage at $DB_DIR -- the one that holds config.json --" \
            "and start again; $LEGACY_DIR is left untouched until then."
    fi
    echo "WARNING: $DB_DIR $fragile. What the app keeps there -- bindings, genres decided by hand, the cache, the run history -- is lost when the container is recreated or the host restarts. Mount a directory from lasting storage at $DB_DIR." >&2
fi

# su-exec keeps the environment, so HOME would still say /root; anything that
# writes under ~ (a provider client's cache) must land somewhere writable.
if [ -n "$RUN_AS" ]; then
    HOME=/tmp
    export HOME
fi

if [ "$#" -gt 0 ]; then
    if [ -n "$RUN_AS" ]; then
        exec su-exec "$RUN_AS" plex-auto-genres --config "$PAG_CONFIG" --db "$PAG_DB" "$@"
    fi
    exec plex-auto-genres --config "$PAG_CONFIG" --db "$PAG_DB" "$@"
fi

MODE="${PAG_MODE:-}"
if [ -z "$MODE" ]; then
    if [ -n "${PAG_WEB_PASSWORD:-}" ] || [ -n "${PAG_WEB_INSECURE:-}" ]; then
        MODE=serve
    else
        MODE=schedule
        echo "PAG_MODE is unset and no PAG_WEB_PASSWORD is configured: running headless." >&2
        echo "Set PAG_WEB_PASSWORD (or PAG_MODE=serve with PAG_WEB_INSECURE=1) for the web UI." >&2
    fi
fi

echo "plex-auto-genres $(plex-auto-genres --version | awk '{print $2}') — mode=$MODE TZ=$TZ user=${RUN_AS:-$I_AM:$(id -g)}"
as_user plex-auto-genres --config "$PAG_CONFIG" --db "$PAG_DB" doctor --offline \
    || echo "(doctor reported issues; continuing anyway)"

# The optional --now flag travels as a positional parameter, never word-split.
set --
if [ "${RUN_ON_START}" = "true" ]; then
    set -- --now
fi

case "$MODE" in
    serve)
        set -- serve --host 0.0.0.0 --port "$PAG_WEB_PORT" --cron "$CRON_SCHEDULE" \
            --posters-dir "$PAG_POSTERS" "$@"
        ;;
    schedule)
        set -- schedule --cron "$CRON_SCHEDULE" --posters-dir "$PAG_POSTERS" "$@"
        ;;
    *)
        die "Unknown PAG_MODE='$MODE' (expected serve or schedule)"
        ;;
esac

if [ -n "$RUN_AS" ]; then
    exec su-exec "$RUN_AS" plex-auto-genres --config "$PAG_CONFIG" --db "$PAG_DB" "$@"
fi
exec plex-auto-genres --config "$PAG_CONFIG" --db "$PAG_DB" "$@"
