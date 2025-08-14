#!/bin/zsh
set -euo pipefail

# Minimal Metaformers builder script for macOS/BSD

ollama_bin="${OLLAMA_BIN:-/usr/local/bin/ollama}"
model_questioner="${QUESTIONER_MODEL:-llama2-uncensored:latest}"
model_creator="${CREATOR_MODEL:-gpt-oss:latest}"

ROOT="$PWD"
RUNS_DIR="$ROOT/runs"
LOCAL_META="$ROOT/local-meta"
mkdir -p "$RUNS_DIR" "$LOCAL_META"

RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
RUN_DIR="$RUNS_DIR/$RUN_ID"
LOG_DIR="$RUN_DIR/logs"
mkdir -p "$LOG_DIR"

Q_LOG="$LOG_DIR/questioner.log"
C_LOG="$LOG_DIR/creator.log"

ALLOW_CMDS=(echo printf cat tee head tail awk sed cut tr sort uniq wc ls mkdir rm mv cp ln find xargs grep)

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { print -- "[$(ts)] $*" }

ollama_run() {
  local model="$1"; shift
  local prompt="$*"
  print -r -- "$prompt" | "$ollama_bin" run "$model"
}

is_allowed_cmd() {
  local line="$1"
  local first="${line%% *}"
  first="${first##*/}"
  for cmd in "${ALLOW_CMDS[@]}"; do
    [[ "$first" == "$cmd" ]] && return 0
  done
  return 1
}

safe_write_file() {
  local path="$1"
  local content="$2"
  case "$path" in
    "$LOCAL_META"/*) ;;
    *) echo "Refusing to write outside $LOCAL_META: $path" >&2; return 1 ;;
  esac
  mkdir -p "$(dirname "$path")"
  print -r -- "$content" > "$path"
}

safe_exec() {
  local line="$1"
  if ! is_allowed_cmd "$line"; then
    echo "Skipping disallowed command: $line" >&2
    return 1
  fi
  eval "$line"
}

# Step 1: Get topic
PROMPT_TOPIC='Propose a single, concrete topic for building/improving a local, self-improving LLM stack on one Mac mini (no cloud). Return ONLY a short title (≤12 words), no preamble, no list.'
topic="$(ollama_run "$model_questioner" "$PROMPT_TOPIC" | head -n 1)"
echo "$topic" | tee "$Q_LOG"

# Step 2: Get plan
read -r -d '' PROMPT_PLAN <<EOF
You are the Creator AI. Given the topic below, output a precise, executable local plan.
macOS constraints: BSD awk/sed; write files ONLY under ./local-meta; shell is zsh.

Format EXACTLY:

### Topic
$topic

### Files
For each file to write/update, use fenced blocks:
\`\`\`file ./local-meta/relative/path.ext
<entire file content>
\`\`\`

### Commands
List shell commands to run, one per line, no explanations.

IMPORTANT: You must include at least one file and one command.
EOF

plan="$(ollama_run "$model_creator" "$PROMPT_PLAN")"
echo "$plan" > "$C_LOG"

echo "===== PLAN OUTPUT FROM MODEL ====="
cat "$C_LOG"
echo "=================================="

# Step 2.5: Fallback if model output invalid
if ! grep -q "### Commands" <<< "$plan" || ! grep -q "### Files" <<< "$plan"; then
    echo "[WARN] Model did not produce valid plan format. Using fallback."
    plan="### Topic
$topic

### Files
\`\`\`file ./local-meta/README.txt
Test file for topic: $topic
\`\`\`

### Commands
echo 'Default build for $topic'
"
fi

# Step 3: Write files
in_file_block=0
file_path=""
file_content=""

while IFS= read -r line; do
  if [[ "$line" =~ ^\`\`\`file\ (.+)$ ]]; then
    in_file_block=1
    file_path="${BASH_REMATCH[1]}"
    file_content=""
  elif [[ "$line" == '```' && $in_file_block -eq 1 ]]; then
    in_file_block=0
    safe_write_file "$file_path" "$file_content" || echo "Failed to write $file_path" >&2
  elif [[ $in_file_block -eq 1 ]]; then
    file_content+="$line"$'\n'
  fi
done <<< "$plan"

# Step 4: Execute commands
commands_section=0
while IFS= read -r line; do
  if [[ "$line" == "### Commands" ]]; then
    commands_section=1
    continue
  fi
  if [[ $commands_section -eq 1 ]]; then
    [[ -z "$line" ]] && continue
    safe_exec "$line" || echo "Command failed: $line" >&2
  fi
done <<< "$plan"