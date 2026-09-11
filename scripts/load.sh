#!/usr/bin/env bash
set -euo pipefail
scenario="${1:-normal}"
set_config() {
  curl --fail --silent --show-error --max-time 30 \
    -X POST "http://127.0.0.1:$1/config" \
    -H 'Content-Type: application/json' -d "$2"
  echo
}
case "$scenario" in
  normal)
    set_config 8001 '{"rps":5,"cpu_ms":5,"memory_mb":16,"delay_ms":0,"error_rate":0}'
    set_config 8002 '{"rps":5,"cpu_ms":5,"memory_mb":16,"delay_ms":0,"error_rate":0}'
    ;;
  high)
    # A -> B at target 80 RPS; B spends 20 ms CPU per received request.
    set_config 8002 '{"rps":5,"cpu_ms":20,"memory_mb":128,"delay_ms":0,"error_rate":0}'
    set_config 8001 '{"rps":80,"cpu_ms":5,"memory_mb":16,"delay_ms":0,"error_rate":0}'
    ;;
  errors)
    set_config 8002 '{"error_rate":0.30}'
    ;;
  slow)
    set_config 8002 '{"delay_ms":1000}'
    ;;
  stop)
    set_config 8001 '{"rps":0}'
    set_config 8002 '{"rps":0}'
    ;;
  *) echo "Usage: bash scripts/load.sh {normal|high|errors|slow|stop}" >&2; exit 1 ;;
esac
