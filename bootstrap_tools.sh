#!/bin/zsh
set -eo pipefail
# =============================================
# Metaformers tools bootstrapper
# Creates tools/ and installs:
#   - mine_patterns.sh      (recurring concept miner)
#   - anomaly_detector.sh   (AGI-ish pattern spotter)
# No external deps beyond BSD awk/sed/grep.
# =============================================

ROOT=${1:-$PWD}
TOOLS_DIR="$ROOT/tools"
OUT_DIR="$ROOT/tools_output"
mkdir -p "$TOOLS_DIR" "$OUT_DIR"
ts(){ date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# -------------------------
# mine_patterns.sh
# -------------------------
cat > "$TOOLS_DIR/mine_patterns.sh" <<'SH'
#!/bin/zsh
set -eo pipefail
# Mine recurring concepts across Creator logs and seeds into a markdown report.
# Usage:
#   tools/mine_patterns.sh [ROOT (default: $PWD)] [TOP_N (default: 40)]
# Output:
#   tools_output/patterns_YYYYmmdd-HHMMSS.md

ROOT=${1:-$PWD}
TOP_N=${2:-40}
RUNS_DIR="$ROOT/runs"
OUT_DIR="$ROOT/tools_output"; mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
REPORT="$OUT_DIR/patterns_${STAMP}.md"

# Collect text from Creator responses + principles + seeds
TMP_ALL="$OUT_DIR/_all_${STAMP}.txt"
: > "$TMP_ALL"

# Creator responses (strip timestamps & labels, keep content)
for f in $(ls -1 "$RUNS_DIR"/*/logs/gpt_oss_creator_*.log 2>/dev/null); do
  # pull only lines after the "[gpt-oss] Response:" markers
  /usr/bin/awk '
    /^\[[^]]*\] \[gpt-oss\] Response:/ { collect=1; next }
    /^\[[^]]*\] \[[^]]+\] / { collect=0 } # next block starts
    { if (collect) print }
  ' "$f" >> "$TMP_ALL" || true
  echo >> "$TMP_ALL"
done

# Principles + seeds
cat "$RUNS_DIR"/*/principles.md 2>/dev/null >> "$TMP_ALL" || true
cat "$RUNS_DIR"/*/seed_prompt.txt 2>/dev/null >> "$TMP_ALL" || true

# Normalise, lowercase, drop obvious noise
CLEAN="$OUT_DIR/_clean_${STAMP}.txt"
cat "$TMP_ALL" \
  | tr '\r' '\n' \
  | sed 's/[()\[\]{}<>]/ /g' \
  | sed 's/[^A-Za-z0-9%\-_.: ]/ /g' \
  | tr '[:upper:]' '[:lower:]' \
  | sed 's/  */ /g' \
  > "$CLEAN"

# Count key multi-word phrases (hand-picked signals)
# You can extend this list easily.
PHRASES=(
  "meta self transformer" "meta controller" "confidence calibration" "expected calibration error"
  "retrieval augmented" "policy gradient" "actor critic" "re decode" "early exit"
  "attention fingerprint" "working memory" "external memory" "uncertainty estimate"
  "self check" "self critique" "ablation study" "ood" "out of distribution" "temperature scheduling"
  "gating factor" "stop condition" "failure mode" "uncertainty budget" "rubric"
)

# Write header
{
  echo "# Metaformers — Pattern Mine ($STAMP UTC)"
  echo
  echo "Scans Creator responses, principles, and seeds across runs for recurring concepts."
  echo
  echo "## Top Keywords"
} > "$REPORT"

# Top keywords (unigrams/bigrams/trigrams) by frequency
# Build naive n-grams (1..3)
/usr/bin/awk '
  function emit(w){ if(length(w)>2 && w!~/(the|and|for|with|that|this|into|from|have|has|are|was|you|your|our|their|can|will|may|should|would|could|not|but|its|it|of|in|to|on|by|as|an|at|be|or|we|a)$/) cnt[w]++ }
  {
    n=split($0,t,/ +/);
    for(i=1;i<=n;i++){ emit(t[i]) }
    for(i=1;i<=n-1;i++){ emit(t[i]" "t[i+1]) }
    for(i=1;i<=n-2;i++){ emit(t[i]" "t[i+1]" "t[i+2]) }
  }
  END{
    for(k in cnt) print cnt[k]"\t"k;
  }
' "$CLEAN" \
| sort -rn \
| head -n "$TOP_N" \
| sed $'s/^/ - /; s/\t/ — /' \
>> "$REPORT"

echo >> "$REPORT"; echo "## Tracked Phrases" >> "$REPORT"
for p in "$PHRASES[@]"; do
  c=$(grep -Fic -- "$p" "$CLEAN" || true)
  echo "- $p — $c" >> "$REPORT"
done

echo >> "$REPORT"; echo "## Runs Ranked by Signal Density" >> "$REPORT"
# Rank runs by density of signal words
SIG_WORDS="meta|metacog|controller|retrieval|calibration|confidence|ablation|ood|policy|gradient|self-?check|self-?crit|temperature|gate|early[- ]exit|memory"
for d in $(ls -1d "$RUNS_DIR"/* 2>/dev/null | sort); do
  c=$(grep -Eir "$SIG_WORDS" "$d/logs" "$d/principles.md" "$d/seed_prompt.txt" 2>/dev/null | wc -l | tr -d ' ')
  echo "- $(basename "$d") — $c" >> "$REPORT"
done

echo "\nWrote: $REPORT"
SH
chmod +x "$TOOLS_DIR/mine_patterns.sh"

# -------------------------
# anomaly_detector.sh
# -------------------------
cat > "$TOOLS_DIR/anomaly_detector.sh" <<'SH'
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
SH
chmod +x "$TOOLS_DIR/anomaly_detector.sh"

# -------------------------
# Done
# -------------------------
cat <<MSG
Installed tools:
 - $TOOLS_DIR/mine_patterns.sh
 - $TOOLS_DIR/anomaly_detector.sh
Outputs will be written to:
 - $OUT_DIR

Examples:
  tools/mine_patterns.sh              # mine top concepts across runs
  tools/anomaly_detector.sh           # flag AGI-ish lines with context

MSG
