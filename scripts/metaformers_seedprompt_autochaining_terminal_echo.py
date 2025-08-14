#!/bin/bash

# Metaformer Loop Script
# Creator: gpt-oss
# Questioner: llama2
# Mediator & Scriber: dolphin3

# ===== CONFIGURATION =====
TOPIC="$1"                   # topic from CLI arg
ITERATIONS="$2"              # number of loops
MEDIATOR_INTERVAL="$3"       # after how many turns mediator joins
LOGFILE="metaformer_$(date +%Y%m%d_%H%M%S).log"

if [ -z "$TOPIC" ] || [ -z "$ITERATIONS" ] || [ -z "$MEDIATOR_INTERVAL" ]; then
    echo "Usage: $0 \"<topic>\" <iterations> <mediator_interval>"
    exit 1
fi

echo "==== METAFORMER LOOP START ====" | tee -a "$LOGFILE"
echo "Topic: $TOPIC" | tee -a "$LOGFILE"
echo "Iterations: $ITERATIONS" | tee -a "$LOGFILE"
echo "Mediator every $MEDIATOR_INTERVAL rounds" | tee -a "$LOGFILE"
echo "===============================" | tee -a "$LOGFILE"
echo "" | tee -a "$LOGFILE"

# ===== INITIAL PROMPT =====
PROMPT="$TOPIC"

for ((i=1; i<=ITERATIONS; i++)); do
    echo "----- ITERATION $i: GPT-OSS (Creator) -----" | tee -a "$LOGFILE"
    CREATOR_OUTPUT=$(ollama run gpt-oss "$PROMPT")
    echo "$CREATOR_OUTPUT" | tee -a "$LOGFILE"

    echo "----- ITERATION $i: LLaMA2 (Questioner) -----" | tee -a "$LOGFILE"
    QUESTION_OUTPUT=$(ollama run llama2 "Given the creator's reply: $CREATOR_OUTPUT — what is the next related question or angle to explore?")
    echo "$QUESTION_OUTPUT" | tee -a "$LOGFILE"

    # Mediator injects every N iterations
    if (( i % MEDIATOR_INTERVAL == 0 )); then
        echo "----- ITERATION $i: Dolphin3 (Mediator & Scriber) -----" | tee -a "$LOGFILE"
        MEDIATOR_OUTPUT=$(ollama run dolphin3 "Summarize and connect the ideas so far, then propose the next logical step. Current thread: $CREATOR_OUTPUT $QUESTION_OUTPUT")
        echo "$MEDIATOR_OUTPUT" | tee -a "$LOGFILE"
        PROMPT="$MEDIATOR_OUTPUT"
    else
        PROMPT="$QUESTION_OUTPUT"
    fi
done

echo "" | tee -a "$LOGFILE"
echo "==== METAFORMER LOOP COMPLETE ====" | tee -a "$LOGFILE"