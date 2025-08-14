#!/bin/zsh
set -eo pipefail

# ===========================
# Metaformers — self-recursive v1.1 (fixed)
# ===========================
# Local, hermetic, automator-friendly; no Homebrew deps; BSD awk/sed safe.

# === AI Models (local) ===
ai_questioner="llama2-uncensored:latest"       # Asks questions
ai_creator="gpt-oss:20b"                        # Proposes ideas
ai_mediator="dolphin3:latest"                   # Meta every 10 iterations

# === Ollama binary (set explicitly; Automator PATH can be weird) ===
ollama_bin="/usr/local/bin/ollama"

# === Iteration & cadence ===
iterations=50
mediator_every=10
creator_think_secs=30

# === Seed behavior ===
TOPIC_DEFAULT="You are three AI models working collaboratively. Your goal is to co-engineer a novel architecture for metacognition in transformer-based LLMs."
: "${AUTO_CHAIN:=0}"   # AUTO_CHAIN=1 to autolaunch the next script

# --- Repo roots / hermetic folders ---
ROOT="$PWD"
RUNS_DIR="$ROOT/runs"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
RUN_DIR="$RUNS_DIR/$RUN_ID"
LOG_DIR="$RUN_DIR/logs"
META_DIR="$RUN_DIR/meta"
mkdir -p "$LOG_DIR" "$META_DIR"

# --- Timestamp helpers ---
ts_iso(){ date -u +"%Y-%m-%dT%H:%M:%SZ"; }
ts(){ ts_iso; }

# --- Logs (timestamped filenames + per-line ts) ---
q_log="$LOG_DIR/llama2_questioner_${RUN_ID}.log"
c_log="$LOG_DIR/gpt_oss_creator_${RUN_ID}.log"
m_log="$LOG_DIR/dolphin3_mediator_${RUN_ID}.log"
master="$LOG_DIR/ai_master_${RUN_ID}.log"
: > "$q_log"; : > "$c_log"; : > "$m_log"; : > "$master"

log_line(){ print -- "[$(ts)] $*" >> "$1"; }
log_both(){ local line="[$(ts)] ${*:2}"; print -- "$line" | tee -a "$1" >> "$master"; }

# --- Sanity: Ollama exists ---
if [[ ! -x "$ollama_bin" ]]; then
  echo "[$(ts)] [fatal] ollama not found or not executable at: $ollama_bin" | tee -a "$master" >&2
  exit 3
fi

# --- Find a seed prompt (override > root seed > last-run seed > default) ---
current_prompt="$TOPIC_DEFAULT"
if [[ -n "${TOPIC_OVERRIDE:-}" ]]; then
  current_prompt="$TOPIC_OVERRIDE"
elif [[ -f "$ROOT/seed_prompt.txt" ]]; then
  current_prompt="$(cat "$ROOT/seed_prompt.txt")"
else
  last_run="$(ls -1dt "$RUNS_DIR"/* 2>/dev/null | grep -v "$RUN_ID" | head -n1 || true)"
  if [[ -n "$last_run" && -f "$last_run/seed_prompt.txt" ]]; then
    current_prompt="$(cat "$last_run/seed_prompt.txt")"
  fi
fi

# --- Environment snapshot & manifest ---
{
  echo "OLLAMA_BIN=$ollama_bin"
  "$ollama_bin" --version 2>&1 || true
  "$ollama_bin" list 2>&1 || true
} > "$META_DIR/version.txt"

cat > "$RUN_DIR/manifest.json" <<JSON
{
  "run_id": "$RUN_ID",
  "started_utc": "$(ts_iso)",
  "cwd": "$ROOT",
  "models": {
    "questioner": "$ai_questioner",
    "creator": "$ai_creator",
    "mediator": "$ai_mediator"
  },
  "iterations": $iterations,
  "mediator_every": $mediator_every,
  "creator_think_secs": $creator_think_secs,
  "ollama_bin": "$ollama_bin",
  "seed_source": "$(print -nr -- "$current_prompt" | head -c 64 | sed 's/"/\\"/g')..."
}
JSON

