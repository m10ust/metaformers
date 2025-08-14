#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metaformers — Four-Role Loop (Questioner, Creator, Mediator, Reviewer)
- Prompts you for: initial topic, iterations, mediator cadence
- Roles (default models via OLLAMA):
    * Questioner: llama2-uncensored:latest  (echoes your topic; typo-fix only)
    * Creator:    gpt-oss:20b               (full plan/content)
    * Mediator:   dolphin3:latest           (meta-question every N turns)
    * Reviewer:   dolphin3:latest           (concise summary every turn)
- Logs:
    runs/<RUN_ID>/logs/
      master_<RUN_ID>.log
      questioner_<RUN_ID>.log
      creator_<RUN_ID>.log
      mediator_<RUN_ID>.log
      reviewer_<RUN_ID>.log
    runs/<RUN_ID>/transcript.txt
- macOS-safe (no GNU awk deps), strips ANSI + spinner glyphs from logs/console.
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
from typing import Optional

# --------------------------- Config ---------------------------

QUESTIONER = os.environ.get("MF_QUESTIONER", "llama2-uncensored:latest")
CREATOR    = os.environ.get("MF_CREATOR",    "gpt-oss:20b")
MEDIATOR   = os.environ.get("MF_MEDIATOR",   "dolphin3:latest")
REVIEWER   = os.environ.get("MF_REVIEWER",   "dolphin3:latest")
OLLAMA_BIN = os.environ.get("OLLAMA_BIN", "/usr/local/bin/ollama")

# Mediator cadence (every how many turns); 0 disables mediator
MEDIATOR_EVERY_DEFAULT = int(os.environ.get("MF_MEDIATOR_EVERY", "4"))

ROOT = pathlib.Path.cwd()
RUNS_DIR = ROOT / "runs"

# ---------------------- Utility: timestamps -------------------

def ts_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def ts_compact() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")

# ---------------------- Utility: ANSI cleanup -----------------

ANSI_RE = re.compile(
    r"(?:\x1B\[[0-9;?]*[ -/]*[@-~])"    # CSI sequences
    r"|(?:\x1B[@-Z\\-_])"               # 2-byte ESC
    r"|(?:\x1B\][^\x07]*\x07)"          # OSC … BEL
)

SPINNER_RE = re.compile(r"[\u2800-\u28FF◐◓◑◒⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+")

def clean_text(s: str) -> str:
    if not s:
        return s
    s = ANSI_RE.sub("", s)
    s = SPINNER_RE.sub("", s)
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

LABEL_RE = re.compile(
    r'^\s*(?:>{1,3}\s*)?'
    r'(?:topic|corrected\s*topic|final\s*topic|revised\s*topic|clean(?:ed)?\s*topic|prompt)'
    r'\s*[:\-]\s*',
    re.I,
)
QUOTE_CHARS = '"\'`“”‘’'

def strip_leading_labels(s: str) -> str:
    out = s or ""
    for _ in range(2):
        out = re.sub(LABEL_RE, "", out)
    return out.strip()

def strip_wrapping_quotes(s: str) -> str:
    out = (s or "").strip()
    while len(out) >= 2 and out[0] in QUOTE_CHARS and out[-1] in QUOTE_CHARS:
        out = out[1:-1].strip()
    while out and out[0] in QUOTE_CHARS:
        out = out[1:].strip()
    while out and out[-1] in QUOTE_CHARS:
        out = out[:-1].strip()
    return out

def normalize_topic(s: str) -> str:
    s = clean_text(s or "")
    s = strip_leading_labels(s)
    s = strip_wrapping_quotes(s)
    return s.strip()

