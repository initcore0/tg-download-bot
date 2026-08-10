#!/bin/sh
# Fix ownership of mounted data directories, then drop to the non-root user.
#
# Hosted platforms (Coolify, Portainer, plain `docker run -v`) create volumes
# owned by root; the bot runs as uid 10001 (tgdl) and could not write there
# otherwise. Runs only the minimum as root, then execs via gosu.
set -eu

resolve_dir() {
    # Absolute-ify a path relative to /app (the WORKDIR).
    case "$1" in
        /*) printf '%s' "$1" ;;
        *) printf '/app/%s' "$1" ;;
    esac
}

if [ "$(id -u)" = "0" ]; then
    db_dir="$(dirname "$(resolve_dir "${DATABASE_PATH:-data/tgdl.db}")")"
    dl_dir="$(resolve_dir "${DOWNLOAD_DIR:-data/downloads}")"
    for dir in "$db_dir" "$dl_dir"; do
        mkdir -p "$dir"
        chown -R tgdl:tgdl "$dir"
    done
    exec gosu tgdl "$@"
fi

# Already non-root (e.g. started with --user): just run.
exec "$@"