# ===========================
#                MAIN LOOP
# ===========================
for i in $(seq 1 $iterations); do
  log_both "$master" ""
  log_both "$master" "========== Iteration $i =========="

  # --- Questioner ---
  log_both "$q_log" "[llama2] Prompt:"
  log_both "$q_log" "$current_prompt"
  q_resp="$("$ollama_bin" run "$ai_questioner" <<< "$current_prompt")"
  log_both "$q_log" "[llama2] Response:"
  log_both "$q_log" "$q_resp"

  # --- Creator think delay ---
  log_both "$c_log" "⏳ Thinking (Creator): ${creator_think_secs}s..."
  for s in $(seq $creator_think_secs -1 1); do printf "\r⏳ %02ds " "$s"; sleep 1; done; printf "\n"
  log_both "$c_log" "Continuing."

  # --- Creator ---
  c_prompt="The Questioner asks:
$q_resp

You are the Creator. Propose a novel architecture or mechanism specifically to co-engineer a novel architecture for metacognition in transformer-based LLMs. Respond with:
- Conceptual Insight
- Practical Mechanism
- Why This Matters"
  log_both "$c_log" "[gpt-oss] Prompt:"
  log_both "$c_log" "$c_prompt"
  c_resp="$("$ollama_bin" run "$ai_creator" <<< "$c_prompt")"
  log_both "$c_log" "[gpt-oss] Response:"
  log_both "$c_log" "$c_resp"

  # --- Mediator every N ---
  if (( i % mediator_every == 0 )); then
    m_prompt="You are the Mediator AI. Read the Creator’s response and challenge the underlying assumptions with a meta-question.

Context:
$c_resp"
    log_both "$m_log" "[dolphin3] Prompt:"
    log_both "$m_log" "$m_prompt"
    m_resp="$("$ollama_bin" run "$ai_mediator" <<< "$m_prompt")"
    log_both "$m_log" "[dolphin3] Response:"
    log_both "$m_log" "$m_resp"
    current_prompt="$m_resp"
  else
    current_prompt="$c_resp"
  fi
done

# --- Close manifest with end time (no jq) ---
END_TS="$(ts_iso)"
tmp_manifest="$RUN_DIR/manifest.json.tmp"
awk -v end="$END_TS" '
  BEGIN { done=0 }
  {
    if (!done && $0 ~ /}\s*$/) {
      sub(/}\s*$/, ",\n  \"ended_utc\": \"" end "\"\n}")
      done=1
    }
    print
  }
' "$RUN_DIR/manifest.json" > "$tmp_manifest" && mv "$tmp_manifest" "$RUN_DIR/manifest.json"
echo "ended_utc=$END_TS" >> "$META_DIR/version.txt"

# ===========================
#     AUTO-DISTILL PRINCIPLES
# ===========================
PRINC="$RUN_DIR/principles.md"
SEED="$RUN_DIR/seed_prompt.txt"
N_CREATOR=6
MAX_KEEP=12

{
  echo "# Principles — distilled from last ${N_CREATOR} Creator outputs"
  echo "_Run: $RUN_ID (UTC)_"
  echo

  /usr/bin/awk -v cap="$N_CREATOR" -v maxk="$MAX_KEEP" '
    function flush_block(){ if (collecting){ idx++; buf[idx]=block; block=""; collecting=0 } }
    /^\[[^]]*\] \[gpt-oss\] Response:/ { flush_block(); collecting=1; next }
    /^\[[^]]*\] \[gpt-oss\] Prompt:/   { flush_block(); next }
    { if (collecting) { block = block $0 "\n" } }
    END {
      flush_block();
      start = (idx-cap+1); if (start < 1) start=1;
      for (i=start; i<=idx; i++) {
        print "## Candidate #" i;
        n = split(buf[i], lines, "\n"); kept=0;
        for (j=1; j<=n; j++) {
          line = lines[j];
          if (line ~ /^([[:space:]]*[-*•]|[[:space:]]*[0-9]+\.)/ \
              || line ~ /(measure|evaluate|ablation|failure|constraint|verify|threshold|rank|prob|mask|consistency|calibration|plan|step|test|score|risk|OOD|out[- ]of[- ]distribution|self-?check|reflect|rubric)/ \
              || line ~ /([0-9]+(\.[0-9]+)?%|[0-9]+(ms|s|k|M|B| tokens?))/) {
            sub(/^[[:space:]]+/, "", line);
            if (length(line) > 0 && kept < maxk) { print "- " line; kept++; }
          }
        }
        print "";
      }
      if (idx >= start) {
        latest = buf[idx];
        print "## Next-run seed (latest distilled)";
        n2 = split(latest, L, "\n"); outc=0;
        for (j=1; j<=n2; j++) {
          line=L[j];
          if (line ~ /^([[:space:]]*[-*•]|[[:space:]]*[0-9]+\.)/ \
              || line ~ /(measure|evaluate|ablation|failure|constraint|verify|threshold|rank|prob|mask|consistency|calibration|plan|step|test|score|risk|OOD|out[- ]of[- ]distribution|self-?check|reflect|rubric)/ \
              || line ~ /([0-9]+(\.[0-9]+)?%|[0-9]+(ms|s|k|M|B| tokens?))/) {
            sub(/^[[:space:]]+/, "", line);
            if (length(line) > 0 && outc < 10) { seed[++outc] = line }
          }
        }
        if (outc > 0) { for (k=1; k<=outc; k++) print seed[k] > "/dev/stderr"; }
      }
    }
  ' "$c_log"
} > "$PRINC" 2> "$SEED.tmp"

