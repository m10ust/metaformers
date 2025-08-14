#!/bin/zsh
set -eo pipefail

# === AI Models Configuration ===
ai_questioner="llama2-uncensored:latest"       # Asks questions
ai_creator="gpt-oss:20b"                        # Proposes ideas
ai_mediator="dolphin3:latest"                   # Asks meta-questions every 10 iterations

# Initial prompt
topic_prompt="You are three AI models working collaboratively. Your goal is to co-engineer a novel architecture for metacognition in transformer-based LLMs "

# Ollama path (set explicitly if Automator PATH is weird)
ollama_bin="/usr/local/bin/ollama"

# --- Run scaffolding (hermetic per-run folder) ---
ROOT="$PWD"
RUNS_DIR="$ROOT/runs"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
RUN_DIR="$RUNS_DIR/$RUN_ID"
LOG_DIR="$RUN_DIR/logs"
META_DIR="$RUN_DIR/meta"

mkdir -p "$LOG_DIR" "$META_DIR"

# --- Timestamped log filenames ---
logfile_questioner="$LOG_DIR/llama2_questioner_${RUN_ID}.log"
logfile_creator="$LOG_DIR/gpt_oss_creator_${RUN_ID}.log"
logfile_mediator="$LOG_DIR/dolphin3_mediator_${RUN_ID}.log"
logfile_master="$LOG_DIR/ai_master_${RUN_ID}.log"

# Initialize logs
: > "$logfile_questioner"
: > "$logfile_creator"
: > "$logfile_mediator"
: > "$logfile_master"

# --- Time helpers ---
ts_iso() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
ts() { ts_iso; }

# --- Sanity check for Ollama binary (Automator-friendly) ---
if [[ ! -x "$ollama_bin" ]]; then
  echo "[$(ts)] [fatal] ollama not found or not executable at: $ollama_bin" | tee -a "$logfile_master" >&2
  exit 3
fi

# --- Environment snapshot & manifest (for reproducibility) ---
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
  "ollama_bin": "$ollama_bin"
}
JSON

# --- Logging helpers (prefix each line with timestamp) ---
log_line() {  # file, text...
  local f="$1"; shift
  print -- "[$(ts)] $*" >> "$f"
}
log_both() {  # fileA, text...  (also mirrors to master)
  local f="$1"; shift
  local line="[$(ts)] $*"
  print -- "$line" | tee -a "$f" >> "$logfile_master"
}

iterations=50
current_prompt="$topic_prompt"

for i in {1..$iterations}; do
  log_both "$logfile_master" ""
  log_both "$logfile_master" "========== Iteration $i =========="

  ## 🧠 LLaMA2 asks a question
  log_both "$logfile_questioner" "[llama2] Prompt:"
  log_both "$logfile_questioner" "$current_prompt"
  question_response="$("$ollama_bin" run "$ai_questioner" <<< "$current_prompt")"
  log_both "$logfile_questioner" "[llama2] Response:"
  log_both "$logfile_questioner" "$question_response"

  ## ⏳ 30 sec countdown for GPT-OSS (console + logs)
  log_both "$logfile_creator" "⏳ Thinking (GPT-OSS): 30 seconds..."
  for sec in {1..30}; do
    printf "\r⏳ %02d seconds remaining..." $((30 - sec + 1))
    sleep 1
  done
  printf "\n"
  log_both "$logfile_creator" "Done. Continuing..."

  ## 🧠 GPT-OSS proposes ideas
  gpt_prompt="The Questioner asks:
$question_response

You are the Creator. Propose a novel architecture or mechanism specifically to co-engineer a novel architecture for metacognition in transformer-based LLMs. Respond with:
- Conceptual Insight
- Practical Mechanism
- Why This Matters"
  log_both "$logfile_creator" "[gpt-oss] Prompt:"
  log_both "$logfile_creator" "$gpt_prompt"
  creator_response="$("$ollama_bin" run "$ai_creator" <<< "$gpt_prompt")"
  log_both "$logfile_creator" "[gpt-oss] Response:"
  log_both "$logfile_creator" "$creator_response"

  ## 🔍 Every 10 iterations: Dolphin3 interjects
  if (( i % 10 == 0 )); then
    dolphin_prompt="You are the Mediator AI. Read the Creator’s response and challenge the underlying assumptions with a meta-question.

