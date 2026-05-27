#!/bin/sh
# Container entrypoint.
#
# Runs database migrations idempotently before starting the main process.
# Each migration is a "CREATE ... IF NOT EXISTS" / "ADD COLUMN IF NOT
# EXISTS" pattern, so running on every container start is safe and cheap
# (sub-second on an already-migrated database).
#
# Why migrate on entrypoint rather than as a separate one-shot:
#  - One source of truth for "what schema does this image expect".
#  - Container restarts pick up new migrations automatically when the
#    image is rebuilt — no manual step that someone might forget.
#  - Still safe for the outcomes-worker service to run the same script;
#    each migration's IF NOT EXISTS handles the concurrent-startup case.
#
# Failure handling: if any migration fails, the container exits non-zero
# and Docker's restart policy kicks in. A real error (broken connection,
# permissions) will keep the container restarting until fixed; a stale
# IF NOT EXISTS will succeed and the main process starts.
#
# Adding a new migration: append it to the list below in dependency order.
# Each line is independent and idempotent.

set -e

echo "[entrypoint] Running migrations..."

python -m config.migrate_classifications
python -m config.migrate_v2
python -m config.migrate_triggers
python -m config.migrate_directional
python -m config.migrate_outcomes
python -m config.migrate_alerts
python -m config.migrate_detrending
python -m config.migrate_calibration_history
python -m config.migrate_calibration_history

echo "[entrypoint] Migrations complete; starting main process: $*"
exec "$@"
