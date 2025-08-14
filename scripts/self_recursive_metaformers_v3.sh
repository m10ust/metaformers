#!/bin/zsh
set -eo pipefail

# ===========================
# Metaformers — self-recursive v3.0 (fixed)
# ===========================
# Local, hermetic, Automator-safe. No Homebrew deps. Uses BSD awk/sed.
# v3 adds: scoring, selection pressure, mutations, leaderboard, lineage.
# This version fixes zsh parse issues via heredocs for AWK and careful quoting.

# === AI Models (local) ===
ai_questioner="llama2-uncensored:latest"     # Asks
ai_creator="gpt-oss:20b"                      # Proposes
ai_mediator="dolphin3:latest"                 # Meta every N
ai_judge="llama2-uncensored:latest"           # Tiny judge for 0–10 self-critique score

# === Ollama binary ===
ollama_bin="/usr/local/bin/ollama"

# === Iteration & cadence ===
iterations=60
mediator_every=10
creator_think_secs=30

# === Seed behavior ===
TOPIC_DEFAULT="Your task is to explore and discover the most effective possible prompts for guiding and enhancing a recursive chain of LLMs in order to achieve deep metacognition, self-improvement, and emergent capabilities. Your output should not just be a list of prompts, but a reasoned framework for identifying, testing, and refining them. Think of this as finding 'the right question' in its purest form."
: "${AUTO_CHAIN:=0}"

# --- Repo roots / hermetic folders ---
ROOT="$PWD"
RUNS_DIR="$ROOT/runs"
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
RUN_DIR="$RUNS_DIR/$RUN_ID"
LOG_DIR="$RUN_DIR/logs"
META_DIR="$RUN_DIR/meta"
CAND_DIR="$RUN_DIR/candidates"
mkdir -p "$LOG_DIR" "$META_DIR" "$CAND_DIR"

# --- Timestamp helpers ---
ts_iso(){ date -u +"%Y-%m-%dT%H:%M:%SZ"; }
ts(){ ts_iso; }

# --- Logs ---
q_log="$LOG_DIR/llama2_questioner_${RUN_ID}.log"
c_log="$LOG_DIR/gpt_oss_creator_${RUN_ID}.log"
m_log="$LOG_DIR/dolphin3_mediator_${RUN_ID}.log"
master="$LOG_DIR/ai_master_${RUN_ID}.log"
: > "$q_log"; : > "$c_log"; : > "$m_log"; : > "$master"

log_line(){ print -- "[$(ts)] $*" >> "$1"; }
log_both(){
  local target="$1"; shift
  local line="[$(ts)] $*"
  if [[ "$target" == "$master" ]]; then
    print -- "$line" >> "$master"
  else
    print -- "$line" | tee -a "$target" >> "$master"
  fi
}

# --- Sanity ---
[[ -x "$ollama_bin" ]] || { echo "[$(ts)] [fatal] ollama not found at: $ollama_bin" | tee -a "$master" >&2; exit 3; }

# --- Timeout wrapper for ollama run ---
ollama_run(){
  local model="$1"; shift
  local prompt="$*"
  ( print -r -- "$prompt" | "$ollama_bin" run "$model" ) &
  local pid=$!
  local limit=$((5*60))
  while kill -0 $pid 2>/dev/null; do
    sleep 1
    ((limit--))
    if (( limit<=0 )); then
      kill -TERM $pid 2>/dev/null || true
      sleep 1
      kill -KILL $pid 2>/dev/null || true
      echo "[timeout] ollama run exceeded 5m for model=$model" >&2
      return 124
    fi
  done
  wait $pid
}

# --- Seed prompt (override > root seed > last-run seed > default) + parent lineage ---
current_prompt="$TOPIC_DEFAULT"
PARENT_ID="$(ls -1dt "$RUNS_DIR"/* 2>/dev/null | head -n1 | xargs -n1 basename 2>/dev/null || true)"
if [[ -n "${TOPIC_OVERRIDE:-}" ]]; then
  current_prompt="$TOPIC_OVERRIDE"
elif [[ -f "$ROOT/seed_prompt.txt" ]]; then
  current_prompt="$(cat "$ROOT/seed_prompt.txt")"
else
  last_run="$(ls -1dt "$RUNS_DIR"/* 2>/dev/null | grep -v "$RUN_ID" | head -n1 || true)"
  if [[ -n "$last_run" && -f "$last_run/seed_prompt.txt" ]]; then
    current_prompt="$(cat "$last_run/seed_prompt.txt")"
    PARENT_ID="$(basename "$last_run")"
  fi