Context:
$creator_response"
    log_both "$logfile_mediator" "[dolphin3] Prompt:"
    log_both "$logfile_mediator" "$dolphin_prompt"
    mediator_response="$("$ollama_bin" run "$ai_mediator" <<< "$dolphin_prompt")"
    log_both "$logfile_mediator" "[dolphin3] Response:"
    log_both "$logfile_mediator" "$mediator_response"
    current_prompt="$mediator_response"
  else
    current_prompt="$creator_response"
  fi
done

# --- Close out manifest with end time (inject ended_utc safely, no jq) ---
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

# --- Auto-distill: principles.md from last few Creator outputs ---
PRINC="$RUN_DIR/principles.md"
SEED="$RUN_DIR/seed_prompt.txt"
N_CREATOR=6          # how many latest Creator responses to mine
MAX_KEEP=12          # max lines to keep per candidate

{
  echo "# Principles — distilled from last ${N_CREATOR} Creator outputs"
  echo "_Run: $RUN_ID (UTC)_"
  echo

  # Extract Creator response blocks and keep only the latest N_CREATOR
  /usr/bin/awk -v cap="$N_CREATOR" -v maxk="$MAX_KEEP" '
    function flush_block(){
      if (collecting) { idx++; buf[idx]=block; block=""; collecting=0 }
    }
    # Start collecting after a Response header
    /^\[[^]]*\] \[gpt-oss\] Response:/ { flush_block(); collecting=1; next }
    # Stop when a new Prompt header appears
    /^\[[^]]*\] \[gpt-oss\] Prompt:/   { flush_block(); next }
    { if (collecting) { block = block $0 "\n" } }
    END { flush_block();
      # choose window
      start = (idx-cap+1); if (start < 1) start=1;
      # For each candidate, filter useful lines
      for (i=start; i<=idx; i++) {
        print "## Candidate #" i;
        n = split(buf[i], lines, "\n");
        kept=0;
        for (j=1; j<=n; j++) {
          line = lines[j];
          # keep bullets, numbered steps, or lines with eval/params keywords or numbers
          if (line ~ /^([[:space:]]*[-*•]|[[:space:]]*[0-9]+\.)/ \
              || line ~ /(measure|evaluate|ablation|failure|constraint|verify|threshold|rank|prob|mask|consistency|calibration|plan|step|test|score|risk|OOD|out[- ]of[- ]distribution|self-?check|reflect|rubric)/ \
              || line ~ /([0-9]+(\.[0-9]+)?%|[0-9]+(ms|s|k|M|B| tokens?))/) {
            # normalize leading whitespace
            sub(/^[[:space:]]+/, "", line);
            if (length(line) > 0 && kept < maxk) {
              print "- " line;
              kept++;
            }
          }
        }
        print "";
      }
      # Build a simple consolidated list from the latest candidate only
      if (idx >= start) {
        latest = buf[idx];
        print "## Next-run seed (latest distilled)";
        n2 = split(latest, L, "\n");
        outc=0;
        for (j=1; j<=n2; j++) {
          line=L[j];
          if (line ~ /^([[:space:]]*[-*•]|[[:space:]]*[0-9]+\.)/ \
              || line ~ /(measure|evaluate|ablation|failure|constraint|verify|threshold|rank|prob|mask|consistency|calibration|plan|step|test|score|risk|OOD|out[- ]of[- ]distribution|self-?check|reflect|rubric)/ \
              || line ~ /([0-9]+(\.[0-9]+)?%|[0-9]+(ms|s|k|M|B| tokens?))/) {
            sub(/^[[:space:]]+/, "", line);
            if (length(line) > 0 && outc < 10) { seed[outc++] = line }
          }
        }
        if (outc > 0) {
          for (k=0; k<outc; k++) print "- " seed[k] > "/dev/stderr"; # send seed bullets to stderr for capture outside
        }
      }
    }
  ' "$logfile_creator"
} > "$PRINC" 2> "$SEED.tmp"

