#!/bin/bash
# Supervisor for the full historical pull: restarts pipeline.py if it dies
# (e.g. transient network/proxy errors), relying on idempotent upserts so
# a restart never loses or duplicates data - it just re-syncs series/events
# and re-checks already-stored markets/candles.
cd "$(dirname "$0")"
source .venv/bin/activate

MAX_ATTEMPTS=50
for i in $(seq 1 "$MAX_ATTEMPTS"); do
    echo "=== attempt $i/$MAX_ATTEMPTS starting at $(date -u +%FT%TZ) ===" >> full_run.log
    python pipeline.py >> full_run.log 2>&1
    status=$?
    if [ "$status" -eq 0 ]; then
        echo "=== pipeline completed successfully at $(date -u +%FT%TZ) ===" >> full_run.log
        exit 0
    fi
    echo "=== attempt $i failed with exit code $status, retrying in 30s ===" >> full_run.log
    sleep 30
done
echo "=== gave up after $MAX_ATTEMPTS attempts ===" >> full_run.log
exit 1