def extract_marked(s: str) -> str:
    """
    Extract text between <<<BEGIN>>> and <<<END>>> if present; else return normalized s.
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
    Run `ollama run {model}`; stream cleaned stdout to console; return cleaned full text.
    """
    cmd = [OLLAMA_BIN, "run", model]
    env = os.environ.copy()
    env.setdefault("TERM", "dumb")
    env.setdefault("NO_COLOR", "1")

    log.write("PROMPT:\n" + prompt)
    print(f"[{ts_iso()}] [{model}] <<<", flush=True)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env
    )

    try:
        assert proc.stdin is not None
        proc.stdin.write(prompt)
        proc.stdin.flush()
        proc.stdin.close()
    except BrokenPipeError:
        pass

    raw_out = []
    # Line-by-line read; if a model streams without newlines, readline will still return chunks
    while True:
        if proc.stdout is None:
            break
        line = proc.stdout.readline()
        if not line:
            break
        raw_out.append(line)
        cleaned = clean_text(line)
        if cleaned:
            print(cleaned)

    stderr = proc.stderr.read() if proc.stderr else ""
    ret = proc.wait()

    raw_text = "".join(raw_out)
    out_clean = clean_text(raw_text).strip()
    err_clean = clean_text(stderr).strip()

    if err_clean:
        log.write(f"[STDERR] {err_clean}", also_stdout=True)
    if ret != 0:
        log.write(f"[warn] ollama exited with {ret}", also_stdout=True)

    log.write("OUTPUT:\n" + out_clean)
    return out_clean

# ---------------------- Prompt builders -----------------------

def build_questioner_prompt(user_topic: str) -> str:
    """
    Force Questioner to return ONLY your topic with typo fixes — no rephrasing, no labels.
    """
    return (
        "You are the Questioner.\n"
        "TASK: Output the user's topic EXACTLY as provided, correcting only obvious typos.\n"
        "- Do NOT rephrase or change word order.\n"
        "- Do NOT add/remove meaning.\n"
        "- Output ONLY the corrected topic between the markers below.\n"
        "- Do NOT include labels like 'Topic:' inside the markers.\n\n"
        "<<<BEGIN>>>\n"
        "<corrected topic here>\n"
        "<<<END>>>\n\n"
        "USER TOPIC:\n"
        f"{user_topic}\n"
    )

def build_creator_prompt(topic: str, mediator_q: Optional[str]) -> str:
    pre = "You are the Creator.\nGiven the Questioner’s topic, produce a concrete, actionable mini‑plan in this EXACT format:\n\n"
    if mediator_q:
        pre += (
            "The Mediator previously asked this meta‑question — you MUST address it explicitly:\n"
            f"» {mediator_q}\n\n"
            "Include a single line at the top of your response:\n"
            "Mediator Answer: <one concise sentence answering the meta‑question>\n\n"
        )
    return (
        pre +
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
        "Read the Creator’s response and challenge a core assumption with ONE concise meta‑question (≤80 words).\n"
        "Output ONE question that ends with a question mark. Nothing else.\n\n"
        "Creator response:\n"
        f"{creator_output}\n"
    )

def build_reviewer_prompt(topic: str, creator_output: str, mediator_q: Optional[str]) -> str:
    return (
        "You are the Reviewer.\n"
        "Summarize the key ideas for fast human skimming. Output a compact digest (80–140 words).\n"
        "Must include: (a) the topic in 1 short clause; (b) the core proposal; (c) any constraints or next steps; "
        "and if present, (d) the Mediator’s concern + the Creator’s answer.\n"
        "No markdown headings. No code fences. No lists. One tight paragraph.\n\n"
        f"Topic: {topic}\n\n"
        f"Creator response:\n{creator_output}\n\n"
        f"Mediator question (if any): {mediator_q or 'none'}\n"
    )

# ---------------------- Topic guards --------------------------

def enforce_topic(original: str, candidate: str) -> str:
    """
    Accept candidate if it looks like a light typo-fix:
      - length ratio >= 0.7
      - token overlap (tokens len>=4) >= 0.6
    Otherwise, keep original.
    """
    orig = original.strip()
    cand = (candidate or "").strip()
    if not cand:
        return orig
    if len(cand) < 0.7 * len(orig):
        return orig
    tok = re.compile(r"[A-Za-z0-9_]+")
    o = [t.lower() for t in tok.findall(orig)]
    c = [t.lower() for t in tok.findall(cand)]
    o4 = {t for t in o if len(t) >= 4}
    c4 = {t for t in c if len(t) >= 4}
    if not o4:
        return cand
    overlap = len(o4 & c4) / max(1, len(o4))
    if overlap < 0.6:
        return orig
    return cand