# Wrap the seed bullets into a tiny prompt file
if [[ -s "$SEED.tmp" ]]; then
  {
    echo "You are three AI models working collaboratively to improve metacognition in transformer LLMs."
    echo "Start from these distilled principles:"
    echo
    sed 's/^/- /' < /dev/null >/dev/null  # noop to ensure sed exists
    cat "$SEED.tmp" | sed 's/^/- /'
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

log_line "$logfile_master" "Distilled: $PRINC"
log_line "$logfile_master" "Seed prompt: $SEED"

log_line "$logfile_master" "Run complete: RUN_ID=$RUN_ID"
echo "✅ Hermetic run complete."
echo "   Run folder: $RUN_DIR"
echo "   Logs:       $LOG_DIR"
echo "   Principles: $PRINC"
echo "   Seed:       $SEED"

# --- Post-run scaffolding: next script + notes (flow-state ready) ---
NEXT_ID="$(date -u +%Y%m%d-%H%M%S)"
ROOT_DIR="$PWD"
NEXT_DIR="$ROOT_DIR/runs/$NEXT_ID"
mkdir -p "$NEXT_DIR"

NEXT_SH="$NEXT_DIR/next_experiment_${NEXT_ID}.sh"
NEXT_MD="$NEXT_DIR/NOTES_${NEXT_ID}.md"

cat > "$NEXT_SH" <<'SH'
#!/bin/zsh
set -eo pipefail

# === Metaformers quick scaffold ===
ai_questioner="llama2-uncensored:latest"
ai_creator="gpt-oss:20b"
ai_mediator="dolphin3:latest"

topic_prompt="You are three AI models working collaboratively. Your goal is: <edit me>"

ollama_bin="/usr/local/bin/ollama"

# logs live alongside this file
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
LOG_DIR="$(cd "$(dirname "$0")" && pwd)/logs_$RUN_ID"
mkdir -p "$LOG_DIR"
ts(){ date -u +"%Y-%m-%dT%H:%M:%SZ"; }

q_log="$LOG_DIR/questioner_$RUN_ID.log"
c_log="$LOG_DIR/creator_$RUN_ID.log"
m_log="$LOG_DIR/mediator_$RUN_ID.log"
master="$LOG_DIR/master_$RUN_ID.log"
: > "$q_log"; : > "$c_log"; : > "$m_log"; : > "$master"

iterations=30
current_prompt="$topic_prompt"

for i in {1..$iterations}; do
  print "[$(ts)] ========== Iteration $i ==========" | tee -a "$master"
  print "[$(ts)] [llama2] Prompt:\n$current_prompt" | tee -a "$q_log" >> "$master"
  q="$("$ollama_bin" run "$ai_questioner" <<< "$current_prompt")"
  print "[$(ts)] [llama2] Response:\n$q\n" | tee -a "$q_log" >> "$master"

  print "[$(ts)] ⏳ Thinking (Creator) 25s..." | tee -a "$c_log" >> "$master"
  for s in {1..25}; do printf "\r⏳ %02d" $((25-s+1)); sleep 1; done; printf "\n"

  gpt_prompt="The Questioner asks:\n$q\n\nYou are the Creator. Respond with:\n- Conceptual Insight\n- Practical Mechanism\n- Why This Matters"
  print "[$(ts)] [gpt-oss] Prompt:\n$gpt_prompt" | tee -a "$c_log" >> "$master"
  c="$("$ollama_bin" run "$ai_creator" <<< "$gpt_prompt")"
  print "[$(ts)] [gpt-oss] Response:\n$c\n" | tee -a "$c_log" >> "$master"

  if (( i % 10 == 0 )); then
    m_prompt="You are the Mediator. Challenge assumptions with one meta-question.\n\nContext:\n$c"
    print "[$(ts)] [dolphin3] Prompt:\n$m_prompt" | tee -a "$m_log" >> "$master"
    m="$("$ollama_bin" run "$ai_mediator" <<< "$m_prompt")"
    print "[$(ts)] [dolphin3] Response:\n$m\n" | tee -a "$m_log" >> "$master"
    current_prompt="$m"
  else
    current_prompt="$c"
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

# If we produced a seed prompt this run, copy it beside the next script for convenience
if [[ -f "$SEED" ]]; then
  cp "$SEED" "$NEXT_DIR/seed_prompt.txt"
fi

# Pop open the next-run folder so you stay in flow
open "$(dirname "$NEXT_SH")"