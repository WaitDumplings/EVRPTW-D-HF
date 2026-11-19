#!/usr/bin/env bash
# Usage: WORK=/data/evrptwd-city-work OUTPUT=/data/evrptwd-city-output bash scripts/generate_cities.sh A
set -euo pipefail
case "${1:-}" in
  A) cities=(chicago houston) ;;
  B) cities=(new-york phoenix jacksonville) ;;
  C) cities=(los-angeles philadelphia san-antonio) ;;
  D) cities=(dallas fort-worth san-diego) ;;
  *) echo "Usage: bash scripts/generate_cities.sh {A|B|C|D}" >&2; exit 2 ;;
esac

: "${WORK:?Set WORK to this server's work directory}"
: "${OUTPUT:?Set OUTPUT to this server's delivery directory}"
mkdir -p "$WORK" "$OUTPUT"
WORK=$(cd "$WORK" && pwd)
OUTPUT=$(cd "$OUTPUT" && pwd)
cd "$(dirname "$0")/.."
WORKERS=${WORKERS:-2}
# Public national/state downloads and the Amazon order catalog are cached on this host.
python pipeline.py --work "$WORK" sources --city "${cities[@]}" --workers "${SOURCE_WORKERS:-2}"
python pipeline.py --work "$WORK" stage1 --city "${cities[@]}" --workers "${CLE_WORKERS:-1}"
for city in "${cities[@]}"; do
    python pipeline.py --work "$WORK" prepare-city --city "$city"
    python pipeline.py --work "$WORK" plan --city "$city"
    python export_output.py --out "$OUTPUT" cle --work "$WORK" --city "$city"
    python pipeline.py --work "$WORK" stage2 --city "$city" --workers "$WORKERS"
    report="$OUTPUT/reports/$city"
    mkdir -p "$report"
    python pipeline.py --work "$WORK" audit --city "$city" > "$report/audit.json"
    python export_output.py --out "$OUTPUT" city-instances --work "$WORK" --city "$city" > "$report/publish.json"
done