# --------------------------- Main -----------------------------

def main():
    check_ollama_or_die()

    print(f"[{ts_iso()}] Metaformers — four-role loop.")
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
    rlog   = TeeLogger(logs_dir / f"reviewer_{run_id}.log")
    tlog   = TeeLogger(run_dir / "transcript.txt")

    master.write(f"Run folder: {run_dir}", also_stdout=True)
    print()

    # We keep the established topic stable across turns (prevents drift)
    established_topic = user_topic
    last_mediator_q: Optional[str] = None

    for turn in range(1, iterations + 1):
        master.write(f"=== Turn {turn}/{iterations} ===", also_stdout=True)

        # 1) QUESTIONER — echo the ORIGINAL topic with typo-fix only
        q_prompt = build_questioner_prompt(established_topic)
        q_out_raw = run_ollama(QUESTIONER, q_prompt, qlog, f"[{QUESTIONER}]")
        q_out = extract_marked(q_out_raw) or established_topic
        q_out = enforce_topic(established_topic, q_out)
        q_out = normalize_topic(q_out)
        # Lock topic to first valid cleaned version from T1; on T>1 we keep the locked one
        if turn == 1:
            established_topic = q_out
        tlog.write(f"[{ts_iso()}] QUESTIONER:\n{q_out}\n")

        master.write(f"Topic: {established_topic}", also_stdout=True)

        # 2) CREATOR — produce full content (must answer last mediator q if present)
        c_prompt = build_creator_prompt(established_topic, mediator_q=last_mediator_q)
        c_out = run_ollama(CREATOR, c_prompt, clog, f"[{CREATOR}]") or "(no output)"
        tlog.write(f"[{ts_iso()}] CREATOR:\n{c_out}\n")

        # 3) MEDIATOR every N turns (never overwrites topic; only constrains next Creator)
        new_mediator_q: Optional[str] = None
        if mediator_every > 0 and (turn % mediator_every == 0):
            m_prompt = build_mediator_prompt(c_out)
            m_out = run_ollama(MEDIATOR, m_prompt, mlog, f"[{MEDIATOR}]")
            new_mediator_q = normalize_topic(m_out or "")
            if not new_mediator_q.endswith("?"):
                # enforce ending in a question mark
                new_mediator_q = (new_mediator_q.rstrip(". ") + "?") if new_mediator_q else \
                                 "What implicit assumption, if false, would invalidate this plan?"
            tlog.write(f"[{ts_iso()}] MEDIATOR:\n{new_mediator_q}\n")
            master.write(f"[note] Mediator constraint queued for next turn: {new_mediator_q}", also_stdout=True)

        # 4) REVIEWER — summarize the state each turn
        r_prompt = build_reviewer_prompt(established_topic, c_out, last_mediator_q)
        r_out = run_ollama(REVIEWER, r_prompt, rlog, f"[{REVIEWER}]") or "(no output)"
        tlog.write(f"[{ts_iso()}] REVIEWER:\n{r_out}\n")

        # Next turn must answer the *new* mediator question (if any)
        last_mediator_q = new_mediator_q or None

        print()

    # Close logs
    for lg in (master, qlog, clog, mlog, rlog, tlog):
        lg.close()

    print(f"[{ts_iso()}] Done. Run folder: {run_dir}")

# --------------------------- Signals --------------------------

def handle_sigint(signum, frame):
    print(f"\n[{ts_iso()}] Aborted by user.", file=sys.stderr)
    sys.exit(130)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sigint)
    try:
        main()
    except KeyboardInterrupt:
        handle_sigint(signal.SIGINT, None)
