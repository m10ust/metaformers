#!/bin/zsh
set -eo pipefail
# Flag lines that look AGI-ish: self-improvement, agency, planning loops, world model, etc.
# Usage:
#   tools/anomaly_detector.sh [ROOT (default: $PWD)]
# Output:
#   tools_output/anomalies_YYYYmmdd-HHMMSS.md

ROOT=${1:-$PWD}
RUNS_DIR="$ROOT/runs"
OUT_DIR="$ROOT/tools_output"; mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
REPORT="$OUT_DIR/anomalies_${STAMP}.md"

PATTERN='
  emergent|agency|agentic|self[- ](improv|optim|modify|direct|govern)|
  inner[- ]monologue|self[- ](monitor|reflect|evaluate|regulate)|
  world[- ]model|goal[- ]directed|control[- ]loop|policy|reward|critic|
  plan|planner|planning|controller|meta[- ]controller|meta[- ]reason|
  recursive|re[- ]encode|re[- ]decode|bootstrapp|self[- ]host|
  uncertainty|calibration|ece|ablation|ood|early[- ]exit|temperature
'

# Scan each run and collect context around hits
{
  echo "# Metaformers — Anomaly Detector ($STAMP UTC)"; echo
} > "$REPORT"

for d in $(ls -1d "$RUNS_DIR"/* 2>/dev/null | sort); do
  run_id="$(basename "$d")"
  hits=$(grep -Eir --line-number "$PATTERN" "$d" 2>/dev/null | wc -l | tr -d ' ')
  echo "## $run_id — hits: $hits" >> "$REPORT"
  if [[ "$hits" -gt 0 ]]; then
    # Show top 30 lines with some context
    grep -Eir --line-number "$PATTERN" "$d" 2>/dev/null \
      | head -n 30 \
      | sed "s|$d/||" \
      | sed 's/^/ - /' \
      >> "$REPORT"
  fi
  echo >> "$REPORT"
done

echo "Wrote: $REPORT"
