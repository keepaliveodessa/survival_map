#!/bin/sh
set -e

loki -config.file=/etc/loki/local-config.yaml &
LOKI_PID=$!

promtail -config.file=/etc/promtail/promtail.yml &
PROMTAIL_PID=$!

# Завершаемся, если любой из процессов упал.
# wait -n (bash/busybox sh) ждёт завершения первого из указанных PID.
wait -n "$LOKI_PID" "$PROMTAIL_PID"
exit 1