fi

# --- Env snapshot & manifest ---
{
  echo "OLLAMA_BIN=$ollama_bin"
  "$ollama_bin" --version 2>&1 || true
  "$ollama_bin" list 2>&1 || true
} > "$META_DIR/version.txt"

cat > "$RUN_DIR/manifest.json" <<JSON
{
  "run_id": "$RUN_ID",
  "parent_run_id": "${PARENT_ID:-}",
  "started_utc": "$(ts_iso)",
  "cwd": "$ROOT",
  "models": {
    "questioner": "$ai_questioner",
    "creator": "$ai_creator",
    "mediator": "$ai_mediator",
    "judge": "$ai_judge"
  },
  "iterations": $iterations,
  "mediator_every": $mediator_every,
  "creator_think_secs": $creator_think_secs,
  "ollama_bin": "$ollama_bin",
  "seed_source": "$(print -nr -- "$current_prompt" | head -c 64 | sed 's/\"/\\\"/g')..."
}
JSON

# ===========================
#                MAIN LOOP
# ===========================
integer i
for (( i = 1; i <= iterations; i++ )); do
  log_both "$master" ""
  log_both "$master" "========== Iteration $i =========="

  # Questioner
  log_both "$q_log" "[llama2] Prompt:"
  log_both "$q_log" "$current_prompt"
  q_resp="$(ollama_run "$ai_questioner" "$current_prompt" || true)"
  log_both "$q_log" "[llama2] Response:"
  log_both "$q_log" "$q_resp"

  # Creator — BSD-safe countdown
  integer s=$creator_think_secs
  log_both "$c_log" "⏳ Thinking (Creator): ${creator_think_secs}s..."
  while (( s > 0 )); do
    printf "\r⏳ %02ds " "$s"
    sleep 1
    (( s-- ))
  done
  printf "\n"
  log_both "$c_log" "Continuing."

  c_prompt="The Questioner asks:
$q_resp

You are the Creator. Propose a novel architecture or mechanism to improve metacognition in transformer-based LLMs.
Respond in EXACTLY this format:

## Conceptual Insight
(2–4 sentences)

## Practical Mechanism
1. Step ...
2. Step ...
3. Step ...
4. Step ...

## Why This Matters
- Bullet
- Bullet
- Bullet"
  log_both "$c_log" "[gpt-oss] Prompt:"; log_both "$c_log" "$c_prompt"
  c_resp="$(ollama_run "$ai_creator" "$c_prompt" || true)"
  log_both "$c_log" "[gpt-oss] Response:"; log_both "$c_log" "$c_resp"

  # Mediator every N
  if (( i % mediator_every == 0 )); then
    m_prompt="You are the Mediator AI. Read the Creator’s response and challenge the underlying assumptions with one concise meta-question (≤80 words). End with a single question mark.

Context:
$c_resp"
    log_both "$m_log" "[dolphin3] Prompt:"; log_both "$m_log" "$m_prompt"
    m_resp="$(ollama_run "$ai_mediator" "$m_prompt" || true)"
    log_both "$m_log" "[dolphin3] Response:"; log_both "$m_log" "$m_resp"
    current_prompt="$m_resp"
  else
    current_prompt="$c_resp"
  fi
done

# --- Close manifest with end time ---
END_TS="$(ts_iso)"
tmp_manifest="$RUN_DIR/manifest.json.tmp"
awk -v end="$END_TS" 'BEGIN{d=0}{ if(!d && $0 ~ /}\s*$/){ sub(/}\s*$/, ",\n  \"ended_utc\": \"" end "\"\n}"); d=1 } print }' "$RUN_DIR/manifest.json" > "$tmp_manifest" && mv "$tmp_manifest" "$RUN_DIR/manifest.json"
echo "ended_utc=$END_TS" >> "$META_DIR/version.txt"

# ===========================
#   DISTILL CANDIDATES (last N Creator blocks)
# ===========================
PRINC="$RUN_DIR/principles.md"
SEED="$RUN_DIR/seed_prompt.txt"
N_CREATOR=8
MAX_KEEP=12

# Extract last N Creator responses into $CAND_DIR/cand_*.txt  (heredoc to avoid quote pitfalls)
 /usr/bin/awk -v cap="$N_CREATOR" -v outdir="$CAND_DIR" -v maxk="$MAX_KEEP" <<'AWK' "$c_log"