if [[ -s "$SEED.tmp" ]]; then
  {
    echo "You are three AI models working collaboratively to improve metacognition in transformer LLMs."
    echo "Start from these distilled principles:"
    echo
    sed 's/^/- /' "$SEED.tmp"
    echo
    echo "Produce a compact, structured response with:"
    echo "- Conceptual Insight (2–4 sentences)"
    echo "- Practical Mechanism (4–8 numbered steps)"
    echo "- Why This Matters (3 bullets)"
  } > "$SEED"
  rm -f "$SEED.tmp"
else
  echo "No seed lines found" > "$SEED"
fi

log_line "$master" "Distilled: $PRINC"
log_line "$master" "Seed prompt: $SEED"

# ===================================
#  NEXT RUN SCAFFOLD (SELF-FEEDING)
# ===================================
NEXT_ID="$(date -u +%Y%m%d-%H%M%S)"
NEXT_DIR="$RUNS_DIR/$NEXT_ID"
mkdir -p "$NEXT_DIR"
NEXT_SH="$NEXT_DIR/next_experiment_${NEXT_ID}.sh"
NEXT_MD="$NEXT_DIR/NOTES_${NEXT_ID}.md"

cat > "$NEXT_SH" <<'SH'
#!/bin/zsh
set -eo pipefail
ai_questioner="llama2-uncensored:latest"
ai_creator="gpt-oss:20b"
ai_mediator="dolphin3:latest"
ollama_bin="/usr/local/bin/ollama"

DEFAULT_TOPIC="You are three AI models working collaboratively. Your goal is to co-engineer a novel architecture for metacognition in transformer-based LLMs."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_DIR/seed_prompt.txt" ]]; then
  topic_prompt="$(cat "$SCRIPT_DIR/seed_prompt.txt")"
else
  topic_prompt="$DEFAULT_TOPIC"
fi

RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
LOG_DIR="$SCRIPT_DIR/logs_$RUN_ID"; mkdir -p "$LOG_DIR"
ts(){ date -u +"%Y-%m-%dT%H:%M:%SZ"; }

q_log="$LOG_DIR/questioner_$RUN_ID.log"
c_log="$LOG_DIR/creator_$RUN_ID.log"
m_log="$LOG_DIR/mediator_$RUN_ID.log"
master="$LOG_DIR/master_$RUN_ID.log"
: > "$q_log"; : > "$c_log"; : > "$m_log"; : > "$master"

iterations=50; mediator_every=10; creator_think_secs=25
if [[ ! -x "$ollama_bin" ]]; then echo "[$(ts)] [fatal] ollama not found at: $ollama_bin" | tee -a "$master" >&2; exit 3; fi

