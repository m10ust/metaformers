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
