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

# --- Environment manifest (for reproducibility) ---
ts_iso() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
ts() { ts_iso; }

# Snapshot versions
{
  echo "OLLAMA_BIN=$ollama_bin"
  "$ollama_bin" --version 2>&1 || true
  "$ollama_bin" list 2>&1 || true
} > "$META_DIR/version.txt"

# Minimal manifest.json
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

# Close out manifest with end time
tmp_manifest="$RUN_DIR/manifest.json.tmp"
awk 'NR==FNR{print; next} {print}' "$RUN_DIR/manifest.json" /dev/null > "$tmp_manifest"
mv "$tmp_manifest" "$RUN_DIR/manifest.json"
echo "{\"ended_utc\": \"$(ts_iso)\"}" >> "$META_DIR/version.txt"

log_line "$logfile_master" "Run complete: RUN_ID=$RUN_ID"
echo "✅ Hermetic run complete."
echo "   Run folder: $RUN_DIR"
echo "   Logs:       $LOG_DIR"

