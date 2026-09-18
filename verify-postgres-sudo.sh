#!/usr/bin/env bash
# One-shot privileged postgres verification runner.
# Usage:   ./verify-postgres-sudo.sh
# Output:  /tmp/pg_verify.log (read by the assistant afterwards)
# Requires: sudo (will ask for password once)
set -uo pipefail
cd "$(dirname "$0")"

LOG=/tmp/pg_verify.log
: > "$LOG"

run() {  # run "<label>" <cmd...>
    local label="$1"; shift
    echo "=== [$label] ===" | tee -a "$LOG"
    "$@" >> "$LOG" 2>&1
    echo "--- exit: $? ---" | tee -a "$LOG"
}

echo "=== PG VERIFY start $(date -Is) ===" | tee -a "$LOG"

run "build"     sudo docker compose build postgres
run "up"        sudo docker compose up -d postgres

# Wait for health (start_period 180s + retries), poll up to 5 min
echo "=== [health-wait] ===" | tee -a "$LOG"
for i in $(seq 1 60); do
    H=$(sudo docker inspect --format '{{.State.Health.Status}}' postgres 2>/dev/null || echo "missing")
    echo "[$i] health=$H" | tee -a "$LOG"
    [ "$H" = "healthy" ] && break
    [ "$H" = "missing" ] && [ "$i" -ge 3 ] && break
    sleep 5
done

run "ps"          sudo docker compose ps postgres
run "inspect"     sh -c "sudo docker inspect postgres | head -100"
run "logs"        sh -c "sudo docker logs --tail 200 postgres 2>&1"
run "health-log"  sh -c "sudo docker inspect --format '{{json .State.Health}}' postgres | head -c 4000"

echo "=== [sql] extensions ===" | tee -a "$LOG"
sudo docker exec postgres psql -U postgres -d postgres -c "SELECT extname, extversion FROM pg_extension ORDER BY 1;" >> "$LOG" 2>&1

echo "=== [sql] cron jobs ===" | tee -a "$LOG"
sudo docker exec postgres psql -U postgres -d postgres -c "SELECT jobid, jobname, schedule, command FROM cron.job ORDER BY jobid;" >> "$LOG" 2>&1

echo "=== [sql] tables ===" | tee -a "$LOG"
sudo docker exec postgres psql -U postgres -d postgres -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1;" >> "$LOG" 2>&1

echo "=== [sql] data counts ===" | tee -a "$LOG"
sudo docker exec postgres psql -U postgres -d postgres -c "SELECT 'geo' t, count(*) FROM geo UNION ALL SELECT 'stopwords', count(*) FROM stopwords UNION ALL SELECT 'layer_keywords', count(*) FROM layer_keywords;" >> "$LOG" 2>&1

echo "=== [sql] collation version ===" | tee -a "$LOG"
sudo docker exec postgres psql -U postgres -d postgres -c "SELECT datname, datcollate, datctype, datcollversion FROM pg_database WHERE datname='postgres';" >> "$LOG" 2>&1
sudo docker exec postgres psql -U postgres -d postgres -c "SELECT collname, collversion FROM pg_collation WHERE collname IN ('en_US.utf8','ru_RU.utf8') AND collprovider='c';" >> "$LOG" 2>&1

echo "=== [sql] postgis version ===" | tee -a "$LOG"
sudo docker exec postgres psql -U postgres -d postgres -c "SELECT PostGIS_Full_Version();" >> "$LOG" 2>&1

echo "=== PG VERIFY done $(date -Is) ===" | tee -a "$LOG"
echo "Log saved to $LOG"
