#!/usr/bin/env bash
# One-command local edge stack: broker + edge service + store simulator.
# Works on a laptop or a Raspberry Pi 4/5 (64-bit OS). No cloud, no Docker needed.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
VENV=.venv
if [ ! -x "$VENV/bin/python" ]; then
  echo "[setup] creating virtualenv"; $PY -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r requirements.txt
fi
P="$VENV/bin/python"
if [ ! -f models/LATEST ]; then
  [ -f data/daily_sales.csv.gz ] || "$P" -m retail_edge.data
  echo "[setup] training model (about 20 s)"; "$P" -m retail_edge.train
fi
mkdir -p logs
pids=()
cleanup() { echo; echo "[stop] shutting down"; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM
if [ "${EMBEDDED_BROKER:-1}" = "1" ]; then
  "$P" -m retail_edge.broker > logs/broker.log 2>&1 & pids+=($!)
  sleep 1.5
fi
"$P" -m retail_edge.service > logs/edge.log 2>&1 & pids+=($!)
for i in $(seq 1 40); do curl -fs "http://127.0.0.1:${HTTP_PORT:-8000}/health" >/dev/null 2>&1 && break; sleep 0.5; done
if [ "${SIMULATOR:-1}" = "1" ]; then
  "$P" -m retail_edge.simulator --speed "${SIM_DAY_SECONDS:-4}" > logs/simulator.log 2>&1 & pids+=($!)
fi
echo "Dashboard  : http://localhost:${HTTP_PORT:-8000}"
echo "REST docs  : http://localhost:${HTTP_PORT:-8000}/docs"
echo "MQTT       : tcp://localhost:${MQTT_PORT:-1883}  (ws :9001)"
echo "Logs       : ./logs   (Ctrl+C to stop)"
wait
