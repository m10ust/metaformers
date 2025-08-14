#!/bin/zsh
set -e
set -o pipefail

# ========= Short Local Meta LLMs Runner =========
# Models
ai_questioner="llama2-uncensored:latest"
ai_creator="gpt-oss:20b"
ai_mediator="dolphin3:latest"
ollama_bin="/usr/local/bin/ollama"

# Run knobs
iterations="${ITERATIONS:-20}"
mediator_every="${MEDIATOR_EVERY:-5}"
creator_think_secs="${CREATOR_THINK_SECS:-10}"

# Paths
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$SCRIPT_DIR/local-meta"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
RUN_DIR="$BASE_DIR/$RUN_ID"
LOG_DIR="$RUN_DIR/logs"
mkdir -p "$LOG_DIR"

# Helpers
ts(){ date -u +"%Y-%m-%dT%H:%M:%SZ"; }

master="$LOG_DIR/master_${RUN_ID}.log"
q_log="$LOG_DIR/questioner_${RUN_ID}.log"
c_log="$LOG_DIR/creator_${RUN_ID}.log"
m_log="$LOG_DIR/mediator_${RUN_ID}.log"
: > "$master"; : > "$q_log"; : > "$c_log"; : > "$m_log"

[[ -x "$ollama_bin" ]] || { echo "[$(ts)] [fatal] ollama not found at: $ollama_bin" | tee -a "$master" >&2; exit 3; }

# Seed prompt: LOCAL LLMs metacognition
SEED_PATH="$RUN_DIR/seed_prompt.txt"
cat > "$SEED_PATH" <<'SEED'
You are three LOCAL LLMs (running offline, quantized) working collaboratively.
Your goal is to co-engineer a novel, lightweight metacognition mechanism that fits small local models (≤13B, CPU/GPU-constrained), without external APIs.

Design requirements:
- Budget-aware: ≤+10% FLOPs over a normal forward; ≤1 extra pass; no external retrieval by default.
- Memory-friendly: works with 4–8K context, gguf/quantized weights.
- Simple to port: minimal new modules; no giant second transformer.

Propose a mechanism that treats metacognition as multi-view consensus for LOCAL LLMs:
- Form K cheap internal “views” (e.g., dropout or low-rank noise) in a single step; K∈{2,4}.
- Aggregate with a tiny “Consistency Head” (pooled stats or 1 small attention block) to produce:
  (a) token-wise confidence ∈[0,1], (b) 1 “coherence” embedding.
- Use confidence to gate residuals + attention OR to set logits temperature; if confidence<τ, allow ONE bounded refinement (re-decode of the current span only).
- Log per-token confidence.

Respond in EXACTLY this format:

## Conceptual Insight
(2–4 sentences, focused on why multi-view consensus works for LOCAL LLMs)

## Practical Mechanism
1. Specify K (2 or 4), what the “view” is, and how it’s computed cheaply on-device.
2. Define the Consistency Head (ops, dims, parameters) and where it plugs in.
3. Describe gating (residual/attention) and/or temperature scaling with equations.
4. Give the single refinement rule (trigger + stop condition).
5. Provide a tiny ablation plan: K∈{1,2,4}, τ∈{0.5,0.7,0.9}.
6. State overhead targets (≤+10% FLOPs) and how you’ll keep within budget.

## Why This Matters
- Bullet on reliability/hallucination under tight compute.
- Bullet on calibration (ECE) improvement.
- Bullet on portability to 7B–13B local models.

Report:
- ECE_before / ECE_after (definition + how to approximate locally).
- Guardrails: ≤1 re-decode, no retrieval, max +10% tokens/sec slowdown.
SEED

current_prompt="$(cat "$SEED_PATH")"

# Minimal manifest
cat > "$RUN_DIR/manifest.json" <<JSON
{
  "run_id": "$RUN_ID",
  "started_utc": "$(ts)",
  "models": {
    "questioner": "$ai_questioner",
    "creator": "$ai_creator",
    "mediator": "$ai_mediator"
  },
  "iterations": $iterations,
  "mediator_every": $mediator_every,
  "seed_path": "$SEED_PATH"
}
JSON

