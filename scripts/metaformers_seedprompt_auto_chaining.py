#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metaformers — seed prompt auto-chaining (v2)
- Roles:
    - The Questioner  -> llama2-uncensored:latest  (restates topic, fixes typos only)
    - The Creator     -> gpt-oss:20b              (answers + emits NextPrompt)
    - The Mediator    -> dolphin3:latest          (meta-question every N turns)
    - The Scriber     -> dolphin3:latest          (brief TL;DR after each turn)
- Flow (per turn):
    1) Questioner receives the current topic and outputs *only* the corrected topic.
    2) (Optional) Mediator injects a single meta-question to stress-test assumptions.
    3) Creator produces an actionable mini-plan AND must end with `NextPrompt: ...` (single line).
    4) Scriber writes a tight summary for the log.
    5) The `NextPrompt` becomes the next turn’s topic (auto-chaining).
- Notes:
    * Uses the local Ollama HTTP API (http://127.0.0.1:11434).
    * Logs are written to runs/YYYYMMDD-HHMMSS/.
    * Designed to avoid spinner/ANSI/braille clutter and "Thinking..." blocks.
"""

import os
import sys
import json
import time
import re
import textwrap
from datetime import datetime
from pathlib import Path

try:
    import requests
except Exception as e:
    print("[fatal] This script requires `requests` (pip install requests).")
    sys.exit(1)

# -----------------------------
# Config / defaults (env override)
# -----------------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL_QUESTIONER = os.getenv("QUESTIONER_MODEL", "llama2-uncensored:latest")
MODEL_CREATOR     = os.getenv("CREATOR_MODEL",   "gpt-oss:20b")
MODEL_MEDIATOR    = os.getenv("MEDIATOR_MODEL",  "dolphin3:latest")
MODEL_SCRIBER     = os.getenv("SCRIBER_MODEL",   "dolphin3:latest")

GEN_OPTIONS = {
    "temperature": float(os.getenv("GEN_TEMPERATURE", "0.7")),
    "top_p": float(os.getenv("GEN_TOP_P", "0.9")),
}

# -----------------------------
# Utilities
# -----------------------------
def ts():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
BRAILLE_RE = re.compile(r"[\u2800-\u28FF]+", re.UNICODE)
THINK_BLOCK_RE = re.compile(r"(?is)\bthinking\.\.\..*?(?:done thinking\.)")

def sanitize(text: str) -> str:
    """Strip ANSI, braille spinners, and 'Thinking.../done thinking' blocks."""
    if not text:
        return text
    text = ANSI_RE.sub("", text)
    text = BRAILLE_RE.sub("", text)
    text = THINK_BLOCK_RE.sub("", text)
    return text.strip()

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def call_ollama(model: str, prompt: str, system: str = None, options: dict = None, timeout: int = 120) -> str:
    """Call Ollama /api/generate non-streaming; returns the 'response' field text."""
    url = f"{OLLAMA_HOST}/api/generate"
    payload = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    if options:
        payload["options"] = options
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("response", "")
    except requests.HTTPError as e:
        return f"[model error] {e}"
    except Exception as e:
        return f"[model error] {e}"

def write(logfile: Path, text: str):
    with logfile.open("a", encoding="utf-8") as f:
        f.write(text)

def header(line: str) -> str:
    return f"\n[{ts()}] {line}\n"

# -----------------------------
# System prompts
# -----------------------------
QUESTIONER_SYS = """You are The Questioner.
Your task: output the user's topic EXACTLY as provided, fixing only obvious spelling, spacing, and punctuation.
Do NOT rephrase, do NOT change word order, do NOT add context, do NOT summarize.
If the input is not phrased as a question, add a trailing '?' without altering meaning.
Output ONLY the corrected topic, nothing else."""

CREATOR_SYS = """You are The Creator.
Given the Topic, produce a concrete, actionable mini-plan in this EXACT format (no extra prose):
## Conceptual Insight
(2–4 sentences)

## Practical Mechanism
1. Step...
2. Step...
3. Step...
4. Step...

## Why This Matters
- Bullet
- Bullet
- Bullet

At the very end, on a single line, emit:
NextPrompt: <a succinct follow-up topic/question that advances the work one step>
Rules:
- Do not include code fences around 'NextPrompt:'.
- Keep 'NextPrompt:' on one line with no trailing commentary.
- Do not include 'Thinking...' or similar internal notes."""

MEDIATOR_SYS = """You are The Mediator.
Given the Topic and the Creator's last answer, output ONE incisive meta-question that stress-tests assumptions, constraints, or safety.
Format EXACTLY one line:
MediatorQ: <your single question>
No explanations, no bullets, no extra lines."""

SCRIBER_SYS = """You are The Scriber.
Summarize the turn for the project log in 3 tight bullets.
No preamble. Avoid repetition. 300 characters max total."""

# -----------------------------
# Chaining helpers
# -----------------------------
NEXTPROMPT_RE = re.compile(r"(?im)^\s*NextPrompt\s*:\s*(.+)\s*$")

def extract_next_prompt(text: str) -> str:
    """Find the last NextPrompt: line in the Creator output."""
    if not text:
        return ""
    m = None
    for m in NEXTPROMPT_RE.finditer(text):
        pass
    if m:
        return m.group(1).strip()
    return ""

def fallback_next_prompt(prev_creator: str, topic_now: str) -> str:
    # Minimal fallback if the Creator forgot NextPrompt
    return f"Refine and deepen: {topic_now}"

# -----------------------------
# Main loop
# -----------------------------
def main():
    print("Metaformers — seed prompt auto-chaining")
    topic = input("First prompt (what should they discuss?): ").strip()
    if not topic:
        print("[fatal] Need a topic. Exiting.")
        sys.exit(1)
    try:
        turns = int(input("How many iterations (turns) do you want? ").strip())
    except:
        turns = 8
        print(f"[warn] Invalid number; defaulting to {turns} turns.")
    try:
        mediator_every = int(input("Mediator pops up every N turns (0 = never): ").strip() or "0")
    except:
        mediator_every = 0

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path("runs") / run_id
    logs_dir = run_dir / "logs"
    ensure_dir(logs_dir)
    master_log = run_dir / "master.log"

    # Write run header
    write(master_log, header(f"Run folder: {run_dir}"))
    write(master_log, f"Models: questioner={MODEL_QUESTIONER} creator={MODEL_CREATOR} mediator={MODEL_MEDIATOR} scriber={MODEL_SCRIBER}\n")
    write(master_log, f"Params: turns={turns} mediator_every={mediator_every}\n")
    write(master_log, "-" * 80 + "\n")

    last_creator_out = ""

    for t in range(1, turns + 1):
        turn_log = logs_dir / f"turn_{t:03d}.log"
        write(master_log, header(f"=== Turn {t}/{turns} ==="))
        write(turn_log, header(f"=== Turn {t}/{turns} ==="))

        # 1) Questioner
        q_prompt = topic
        q_out = call_ollama(
            MODEL_QUESTIONER,
            prompt=q_prompt,
            system=QUESTIONER_SYS,
            options=GEN_OPTIONS
        )
        q_out = sanitize(q_out)
        write(master_log, f"[{MODEL_QUESTIONER}] <<<\n{q_out}\n\n")
        write(turn_log,      f"[{MODEL_QUESTIONER}] <<<\n{q_out}\n\n")

        corrected_topic = q_out if q_out else topic

        # 2) Mediator (optional)
        mediator_line = ""
        if mediator_every and (t % mediator_every == 0):
            med_prompt = f"Topic:\n{corrected_topic}\n\nLastCreator:\n{last_creator_out[-2000:]}"
            med_out = call_ollama(
                MODEL_MEDIATOR,
                prompt=med_prompt,
                system=MEDIATOR_SYS,
                options={"temperature": 0.2, "top_p": 0.9}
            )
            med_out = sanitize(med_out)
            write(master_log, f"[{MODEL_MEDIATOR}] <<<\n{med_out}\n\n")
            write(turn_log,      f"[{MODEL_MEDIATOR}] <<<\n{med_out}\n\n")
            # Expect exactly: "MediatorQ: ..."
            if med_out.lower().startswith("mediatorq:"):
                mediator_line = med_out.strip()
            elif "MediatorQ:" in med_out:
                mediator_line = med_out.split("MediatorQ:", 1)[1].strip()
                mediator_line = "MediatorQ: " + mediator_line

        # 3) Creator
        creator_prompt = f"Topic: {corrected_topic}"
        if mediator_line:
            creator_prompt += f"\n\n{mediator_line}\n(Answer the Mediator's question directly within the plan above.)"

        c_out = call_ollama(
            MODEL_CREATOR,
            prompt=creator_prompt,
            system=CREATOR_SYS,
            options=GEN_OPTIONS
        )
        c_out = sanitize(c_out)
        write(master_log, f"[{MODEL_CREATOR}] <<<\n{c_out}\n\n")
        write(turn_log,      f"[{MODEL_CREATOR}] <<<\n{c_out}\n\n")
        last_creator_out = c_out

        # 4) Scriber
        s_prompt = f"Topic: {corrected_topic}\n\nCreator:\n{c_out[-2400:]}"
        s_out = call_ollama(
            MODEL_SCRIBER,
            prompt=s_prompt,
            system=SCRIBER_SYS,
            options={"temperature": 0.2, "top_p": 0.9}
        )
        s_out = sanitize(s_out)
        write(master_log, f"[{MODEL_SCRIBER}] <<<\n{s_out}\n\n")
        write(turn_log,      f"[{MODEL_SCRIBER}] <<<\n{s_out}\n\n")

        # 5) Next topic
        next_topic = extract_next_prompt(c_out)
        if not next_topic:
            next_topic = fallback_next_prompt(c_out, corrected_topic)

        write(master_log, f"NextTopic -> {next_topic}\n" + ("-" * 80) + "\n")
        write(turn_log,   f"NextTopic -> {next_topic}\n" + ("-" * 80) + "\n")

        # Chain
        topic = next_topic

        # Be nice to CPU / logs
        time.sleep(0.2)

    print(f"[done] Run folder: {run_dir}")
    print("Tip: tail -f {}/master.log".format(run_dir))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[abort] Interrupted by user.")
        sys.exit(130)
