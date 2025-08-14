#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Metaformers — Choose Your Prompt (ANSI-clean, streaming, macOS-friendly)

What this does
--------------
- Asks you for the first prompt/topic.
- Runs a lightweight 3-agent discussion locally via Ollama:
  * Questioner: llama2-uncensored:latest
  * Creator:    gpt-oss:20b
  * Mediator:   dolphin3:latest (every 3rd turn)
- Streams model outputs to the console in a human-readable way:
  * Strips ANSI escape codes for on-screen display
  * Keeps raw bytes (with ANSI) in per-model log files
- Saves logs + manifest in ./runs/<TIMESTAMP>/

Dependencies
------------
- Ollama installed and running locally
- Models pulled: `ollama pull llama2-uncensored:latest`, `ollama pull gpt-oss:20b`, `ollama pull dolphin3:latest`
- Python 3.10+ recommended, but the code avoids 3.10-only syntax for compatibility.

Notes
-----
- No external Python packages required.
- BSD/macOS friendly (no GNU-only flags).
"""

from __future__ import annotations

import os
import sys
import json
import time
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Tuple, Optional

# ------------- Configuration -------------

QUESTIONER = os.environ.get("AI_QUESTIONER", "llama2-uncensored:latest")
CREATOR    = os.environ.get("AI_CREATOR",    "gpt-oss:20b")
MEDIATOR   = os.environ.get("AI_MEDIATOR",   "dolphin3:latest")

OLLAMA_BIN = os.environ.get("OLLAMA_BIN", "/usr/local/bin/ollama")
MEDIATOR_EVERY = int(os.environ.get("MEDIATOR_EVERY", "3"))

# ------------- ANSI handling -------------

# CSI (e.g., \x1b[?25l), OSC (e.g., \x1b]8;;…\x07), and single-char C1 codes
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07")
_ANSI_C1  = re.compile(r"\x1b[@-Z\\-_]")  # 0x40–0x5F range
_BACKSPACE_OVERSTRIKE = re.compile(r".\x08")  # remove overstruck chars

# Common braille spinner frames and similar glyphs; drop long runs so they don't leak into prompts/logs
_SPIN_CHARS = "⠁⠂⠄⠆⠇⠋⠙⠚⠛⠟⠿⠷⠧⠦⠴⠼⠸⠹⠏"
# Match runs of braille spinner glyphs (U+2800–U+28FF). We allow optional spaces between frames.
# Using 2+ to aggressively drop sequences even if the model prints only a couple frames.
_SPINNER_BLOCK_RE = re.compile(r'(?:[\u2800-\u28FF]\s*){2,}')

def strip_spinners(s: str) -> str:
    # Remove braille runs anywhere
    s = _SPINNER_BLOCK_RE.sub('', s)
    # And specifically nuke any braille run at the beginning of lines (paranoid pass)
    s = re.sub(r'^(?:[\u2800-\u28FF]\s*)+', '', s, flags=re.M)
    return s

def sanitize_chunk(s: str) -> str:
    """ANSI + spinner cleanup WITHOUT trimming edges; safe for streaming so spaces at chunk boundaries are preserved."""
    return strip_spinners(strip_ansi(s))

def sanitize_block(s: str) -> str:
    """ANSI + spinner cleanup WITH trimming; use for prompts and finalized blocks."""
    return strip_spinners(strip_ansi(s)).strip()

def sanitize_text(s: str) -> str:
    return sanitize_block(s)

def _split_complete_ansi_window(s: str) -> tuple[str, str]:
    """
    Split `s` into (head, tail) where `head` contains only complete ANSI sequences,
    and `tail` is a possible incomplete escape sequence starting at the last ESC.
    """
    last = s.rfind("\x1b")
    if last == -1:
        return s, ""
    tail = s[last:]
    # If tail is a full escape sequence, keep everything in head
    if _ANSI_CSI.fullmatch(tail) or _ANSI_OSC.fullmatch(tail) or _ANSI_C1.fullmatch(tail):
        return s, ""
    # Otherwise, cut the tail so we can prepend it to the next chunk
    return s[:last], tail

def strip_ansi(s: str) -> str:
    """Remove ANSI escape codes (CSI/OSC/C1), normalize carriage returns, and drop overstrikes for clean console/logging."""
    s = s.replace("\r", "")
    # iteratively remove overstrikes like "A\b"
    while True:
        new = _BACKSPACE_OVERSTRIKE.sub("", s)
        if new == s:
            break
        s = new
    s = _ANSI_OSC.sub("", s)
    s = _ANSI_CSI.sub("", s)
    s = _ANSI_C1.sub("", s)
    return s

# ------------- Topic & plan guards -------------

_BAD_TOPIC_PATTERNS = [
    r"^thank you\b",
    r"^thanks\b",
    r"^i have provided\b",
    r"^could you please confirm\b",
    r"^you are the creator\b",
    r"^given the questioner'?s topic\b",
    r"^here (?:are|is) the (?:final )?questions\b",
    r"^the user (?:says|asked)\b",
    r"^topic:\b",
]
_BAD_TOPIC_RE = re.compile("|".join(_BAD_TOPIC_PATTERNS), re.I)

def _collapse_spaces(s: str) -> str:
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([^\-\s])([.,!?])", r"\1\2", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def choose_next_topic(raw_text: str, fallback: str) -> str:
    """
    Pick a clean, usable topic/question from a model reply.
    - Strips ANSI/spinners, quotes, and instruction echoes
    - Prefers a single question ending with '?'
    - Falls back if unusable
    """
    t = sanitize_block(raw_text)
    t = t.strip().strip('"').strip("'")
    t = _collapse_spaces(t)

    if _BAD_TOPIC_RE.search(t):
        return fallback

    if len(t) > 400:
        t = t[:400].rsplit(" ", 1)[0] + "…"

    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    for ln in lines:
        if ln.endswith("?") and 8 <= len(ln) <= 240:
            return ln

    if lines and 8 <= len(lines[0]) <= 240:
        return lines[0]

    return fallback

# Require Creator to emit the three headings
_CREATOR_HEADINGS = (
    re.compile(r"^##\s*Conceptual Insight\b", re.I | re.M),
    re.compile(r"^##\s*Practical Mechanism\b", re.I | re.M),
    re.compile(r"^##\s*Why This Matters\b", re.I | re.M),
)

def looks_like_plan(s: str) -> bool:
    s = sanitize_block(s or "")
    return bool(s) and all(r.search(s) for r in _CREATOR_HEADINGS)

# ------------- IO / logging -------------

def ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def mkdirp(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def write_text(path: Path, data: str) -> None:
    mkdirp(path.parent)
    path.write_text(data, encoding="utf-8")

def append_text(path: Path, data: str) -> None:
    mkdirp(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(data)

def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()

# ------------- Ollama runner (streaming) -------------

def run_ollama(model: str, prompt: str, log_file: Path, *, log_raw: bool = False, raw_log_file: Optional[Path] = None) -> str:
    """
    Run `ollama run <model>` with the given prompt.
    Streams output; prints ANSI-stripped to console; writes ANSI-stripped text to `log_file`.
    If log_raw=True and raw_log_file is provided, also mirrors raw bytes to that file.
    Returns the full ANSI-stripped text.
    """
    env = os.environ.copy()
    env.update({
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CLICOLOR": "0",
        "PYTHONUNBUFFERED": "1",
        "COLORTERM": "0",
        "OLLAMA_SHELL": "0",
    })

    mkdirp(log_file.parent)
    if log_raw and raw_log_file:
        mkdirp(raw_log_file.parent)

    full_clean_parts = []

    proc = subprocess.Popen(
        [OLLAMA_BIN, "run", model],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        bufsize=0,
    )

    # Feed prompt then close stdin so generation begins
    assert proc.stdin is not None
    proc.stdin.write((prompt + "\n").encode("utf-8", errors="ignore"))
    proc.stdin.flush()
    proc.stdin.close()

    assert proc.stdout is not None
    # Console heading + log headers
    say(f"[{ts()}] [{model}] <<<")
    append_text(log_file, f"\n[{ts()}] PROMPT:\n{sanitize_block(prompt)}\n\n[{ts()}] OUTPUT:\n")
    if log_raw and raw_log_file:
        append_text(raw_log_file, f"\n[{ts()}] PROMPT:\n{prompt}\n\n[{ts()}] RAW_OUT:\n")

    # Stream stdout
    carry = ""
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        raw = chunk.decode("utf-8", errors="ignore")
        # Prepend any carried, possibly-incomplete escape sequence
        combined = carry + raw
        head, carry = _split_complete_ansi_window(combined)
        clean = sanitize_chunk(head)

        if clean:
            sys.stdout.write(clean)
            sys.stdout.flush()
            append_text(log_file, clean)
            full_clean_parts.append(clean)
        if log_raw and raw_log_file and raw:
            append_text(raw_log_file, raw)

    # Flush any remaining carried bytes at EOF
    if carry:
        clean_tail = sanitize_chunk(carry)
        if clean_tail:
            sys.stdout.write(clean_tail)
            sys.stdout.flush()
            append_text(log_file, clean_tail)
            full_clean_parts.append(clean_tail)
        if log_raw and raw_log_file:
            append_text(raw_log_file, carry)

    rc = proc.wait()

    # Ensure newline termination in console and log
    final_text = "".join(full_clean_parts)
    if final_text and not final_text.endswith("\n"):
        say("")
        append_text(log_file, "\n")
        if log_raw and raw_log_file:
            append_text(raw_log_file, "\n")

    if rc != 0:
        warn_line = f"[{ts()}] [warn] {model} exited with code {rc} (see logs)."
        say(warn_line)
        append_text(log_file, warn_line + "\n")
        if log_raw and raw_log_file:
            append_text(raw_log_file, warn_line + "\n")

    return final_text.strip()

# ------------- Prompt templates -------------

def make_questioner_prompt(seed: str, last_creator_take: Optional[str]) -> str:
    """Prompt for Questioner to output only the corrected question between <out> tags."""
    return (
        "TASK: You will receive ONE line of text between <q> and </q>.\n"
        "GOAL: Output the SAME line, correcting ONLY obvious typos/spelling.\n"
        "RULES: Do not rephrase, change word order, or add/remove words. No comments.\n"
        "FORMAT: Return ONLY the corrected line wrapped in <out> and </out> — nothing else.\n"
        "<q>\n"
        f"{seed}\n"
        "</q>\n"
    )

def make_creator_prompt(topic: str) -> str:
    """Prompt for Creator to propose actionable steps."""
    return (
        "You are the Creator.\n"
        "Given the Questioner's topic, propose a concrete, actionable mini‑plan in this EXACT format:\n\n"
        "## Conceptual Insight\n"
        "(2–4 sentences)\n\n"
        "## Practical Mechanism\n"
        "1. Step ...\n"
        "2. Step ...\n"
        "3. Step ...\n"
        "4. Step ...\n\n"
        "## Why This Matters\n"
        "- Bullet\n"
        "- Bullet\n"
        "- Bullet\n\n"
        f"Topic:\n{topic}\n"
    )

def make_mediator_prompt(creator_text: str) -> str:
    """Prompt for Mediator to challenge assumptions and maybe pivot."""
    return (
        "You are the Mediator. Read the Creator’s output below and challenge the core assumption with ONE incisive meta‑question "
        "(<=80 words) that either (a) reveals a flaw, or (b) suggests a sharper objective. End with a question mark.\n\n"
        f"Creator output:\n{creator_text}\n"
    )

# ------------- Main flow -------------

def main() -> None:
    say(f"[{ts()}] Meta Discussion — three local models, one topic.")
    try:
        seed = input("First prompt (what should they discuss?): ").strip()
    except KeyboardInterrupt:
        say("\n[abort] no topic provided.")
        return
    if not seed:
        say("[fatal] empty topic; nothing to do.")
        return

    try:
        turns_input = input("How many iterations (turns) do you want? ").strip()
        TURNS = int(turns_input)
    except (KeyboardInterrupt, ValueError):
        say("\n[abort] invalid or no iteration count provided.")
        return

    root = Path.cwd()
    runs_dir = root / "runs"
    run_id = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    run_dir = runs_dir / run_id
    logs_dir = run_dir / "logs"
    mkdirp(logs_dir)

    master_log = logs_dir / f"master_{run_id}.log"
    q_log = logs_dir / f"questioner_{run_id}.log"
    c_log = logs_dir / f"creator_{run_id}.log"
    m_log = logs_dir / f"mediator_{run_id}.log"

    manifest = {
        "run_id": run_id,
        "started_utc": ts(),
        "models": {"questioner": QUESTIONER, "creator": CREATOR, "mediator": MEDIATOR},
        "seed": seed,
        "turns": TURNS,
        "mediator_every": MEDIATOR_EVERY,
        "ollama_bin": OLLAMA_BIN,
    }
    write_text(run_dir / "manifest.json", json.dumps(manifest, indent=2))

    say(f"[{ts()}] Run folder: {run_dir}\n")

    current_topic = seed
    last_creator = None

    for turn in range(1, TURNS + 1):
        say(f"\n[{ts()}] === Turn {turn}/{TURNS} ===")

        # Questioner outputs the user's question with typo corrections only
        q_prompt = make_questioner_prompt(current_topic, last_creator)
        q_text = run_ollama(QUESTIONER, q_prompt, q_log)

        # Extract only the corrected line between <out>...</out>; else fall back
        extracted = None
        if q_text:
            m = re.search(r"<out>(.*?)</out>", q_text, re.S | re.I)
            if m:
                extracted = sanitize_block(m.group(1))
            else:
                # Heuristic: if the model wrongly echoed the instructions, ignore them
                bad_lead = "you are to output the user’s question"
                if sanitize_block(q_text).lower().startswith(bad_lead):
                    extracted = None

        current_topic = extracted if extracted else current_topic
        say(f"\n[{ts()}] Topic: {sanitize_block(current_topic)}")

        # Creator proposes the plan
        c_prompt = make_creator_prompt(current_topic)
        creator_text = run_ollama(CREATOR, c_prompt, c_log)
        if not looks_like_plan(creator_text):
            strict = c_prompt + "\n\nIMPORTANT: Output MUST include the three headings exactly as specified. No preface. No 'Thinking...'."
            creator_text = run_ollama(CREATOR, strict, c_log)
        last_creator = creator_text if creator_text else last_creator

        # Mediator every N turns (uses the creator text to refine the next topic)
        if turn % MEDIATOR_EVERY == 0 and creator_text:
            m_prompt = make_mediator_prompt(creator_text)
            mediator_q = run_ollama(MEDIATOR, m_prompt, m_log)
            current_topic = choose_next_topic(mediator_q, current_topic)
            say(f"\n[{ts()}] (mediator steered) Next topic: {sanitize_block(current_topic)}")

    # Close manifest
    manifest["ended_utc"] = ts()
    write_text(run_dir / "manifest.json", json.dumps(manifest, indent=2))
    say(f"\n[{ts()}] Done. Logs at: {logs_dir}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say(f"\n[{ts()}] Interrupted by user.")