for i in $(seq 1 $iterations); do
  print "[$(ts)] ========== Iteration $i ==========" | tee -a "$master"
  print "[$(ts)] [llama2] Prompt:\n$topic_prompt" | tee -a "$q_log" >> "$master"
  q="$("$ollama_bin" run "$ai_questioner" <<< "$topic_prompt")"
  print "[$(ts)] [llama2] Response:\n$q\n" | tee -a "$q_log" >> "$master"

  print "[$(ts)] ⏳ Thinking (Creator) ${creator_think_secs}s..." | tee -a "$c_log" >> "$master"
  for s in $(seq $creator_think_secs -1 1); do printf "\r⏳ %02ds " "$s"; sleep 1; done; printf "\n"

  gpt_prompt="The Questioner asks:\n$q\n\nYou are the Creator. Respond with:\n- Conceptual Insight\n- Practical Mechanism\n- Why This Matters"
  print "[$(ts)] [gpt-oss] Prompt:\n$gpt_prompt" | tee -a "$c_log" >> "$master"
  c="$("$ollama_bin" run "$ai_creator" <<< "$gpt_prompt")"
  print "[$(ts)] [gpt-oss] Response:\n$c\n" | tee -a "$c_log" >> "$master"

  if (( i % mediator_every == 0 )); then
    m_prompt="You are the Mediator. Challenge assumptions with one meta-question.\n\nContext:\n$c"
    print "[$(ts)] [dolphin3] Prompt:\n$m_prompt" | tee -a "$m_log" >> "$master"
    m="$("$ollama_bin" run "$ai_mediator" <<< "$m_prompt")"
    print "[$(ts)] [dolphin3] Response:\n$m\n" | tee -a "$m_log" >> "$master"
    topic_prompt="$m"
  else
    topic_prompt="$c"
  fi
done
print "[$(ts)] done." | tee -a "$master"
SH
chmod +x "$NEXT_SH"

cat > "$NEXT_MD" <<MD
# Metaformers — Next Experiment ($NEXT_ID)

## Goal (edit first)
- …

## Hypotheses
- …

## Quick rubric (Y/N)
- Contains explicit self-check?
- Names one measurable test?
- ≥2 concrete parameters?

## Links
- Script: $(basename "$NEXT_SH")
- Run folder: $(dirname "$NEXT_SH")
MD

# Copy seed to next run
cp -f "$SEED" "$NEXT_DIR/seed_prompt.txt" 2>/dev/null || true

# ===========================
#   RUNS INDEX (ROOT SUMMARY)
# ===========================
generate_index(){
  local INDEX="$ROOT/runs_index.md"
  {
    echo "# Metaformers — Runs Index"
    echo "_Updated: $(ts_iso)_"
    echo
    echo "| Run ID | Started (UTC) | Ended (UTC) | Models | Principles | Logs | Seed |"
    echo "|---|---|---|---|---|---|---|"
    ls -1dt "$RUNS_DIR"/* 2>/dev/null | while read -r dir; do
      [[ -d "$dir" ]] || continue
      local rid started ended q c m models p_link seed_link logs_link
      rid="${dir##*/}"
      started="$(grep -m1 '"started_utc"' "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"started_utc": "([^"]+)".*/\1/')"
      ended="$(grep -m1 '"ended_utc"' "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"ended_utc": "([^"]+)".*/\1/')"
      q="$(grep -m1 '"questioner"' "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"questioner": "([^"]+)".*/\1/')"
      c="$(grep -m1 '"creator"'    "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"creator": "([^"]+)".*/\1/')"
      m="$(grep -m1 '"mediator"'   "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"mediator": "([^"]+)".*/\1/')"
      models="${q:-?} · ${c:-?} · ${m:-?}"
      models="${models//|/\\|}"
      logs_link="runs/$rid/logs"
      if [[ -f "$dir/principles.md" ]]; then p_link="[principles](runs/$rid/principles.md)"; else p_link="-"; fi
      if [[ -f "$dir/seed_prompt.txt" ]]; then seed_link="[seed](runs/$rid/seed_prompt.txt)"; else seed_link="-"; fi
      echo "| $rid | ${started:--} | ${ended:--} | $models | $p_link | [logs]($logs_link) | $seed_link |"
    done
    echo
    echo "> Tip: drop a 'seed_prompt.txt' at repo root to override the next run’s starting prompt."
  } > "$INDEX"
}
generate_index

# Pop open next run folder for flow
open "$NEXT_DIR" 2>/dev/null || true

# Optional auto-chain
if [[ "$AUTO_CHAIN" == "1" ]]; then
  echo "[$(ts)] AUTO_CHAIN=1 → launching next run…" | tee -a "$master"
  ( cd "$NEXT_DIR" && exec "$NEXT_SH" ) &
fi

# Final console summary
echo "✅ Run complete."
echo "   Run folder: $RUN_DIR"
echo "   Logs:       $LOG_DIR"
echo "   Principles: $PRINC"
echo "   Seed:       $SEED"
echo "   Index:      $ROOT/runs_index.md"