function flush_block(){ if (collecting){ idx++; buf[idx]=block; block=""; collecting=0 } }
$0 ~ /^\[[^]]*\] \[gpt-oss\] Response:/ { flush_block(); collecting=1; next }
$0 ~ /^\[[^]]*\] \[gpt-oss\] Prompt:/   { flush_block(); next }
{ if (collecting) { block = block $0 "\n" } }
END {
  flush_block();
  start = (idx-cap+1); if (start < 1) start=1;
  for (i=start; i<=idx; i++) {
    fn=sprintf("%s/cand_%02d.txt", outdir, i);
    print buf[i] > fn; close(fn);
  }
}
AWK

# Build principles.md and next-run seed lines  (also heredoc)
{
  echo "# Principles — distilled from last ${N_CREATOR} Creator outputs"
  echo "_Run: $RUN_ID (UTC)_"
  echo
  /usr/bin/awk -v cap="$N_CREATOR" -v maxk="$MAX_KEEP" <<'AWK' "$c_log"
function flush_block(){ if (collecting){ idx++; buf[idx]=block; block=""; collecting=0 } }
$0 ~ /^\[[^]]*\] \[gpt-oss\] Response:/ { flush_block(); collecting=1; next }
$0 ~ /^\[[^]]*\] \[gpt-oss\] Prompt:/   { flush_block(); next }
{ if (collecting) { block = block $0 "\n" } }
END {
  flush_block();
  start = (idx-cap+1); if (start < 1) start=1;
  for (i=start; i<=idx; i++) {
    print "## Candidate #" i;
    n = split(buf[i], lines, "\n"); kept=0;
    for (j=1; j<=n; j++) {
      line = lines[j]; gsub(/\r/,"", line);
      if (line ~ /^(Thinking\.\.\.|We need to|Let'?s produce|done thinking|Analysis:)/) continue;
      if (line ~ /^([[:space:]]*[-*•]|[[:space:]]*[0-9]+\.)/
          || line ~ /(measure|evaluate|ablation|failure|constraint|verify|threshold|rank|prob|mask|consistency|calibration|plan|step|test|score|risk|OOD|out[- ]of[- ]distribution|self-?check|reflect|rubric)/
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
      line=L[j]; gsub(/\r/,"", line);
      if (line ~ /^(Thinking\.\.\.|We need to|Let'?s produce|done thinking|Analysis:)/) continue;
      if (line ~ /^([[:space:]]*[-*•]|[[:space:]]*[0-9]+\.)/
          || line ~ /(measure|evaluate|ablation|failure|constraint|verify|threshold|rank|prob|mask|consistency|calibration|plan|step|test|score|risk|OOD|out[- ]of[- ]distribution|self-?check|reflect|rubric)/
          || line ~ /([0-9]+(\.[0-9]+)?%|[0-9]+(ms|s|k|M|B| tokens?))/) {
        sub(/^[[:space:]]+/, "", line);
        if (length(line) > 0 && outc < 10) { seed[++outc] = line }
      }
    }
    if (outc > 0) { for (k=1; k<=outc; k++) print seed[k] > "/dev/stderr"; }
  }
}
AWK
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

# ===========================
#   SCORING & SELECTION
# ===========================
LEADER="$RUN_DIR/leaderboard.md"
REF_CORPUS="$RUN_DIR/ref_corpus.txt"

# Build reference corpus from prior run principles + seed if available
{
  last_run="$(ls -1dt "$RUNS_DIR"/* 2>/dev/null | grep -v "$RUN_ID" | head -n1 || true)"
  if [[ -n "$last_run" && -f "$last_run/principles.md" ]]; then cat "$last_run/principles.md"; fi
  if [[ -n "$last_run" && -f "$last_run/seed_prompt.txt" ]]; then echo; cat "$last_run/seed_prompt.txt"; fi
} > "$REF_CORPUS" 2>/dev/null || true

# Function: 3-gram Jaccard similarity (awk)
jaccard(){ # fileA fileB -> echo float 0..1
  /usr/bin/awk '
    function norm(x){ gsub(/[^A-Za-z0-9]+/," ",x); return tolower(x) }
    function add_trigrams(str, arr,   n,i,w1,w2,w3){
      str=norm(str); n=split(str, t, /[[:space:]]+/); if(n<3) return;
      for(i=1;i<=n-2;i++){ w1=t[i]; w2=t[i+1]; w3=t[i+2]; if(w1==""||w2==""||w3=="") continue; arr[w1"\t"w2"\t"w3]=1 }
    }
    FNR==NR{ add_trigrams($0,A); next }
    { add_trigrams($0,B) }
    END{
      for(k in A){ if(k in B) i++ }
      for(k in A) u++
      for(k in B){ if(!(k in A)) u++ }
      if(u==0) print 0; else printf("%.6f", i/u)
    }
  ' "$1" "$2"
}

# Judge score (0-10) via LLaMA2; returns integer or 0 on failure
judge(){
  local text="$1"
  local prompt="Rate the following proposal for metacognitive rigor on a 0-10 scale. Consider clarity, testability, calibration/ablation mentions, and feasibility. Respond with ONLY an integer 0-10.

---
$text
---
Score:"
  local out
  out="$(ollama_run "$ai_judge" "$prompt" || true)"
  echo "$out" | tr -cd '0-9' | sed -E 's/^([0-9]{1,2}).*$/\1/' | awk '{s=$0+0; if(s>10) s=10; print s+0}' 2>/dev/null || echo 0
}

# Structure & signal score from text (awk)
score_struct_signal(){ # stdin -> echo "struct signal"
  /usr/bin/awk '
    BEGIN{hasCI=0; hasPM=0; hasWM=0; nums=0; steps=0; bullets=0; sig=0}
    {
      line=$0
      if(line ~ /^##[[:space:]]*Conceptual[[:space:]]*Insight/i) hasCI=1
      if(line ~ /^##[[:space:]]*Practical[[:space:]]*Mechanism/i) hasPM=1
      if(line ~ /^##[[:space:]]*Why[[:space:]]*This[[:space:]]*Matters/i) hasWM=1
      if(line ~ /^[[:space:]]*[0-9]+\./) steps++
      if(line ~ /^[[:space:]]*[-*•][[:space:]]+/) bullets++
      if(line ~ /([0-9]+(\.[0-9]+)?%|[0-9]+(ms|s|tokens|k|M|B))/) nums++
      if(line ~ /(measure|evaluate|ablation|calibration|threshold|error|uncertainty|confidence|rubric|score|metric|risk|OOD|out[- ]of[- ]distribution|self-?check|reflect)/i) sig++
    }
    END{
      struct=0
      if(hasCI && hasPM && hasWM) struct+=0.5
      if(steps>=4 && steps<=12) struct+=0.3
      if(bullets>=3) struct+=0.2
      if(struct>1) struct=1
      s = sig*0.05 + nums*0.05; if(s>1) s=1
      printf("%.3f %.3f\n", struct, s)
    }
  '
}

# Iterate candidates, compute scores, build leaderboard
{
  echo "# Leaderboard — $RUN_ID"
  echo
  echo "| Rank | File | Novelty | Structure | Signal | Judge | Total |"
  echo "|---:|---|---:|---:|---:|---:|---:|"
} > "$LEADER"

best_total=0; best_file=""; rank=1
for f in $(ls -1 "$CAND_DIR"/cand_*.txt 2>/dev/null | tail -n "$N_CREATOR" 2>/dev/null); do
  text="$(cat "$f")"
  # novelty = 1 - jaccard(candidate, ref_corpus)
  if [[ -s "$REF_CORPUS" ]]; then j="$(jaccard "$f" "$REF_CORPUS")"; else j=0; fi
  novelty=$(awk -v x="$j" 'BEGIN{n=1.0-x; if(n<0)n=0; if(n>1)n=1; printf("%.3f",n)}')
  read struct signal < <(print -nr -- "$text" | score_struct_signal)
  judge_score="$(judge "$text" 2>/dev/null || echo 0)"
  total=$(awk -v n="$novelty" -v a="$struct" -v b="$signal" -v j="$judge_score" 'BEGIN{t=0.35*n + 0.25*a + 0.20*b + 0.20*(j/10.0); printf("%.3f", t)}')
  printf "| %3d | %s | %s | %s | %s | %s | %s |\n" "$rank" "$(basename "$f")" "$novelty" "$struct" "$signal" "$judge_score" "$total" >> "$LEADER"
  if awk -v t="$total" -v bt="$best_total" 'BEGIN{exit !(t>bt)}'; then best_total="$total"; best_file="$f"; fi
  rank=$((rank+1))
done

# ===========================
#   SEED SELECTION + MUTATIONS
# ===========================
if [[ -n "$best_file" && -s "$best_file" ]]; then
  # Base seed from best candidate + light wrapper
  {
    echo "You are three AI models working collaboratively to improve metacognition in transformer LLMs."
    echo "Adopt the following distilled directives:"
    echo
    print -nr -- "$(cat "$best_file")" | sed 's/^/- /' | sed 's/^-- /- /'
    echo
    echo "Respond with:"
    echo "- Conceptual Insight (2–4 sentences)"
    echo "- Practical Mechanism (4–8 numbered steps)"
    echo "- Why This Matters (3 bullets)"
  } > "$SEED"

  # Mutations
  {
    cat "$SEED"
    echo
    echo "Additional constraints:"
    echo "- Report Expected Calibration Error (ECE) before/after."
    echo "- Include one ablation plan with a measurable threshold."
    echo "- State a stop-condition for the controller."
  } > "$RUN_DIR/alt_seed_1.txt"

  {
    cat "$SEED"
    echo
    echo "Additional constraints:"
    echo "- Add a failure-mode guard (max 1 re-decode pass, 2 retrieval hops)."
    echo "- Output an uncertainty budget (where the error likely comes from)."
    echo "- Propose a small held-out OOD test."
  } > "$RUN_DIR/alt_seed_2.txt"
else
  # Fallback: keep distilled seed as-is
  cp -f "$SEED" "$SEED" 2>/dev/null || true
fi

# Seed hash → manifest
SEED_SHA="$({ /sbin/shasum -a 256 "$SEED" 2>/dev/null || shasum -a 256 "$SEED" 2>/dev/null; } | awk '{print $1}')"
tmp_manifest="$RUN_DIR/manifest.json.tmp"
awk -v seedsha="$SEED_SHA" 'BEGIN{d=0}{ if(!d && $0 ~ /}\s*$/){ sub(/}\s*$/, ",\n  \"seed_sha256\": \"" seedsha "\"\n}"); d=1 } print }' "$RUN_DIR/manifest.json" > "$tmp_manifest" && mv "$tmp_manifest" "$RUN_DIR/manifest.json"

log_line "$master" "Principles: $PRINC"
log_line "$master" "Seed: $SEED (sha256=$SEED_SHA)"
log_line "$master" "Leaderboard: $LEADER"
log_line "$master" "Mutations: alt_seed_1.txt, alt_seed_2.txt"

# ===========================
#   NEXT-RUN SCAFFOLD (single child; you can launch alts manually)
# ===========================
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
if [[ -f "$SCRIPT_DIR/seed_prompt.txt" ]]; then topic_prompt="$(cat "$SCRIPT_DIR/seed_prompt.txt")"; else topic_prompt="$DEFAULT_TOPIC"; fi
RUN_ID="$(date -u +%Y%m%d-%H%M%S)"
LOG_DIR="$SCRIPT_DIR/logs_$RUN_ID"
mkdir -p "$LOG_DIR"
ts(){ date -u +"%Y-%m-%dT%H:%M:%SZ"; }
q_log="$LOG_DIR/questioner_$RUN_ID.log"
c_log="$LOG_DIR/creator_$RUN_ID.log"
m_log="$LOG_DIR/mediator_$RUN_ID.log"
master="$LOG_DIR/master_$RUN_ID.log"
: > "$q_log"; : > "$c_log"; : > "$m_log"; : > "$master"
iterations=50; mediator_every=10; creator_think_secs=25
[[ -x "$ollama_bin" ]] || { echo "[$(ts)] [fatal] ollama not found at: $ollama_bin" | tee -a "$master" >&2; exit 3; }

integer i
for (( i = 1; i <= iterations; i++ )); do
  print "[$(ts)] ========== Iteration $i ==========" | tee -a "$master"
  print "[$(ts)] [llama2] Prompt:\n$topic_prompt" | tee -a "$q_log" >> "$master"
  q="$("$ollama_bin" run "$ai_questioner" <<< "$topic_prompt")"
  print "[$(ts)] [llama2] Response:\n$q\n" | tee -a "$q_log" >> "$master"

  integer s=$creator_think_secs
  print "[$(ts)] ⏳ Thinking (Creator) ${creator_think_secs}s..." | tee -a "$c_log" >> "$master"
  while (( s > 0 )); do printf "\r⏳ %02ds " "$s"; sleep 1; (( s-- )); done; printf "\n"

  gpt_prompt="The Questioner asks:
$q

You are the Creator. Respond in EXACTLY this format:

## Conceptual Insight
(2–4 sentences)

## Practical Mechanism
1. Step ...
2. Step ...
3. Step ...
4. Step ...

## Why This Matters
- Bullet
- Bullet
- Bullet"
  print "[$(ts)] [gpt-oss] Prompt:\n$gpt_prompt" | tee -a "$c_log" >> "$master"
  c="$("$ollama_bin" run "$ai_creator" <<< "$gpt_prompt")"
  print "[$(ts)] [gpt-oss] Response:\n$c\n" | tee -a "$c_log" >> "$master"

  if (( i % mediator_every == 0 )); then
    m_prompt="You are the Mediator. Challenge assumptions with one meta-question (≤80 words). End with a question mark.

Context:
$c"
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

# Place chosen seed next to next script; keep alternates in parent run
cp -f "$SEED" "$NEXT_DIR/seed_prompt.txt" 2>/dev/null || true

# ===========================
#   RUNS INDEX (ROOT SUMMARY)
# ===========================
INDEX="$ROOT/runs_index.md"

# 1) Header
{
  printf "# Metaformers — Runs Index\n"
  printf "_Updated: %s_\n\n" "$(ts_iso)"
  printf "| Run ID | Parent | Started (UTC) | Ended (UTC) | Models | Principles | Leaderboard | Logs | Seed |\n"
  printf "|---|---|---|---|---|---|---|---|---|\n"
} > "$INDEX"

# 2) Rows
for dir in "$RUNS_DIR"/*; do
  [[ -d "$dir" ]] || continue
  rid="${dir##*/}"

  started="$(grep -m1 '"started_utc"' "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"started_utc": "([^"]+)".*/\1/')"
  ended="$(grep -m1 '"ended_utc"' "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"ended_utc": "([^"]+)".*/\1/')"
  parent="$(grep -m1 '"parent_run_id"' "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"parent_run_id": "([^"]*)".*/\1/')"

  q="$(grep -m1 '"questioner"' "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"questioner": "([^"]+)".*/\1/')"
  c="$(grep -m1 '"creator"'    "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"creator": "([^"]+)".*/\1/')"
  m="$(grep -m1 '"mediator"'   "$dir/manifest.json" 2>/dev/null | sed -E 's/.*"mediator": "([^"]+)".*/\1/')"
  models="${q:-?} · ${c:-?} · ${m:-?}"
  models="${models//|/\\|}"

  p_link="-";   [[ -f "$dir/principles.md"    ]] && p_link="[principles](runs/$rid/principles.md)"
  ldr_link="-"; [[ -f "$dir/leaderboard.md"   ]] && ldr_link="[leaderboard](runs/$rid/leaderboard.md)"
  seed_link="-";[[ -f "$dir/seed_prompt.txt"  ]] && seed_link="[seed](runs/$rid/seed_prompt.txt)"
  logs_link="runs/$rid/logs"

  printf "| %s | %s | %s | %s | %s | %s | %s | [logs](%s) | %s |\n" \
    "$rid" "${parent:--}" "${started:--}" "${ended:--}" "$models" "$p_link" "$ldr_link" "$logs_link" "$seed_link" >> "$INDEX"
done

# 3) Footer
{
  printf "\n> Tip: launch alternates:\n"
  printf ">   (cd runs/<RUN_ID> && TOPIC_OVERRIDE=\\\"alt_seed_1.txt\\\" ../../self_recursive_metaformers_v3_fixed.sh)\n"
} >> "$INDEX"

# Open the next-run folder for flow
open "$NEXT_DIR" 2>/dev/null || true

# Optional auto-chain (single child)
if [[ "$AUTO_CHAIN" == "1" ]]; then
  echo "[$(ts)] AUTO_CHAIN=1 → launching next run…" | tee -a "$master"
  ( cd "$NEXT_DIR" && exec "$NEXT_SH" ) &
fi

# Final console summary
echo "✅ Run complete."
echo "Run: $RUN_DIR"
echo "Logs: $LOG_DIR"
echo "Principles: $PRINC"
echo "Seed: $SEED"
echo "Leaderboard: $LEADER"
echo "Alts: $RUN_DIR/alt_seed_1.txt, $RUN_DIR/alt_seed_2.txt"
echo "Index: $INDEX"