# Function to run a model with a prompt (simple, blocking)
run_model(){
  local model="$1"; shift
  local prompt="$*"
  print -r -- "$prompt" | "$ollama_bin" run "$model"
}

# ================== MAIN LOOP ==================
for (( i=1; i<=iterations; i++ )); do
  print "[$(ts)] ========== Iteration $i ==========" | tee -a "$master"

  # Questioner
  print "[$(ts)] [llama2] Prompt:\n$current_prompt" | tee -a "$q_log" >> "$master"
  q_resp="$(run_model "$ai_questioner" "$current_prompt" || true)"
  print "[$(ts)] [llama2] Response:\n$q_resp\n" | tee -a "$q_log" >> "$master"

  # Creator countdown (optional visual)
  print "[$(ts)] ⏳ Thinking (Creator) ${creator_think_secs}s..." | tee -a "$c_log" >> "$master"
  secs=$creator_think_secs
  while (( secs > 0 )); do printf "\r⏳ %02ds " "$secs"; sleep 1; ((secs--)); done; printf "\n"

  # Creator prompt (ties to questioner output)
  c_prompt="The Questioner asks:
$q_resp

You are the Creator. Using the above LOCAL-LLM seed and constraints, respond in EXACTLY this format:

## Conceptual Insight
(2–4 sentences, focused on why multi-view consensus works for LOCAL LLMs)

## Practical Mechanism
1. Specify K (2 or 4), what the “view” is, and how it’s computed cheaply on-device.
2. Define the Consistency Head (ops, dims, parameters) and where it plugs in.
3. Describe gating (residual/attention) and/or temperature scaling with equations.
4. Give the single refinement rule (trigger + stop condition).
5. Provide a tiny ablation plan: K∈{1,2,4}, τ∈{0.5,0.7,0.9}.
6. State overhead targets (≤+10% FLOPs) and how you’ll keep within budget.

## Why This Matters
- Bullet on reliability/hallucination under tight compute.
- Bullet on calibration (ECE) improvement.
- Bullet on portability to 7B–13B local models.

Report:
- ECE_before / ECE_after (definition + how to approximate locally).
- Guardrails: ≤1 re-decode, no retrieval, max +10% tokens/sec slowdown."
  print "[$(ts)] [gpt-oss] Prompt:\n$c_prompt" | tee -a "$c_log" >> "$master"
  c_resp="$(run_model "$ai_creator" "$c_prompt" || true)"
  print "[$(ts)] [gpt-oss] Response:\n$c_resp\n" | tee -a "$c_log" >> "$master"

  # Mediator every N
  if (( i % mediator_every == 0 )); then
    m_prompt="You are the Mediator. Challenge the feasibility and compute budget of the Creator’s plan in <=80 words. End with a single question mark.

Context:
$c_resp"
    print "[$(ts)] [dolphin3] Prompt:\n$m_prompt" | tee -a "$m_log" >> "$master"
    m_resp="$(run_model "$ai_mediator" "$m_prompt" || true)"
    print "[$(ts)] [dolphin3] Response:\n$m_resp\n" | tee -a "$m_log" >> "$master"
    current_prompt="$m_resp"
  else
    current_prompt="$c_resp"
  fi
done

# Wrap up
print "[$(ts)] done." | tee -a "$master"
jq '.ended_utc = "'"$(ts)"'"' "$RUN_DIR/manifest.json" 2>/dev/null > "$RUN_DIR/manifest.json.tmp" || true
[[ -s "$RUN_DIR/manifest.json.tmp" ]] && mv "$RUN_DIR/manifest.json.tmp" "$RUN_DIR/manifest.json"

echo ""
echo "✅ Short Local Meta run complete."
echo "Run dir: $RUN_DIR"
echo "Logs: $LOG_DIR"
echo "Seed: $SEED_PATH"