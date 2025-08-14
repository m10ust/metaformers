#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metaformers — Choose Your Prompt (v2)
- Prompts you for the initial topic and number of iterations
- Orchestrates a 3-model discussion via Ollama:
  * Questioner: llama2-uncensored:latest
  * Creator:    gpt-oss:20b
  * Mediator:   dolphin3:latest (asks a meta-question every N turns)
- Robust logging and ANSI/spinner cleanup so logs stay readable
- Makes the Questioner echo your topic "as-is" (typo-fix only) via instruction + fallback
"""

import os
import re
import sys
import time
import json
import shlex
import signal
import pathlib
import datetime
import subprocess
from typing import Optional, Tuple

# --------------------------- Config ---------------------------

QUESTIONER = os.environ.get("MF_QUESTIONER", "llama2-uncensored:latest")
CREATOR    = os.environ.get("MF_CREATOR",    "gpt-oss:20b")
MEDIATOR   = os.environ.get("MF_MEDIATOR",   "dolphin3:latest")
OLLAMA_BIN = os.environ.get("OLLAMA_BIN", "/usr/local/bin/ollama")

# Every how many turns the mediator jumps in (set 0 to disable)
MEDIATOR_EVERY_DEFAULT = int(os.environ.get("MF_MEDIATOR_EVERY", "4"))

# Where to write runs
ROOT = pathlib.Path.cwd()
RUNS_DIR = ROOT / "runs"

# ---------------------- Utility: timestamps -------------------

def ts_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def ts_compact() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")

# ---------------------- Utility: ANSI cleanup -----------------

ANSI_RE = re.compile(
    r"(?:\x1B\[[0-9;?]*[ -/]*[@-~])"        # CSI sequences
    r"|(?:\x1B[@-Z\\-_])"                   # 2-byte
    r"|(?:\x1B\][^\x07]*\x07)"              # OSC … BEL
)

# Common braille/spinner chars set (Unicode 2800–28FF + a few)
SPINNER_RE = re.compile(r"[\u2800-\u28FF◐◓◑◒⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+")

def clean_text(s: str) -> str:
    if not s:
        return s
    s = ANSI_RE.sub("", s)
    s = SPINNER_RE.sub("", s)
    # Normalize excessive whitespace while preserving newlines
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\r", "", s)
    # Collapse long runs of blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

# Remove labels like "Corrected Topic:" or "Topic:" the model might prepend,
# and strip wrapping quotes/backticks.
LABEL_RE = re.compile(
    r'^\s*(?:>{1,3}\s*)?'
    r'(?:topic|corrected\s*topic|final\s*topic|revised\s*topic|clean(?:ed)?\s*topic|prompt)'
    r'\s*[:\-]\s*',
    re.I,
)

QUOTE_CHARS = '"\'`“”‘’'

def strip_leading_labels(s: str) -> str:
    if not s:
        return s
    out = s
    # Run twice in case the model stacked labels (rare but seen)
    for _ in range(2):
        out = re.sub(LABEL_RE, '', out)
    return out.strip()

def strip_wrapping_quotes(s: str) -> str:
    if not s:
        return s
    out = s.strip()
    # Strip symmetrical wrapping quotes/backticks
    while len(out) >= 2 and out[0] in QUOTE_CHARS and out[-1] in QUOTE_CHARS:
        out = out[1:-1].strip()
    # Also trim any leading quote leftovers
    while out and out[0] in QUOTE_CHARS:
        out = out[1:].strip()
    while out and out[-1] in QUOTE_CHARS:
        out = out[:-1].strip()
    return out

def normalize_topic(s: str) -> str:
    """Clean ANSI/spinners, drop labels like 'Corrected Topic:', and strip wrapping quotes."""
    if not s:
        return s
    s = clean_text(s)
    s = strip_leading_labels(s)
    s = strip_wrapping_quotes(s)
    return s.strip()

def extract_marked(s: str) -> str:
    """
    Extract text between <<<BEGIN>>> and <<<END>>> markers if present.
    Falls back to cleaned whole text. Always normalized to remove labels/quotes.
    """
    if not s:
        return ""
    m = re.search(r"<<<BEGIN>>>\s*(.*?)\s*<<<END>>>", s, flags=re.S)
    if m:
        return normalize_topic(m.group(1))
    return normalize_topic(s)

# ------------------------- Logging ----------------------------

class TeeLogger:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, line: str, also_stdout: bool = False):
        stamp = f"[{ts_iso()}] "
        text = stamp + line.rstrip("\n")
        self._fh.write(text + "\n")
        self._fh.flush()
        if also_stdout:
            print(text)

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass

# ---------------------- Ollama invocation ---------------------

def check_ollama_or_die():
    if not pathlib.Path(OLLAMA_BIN).exists():
        print(f"[{ts_iso()}] [fatal] Ollama binary not found at: {OLLAMA_BIN}", file=sys.stderr)
        sys.exit(2)

def run_ollama(model: str, prompt: str, log: TeeLogger, show_prefix: str) -> str:
    """
    Run `ollama run {model}` with `prompt` on stdin.
    We capture stdout/stderr, stream *cleaned* output to the console for readability,
    and return the raw (but cleaned) text for downstream use.
    """
    cmd = [OLLAMA_BIN, "run", model]
    env = os.environ.copy()
    # Reduces TTY-style cosmetics; still sanitize just in case
    env.setdefault("TERM", "dumb")
    env.setdefault("NO_COLOR", "1")

    log.write(f"{show_prefix} <<<", also_stdout=True)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )

    # Feed prompt
    try:
        assert proc.stdin is not None
        proc.stdin.write(prompt)
        proc.stdin.flush()
        proc.stdin.close()
    except BrokenPipeError:
        pass

    # Stream + collect
    raw_out_chunks = []
    # Read stdout line-buffered; if a model prints without newlines, fall back to chunked reads
    while True:
        if proc.stdout is None:
            break
        line = proc.stdout.readline()
        if not line:
            break
        raw_out_chunks.append(line)
        cleaned = clean_text(line)
        if cleaned:
            # echo cleaned line to console for human readability
            print(cleaned)

    # Drain stderr
    stderr = proc.stderr.read() if proc.stderr else ""
    ret = proc.wait()

    raw_text = "".join(raw_out_chunks)
    out_clean = clean_text(raw_text).strip()
    err_clean = clean_text(stderr).strip()

    if err_clean:
        log.write(f"[STDERR] {err_clean}", also_stdout=True)
    if ret != 0:
        log.write(f"[warn] ollama exited with {ret}", also_stdout=True)

    return out_clean

# ---------------------- Prompt builders -----------------------

def build_questioner_prompt(user_topic: str) -> str:
    """
    Force the Questioner to echo the user's topic verbatim with ONLY typo/punctuation fixes.
    The model must return ONLY the corrected topic wrapped in markers.
    """
    return (
        "You are the Questioner.\n"
        "TASK: Repeat the user's topic EXACTLY as given, fixing only obvious spelling and punctuation typos.\n"
        "- Preserve wording, order, meaning, and all clauses.\n"
        "- Do NOT simplify, summarize, add context, or change technical terms.\n"
        "- Output ONLY the corrected topic between these exact markers (no labels or quotes):\n"
        "<<<BEGIN>>>\n"
        f"{user_topic}\n"
        "<<<END>>>\n"
        "If you output anything outside the markers, the run will fail.\n\n"
        "USER TOPIC:\n"
        f"{user_topic}\n"
    )

def build_creator_prompt(topic: str, mediator_q: Optional[str] = None) -> str:
    preface = "You are the Creator.\nGiven the Questioner's topic, propose a concrete, actionable mini‑plan in this EXACT format:\n\n"
    if mediator_q:
        preface += (
            "The Mediator previously asked this meta‑question — you MUST address it explicitly in your plan:\n"
            f"» {mediator_q}\n\n"
            "Include a single line at the top of your response:\n"
            "Mediator Answer: <one concise sentence answering the meta‑question>\n\n"
        )
    return (
        preface +
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

def build_mediator_prompt(creator_output: str) -> str:
    return (
        "You are the Mediator.\n"
        "Read the Creator’s response and challenge its core assumption with one concise meta‑question (≤80 words).\n"
        "Output one question ending with a question mark, nothing else.\n\n"
        "Creator response:\n"
        f"{creator_output}\n"
    )

# ---------------------- Topic guards --------------------------

def enforce_topic(original: str, candidate: str) -> str:
    """
    Accept the candidate only if it looks like a light typo-fix of the original.
    Heuristics:
      - length ratio must be >= 0.7
      - shared token overlap (len>=4) must be >= 0.6
    Otherwise, return the original.
    """
    orig = original.strip()
    cand = candidate.strip()
    if not cand:
        return orig
    # Length heuristic
    if len(cand) < 0.7 * len(orig):
        return orig
    # Token overlap heuristic (long tokens only)
    tok = re.compile(r"[A-Za-z0-9_]+")
    orig_tokens = [t.lower() for t in tok.findall(orig)]
    cand_tokens = [t.lower() for t in tok.findall(cand)]
    orig_long = {t for t in orig_tokens if len(t) >= 4}
    cand_long = {t for t in cand_tokens if len(t) >= 4}
    if not orig_long:
        return cand  # nothing to compare
    overlap = len(orig_long & cand_long) / max(1, len(orig_long))
    if overlap < 0.6:
        return orig
    return cand

# --------------------------- Main -----------------------------

def main():
    check_ollama_or_die()

    print(f"[{ts_iso()}] Meta Discussion — three local models, one topic.")
    user_topic = input("First prompt (what should they discuss?): ").strip()
    if not user_topic:
        print("No topic provided. Exiting.")
        sys.exit(1)

    try:
        iters_str = input("How many iterations (turns) do you want? ").strip()
        iterations = int(iters_str) if iters_str else 12
    except Exception:
        iterations = 12

    try:
        med_str = input(f"Mediator every how many turns? [default {MEDIATOR_EVERY_DEFAULT}]: ").strip()
        mediator_every = int(med_str) if med_str else MEDIATOR_EVERY_DEFAULT
    except Exception:
        mediator_every = MEDIATOR_EVERY_DEFAULT

    run_id = ts_compact()
    run_dir = RUNS_DIR / run_id
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    master = TeeLogger(logs_dir / f"master_{run_id}.log")
    qlog   = TeeLogger(logs_dir / f"questioner_{run_id}.log")
    clog   = TeeLogger(logs_dir / f"creator_{run_id}.log")
    mlog   = TeeLogger(logs_dir / f"mediator_{run_id}.log")
    tlog   = TeeLogger(run_dir / "transcript.txt")

    master.write(f"Run folder: {run_dir}", also_stdout=True)
    print()

    current_topic = user_topic
    last_mediator_q: Optional[str] = None
    frozen_topic: Optional[str] = None

    for turn in range(1, iterations + 1):
        master.write(f"=== Turn {turn}/{iterations} ===", also_stdout=True)

        # 1) QUESTIONER — run once to typo‑fix the user's topic, then freeze
        if turn == 1 or not frozen_topic:
            q_prompt = build_questioner_prompt(user_topic)
            qlog.write("PROMPT:\n" + q_prompt, also_stdout=False)
            print(f"[{ts_iso()}] [{QUESTIONER}] <<<", flush=True)
            q_out_raw = run_ollama(QUESTIONER, q_prompt, qlog, f"[{QUESTIONER}]")
            q_out = extract_marked(q_out_raw)
            if not q_out:
                q_out = user_topic
            q_out = enforce_topic(user_topic, q_out)
            q_out = normalize_topic(q_out)
            frozen_topic = q_out
            tlog.write(f"[{ts_iso()}] QUESTIONER:\n{q_out}\n", also_stdout=False)
        else:
            q_out = frozen_topic
            qlog.write("PROMPT: (skipped; reusing frozen topic)\n", also_stdout=False)
            tlog.write(f"[{ts_iso()}] QUESTIONER (reused):\n{q_out}\n", also_stdout=False)

        # Log topic line for humans
        master.write(f"Topic: {q_out}", also_stdout=True)

        # 2) CREATOR — mini-plan
        c_prompt = build_creator_prompt(q_out, mediator_q=last_mediator_q)
        clog.write("PROMPT:\n" + c_prompt, also_stdout=False)
        print(f"[{ts_iso()}] [{CREATOR}] <<<", flush=True)
        c_out = run_ollama(CREATOR, c_prompt, clog, f"[{CREATOR}]")
        if not c_out:
            c_out = "(no output)"
        tlog.write(f"[{ts_iso()}] CREATOR:\n{c_out}\n", also_stdout=False)
        last_mediator_q = None

        # 3) MEDIATOR every N turns — produce next topic from mediator question,
        #    otherwise keep using the Creator output's first line as a seed
        if mediator_every > 0 and (turn % mediator_every == 0):
            m_prompt = build_mediator_prompt(c_out)
            mlog.write("PROMPT:\n" + m_prompt, also_stdout=False)
            print(f"[{ts_iso()}] [{MEDIATOR}] <<<", flush=True)
            m_out = run_ollama(MEDIATOR, m_prompt, mlog, f"[{MEDIATOR}]")
            if not m_out:
                m_out = "What underlying assumption, if false, would invalidate the plan?"
            tlog.write(f"[{ts_iso()}] MEDIATOR:\n{m_out}\n", also_stdout=False)
            # Next turn: require the Creator to explicitly answer the mediator question.
            last_mediator_q = normalize_topic(m_out)
            master.write(f"[note] Mediator constraint queued for next turn (topic unchanged): {last_mediator_q}", also_stdout=True)
            # Keep the discussion on the established topic; the mediator shapes the next plan rather than replacing the topic.
            # current_topic remains unchanged here.
        else:
            # Keep discussion on the frozen user topic to prevent drift.
            current_topic = frozen_topic or q_out

        print()

    # Close logs
    for lg in (master, qlog, clog, mlog, tlog):
        lg.close()

    print(f"[{ts_iso()}] Done. Run folder: {run_dir}")

def handle_sigint(signum, frame):
    print(f"\n[{ts_iso()}] Aborted by user.", file=sys.stderr)
    sys.exit(130)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sigint)
    try:
        main()
    except KeyboardInterrupt:
        handle_sigint(signal.SIGINT, None)
