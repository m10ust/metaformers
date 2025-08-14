#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meta Discussion — minimal loop
Ask for a first prompt, then let three local models (Questioner/Creator/Mediator)
discuss it in turns. Uses Ollama via subprocess; streams output live.
"""

import os
import sys
import time
import subprocess
import select
from datetime import datetime, timezone
from typing import Optional, List

# ========== Config (env overrides) ==========
OLLAMA_BIN      = os.environ.get("OLLAMA_BIN", "/usr/local/bin/ollama")
MODEL_QUESTION  = os.environ.get("MODEL_QUESTION", "llama2-uncensored:latest")
MODEL_CREATOR   = os.environ.get("MODEL_CREATOR",  "gpt-oss:20b")
MODEL_MEDIATOR  = os.environ.get("MODEL_MEDIATOR", "dolphin3:latest")

ITERATIONS      = int(os.environ.get("ITERATIONS", "12"))     # total turns
MEDIATOR_EVERY  = int(os.environ.get("MEDIATOR_EVERY", "3"))  # mediator cadence
TIMEOUT_SECS    = int(os.environ.get("OLLAMA_TIMEOUT", "600"))
CTX_WINDOW      = int(os.environ.get("CTX_WINDOW", "3"))      # keep last N utterances as context

# ========== Pretty logging ==========
C = {
    "ts": "\033[38;5;245m",
    "q":  "\033[38;5;214m",
    "c":  "\033[38;5;44m",
    "m":  "\033[38;5;201m",
    "ok": "\033[38;5;42m",
    "err":"\033[38;5;196m",
    "dim":"\033[2m",
    "rst":"\033[0m"
}
def ts() -> str:
    return f"{C['ts']}[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {C['rst']}"

def say(role: str, text: str) -> None:
    color = C.get(role, C["dim"])
    print(f"{ts()}{color}{text}{C['rst']}")

# ========== Ollama streaming helper ==========
def have_ollama() -> bool:
    return os.path.exists(OLLAMA_BIN) and os.access(OLLAMA_BIN, os.X_OK)

def ollama_stream(model: str, prompt: str, timeout: int = TIMEOUT_SECS) -> str:
    """
    Run `ollama run` with robust streaming:
    - Reads stdout/stderr without waiting for newline (handles models that emit long lines).
    - Times out after `timeout` seconds.
    - Returns the full captured stdout text.
    """
    cmd = [OLLAMA_BIN, "run", model]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0  # unbuffered to get characters immediately
        )
    except Exception as e:
        say("err", f"[ollama] failed to start: {e}")
        return ""

    # Feed prompt
    try:
        proc.stdin.write(prompt)
        proc.stdin.flush()
        proc.stdin.close()
    except Exception:
        pass

    out_chunks: List[str] = []
    err_chunks: List[str] = []
    start = time.time()

    # Helper to flush any remaining data when proc exits
    def _drain():
        try:
            so, se = proc.communicate(timeout=0.1)
        except Exception:
            so, se = ("", "")
        if so:
            print(so, end="")
            out_chunks.append(so)
        if se:
            for line in se.splitlines():
                print(f"[STDERR] {line}")
            err_chunks.append(se)

    # Non-blocking select loop
    stdout_fd = proc.stdout.fileno() if proc.stdout else None
    stderr_fd = proc.stderr.fileno() if proc.stderr else None

    # Print a small hint once
    printed_hint = False

    while True:
        # Timeout guard
        if time.time() - start > timeout:
            proc.kill()
            say("err", "[timeout] model exceeded time limit")
            break

        fds = []
        if stdout_fd is not None:
            fds.append(stdout_fd)
        if stderr_fd is not None:
            fds.append(stderr_fd)

        if not fds:
            break

        rlist, _, _ = select.select(fds, [], [], 0.1)

        if stdout_fd in rlist:
            chunk = proc.stdout.read(1)
            if chunk:
                if not printed_hint:
                    say("dim", "Streaming model output…")
                    printed_hint = True
                print(chunk, end="", flush=True)
                out_chunks.append(chunk)

        if stderr_fd in rlist:
            err_chunk = proc.stderr.read(1)
            if err_chunk:
                # Buffer stderr until newline for cleaner prints
                if err_chunk == "\n":
                    print(f"[STDERR] ", end="")
                else:
                    print(err_chunk, end="", flush=True)
                err_chunks.append(err_chunk)

        # Exit if process finished and pipes drained
        if proc.poll() is not None:
            # Drain any remaining buffered data
            _drain()
            break

    return "".join(out_chunks).strip()

# ========== Prompt templates ==========
Q_SEED = (
    "You are the Questioner in a 3‑AI roundtable (Questioner → Creator → Mediator).\n"
    "Your job: ask one short, pointed, technical question that best advances the user’s topic.\n"
    "Rules: reply with ONE sentence ending in '?', ≤25 words, no preamble.\n"
)
C_SEED = (
    "You are the Creator in a 3‑AI roundtable.\n"
    "Answer the Questioner with a concrete, practical proposal (4–7 sentences)."
    " Be specific about steps, small experiments, and expected signals. No code fences."
)
M_SEED = (
    "You are the Mediator in a 3‑AI roundtable.\n"
    "In ≤40 words, challenge a hidden assumption or risk in the Creator answer."
    " End with ONE incisive question. No preamble."
)

def make_context(history: List[str]) -> str:
    """Join the last few turns into a compact context."""
    if not history:
        return ""
    tail = history[-CTX_WINDOW:]
    return "\n\n".join(tail)

# ========== Transcript folder ==========
def ensure_run_dir() -> str:
    root = os.getcwd()
    runs = os.path.join(root, "runs")
    os.makedirs(runs, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(runs, run_id)
    os.makedirs(run_dir, exist_ok=True)
    return run_dir

def write_transcript(run_dir: str, text: str) -> None:
    try:
        path = os.path.join(run_dir, "transcript.txt")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception as e:
        say("err", f"[transcript] write failed: {e}")

# ========== Main ==========
def main() -> None:
    if not have_ollama():
        say("err", f"Ollama not found at {OLLAMA_BIN}")
        sys.exit(1)

    say("ok", "Meta Discussion — three local models, one topic.")
    user_topic = input("First prompt (what should they discuss?): ").strip()
    if not user_topic:
        say("err", "Empty prompt. Exiting.")
        sys.exit(2)

    run_dir = ensure_run_dir()
    history: List[str] = [f"User topic: {user_topic}"]

    say("ok", f"Run folder: {run_dir}")
    print()

    for i in range(1, ITERATIONS + 1):
        say("dim", f"=== Turn {i}/{ITERATIONS} ===")

        # Questioner
        q_prompt = (
            f"{Q_SEED}\n\n"
            f"User topic:\n{user_topic}\n\n"
            f"Recent context:\n{make_context(history)}\n\n"
            f"Your question:"
        )
        say("q", f"[{MODEL_QUESTION}] <<<")
        q_text = ollama_stream(MODEL_QUESTION, q_prompt, timeout=TIMEOUT_SECS)
        q_text = q_text.strip()
        history.append(f"Questioner: {q_text}")
        write_transcript(run_dir, f"[Q] {q_text}")
        print()

        # Creator
        c_prompt = (
            f"{C_SEED}\n\n"
            f"User topic:\n{user_topic}\n\n"
            f"Questioner asked:\n{q_text}\n\n"
            f"Recent context:\n{make_context(history)}\n\n"
            f"Creator answer:"
        )
        say("c", f"[{MODEL_CREATOR}] <<<")
        c_text = ollama_stream(
            MODEL_CREATOR,
            c_prompt,
            timeout=TIMEOUT_SECS,
        ).strip()
        history.append(f"Creator: {c_text}")
        write_transcript(run_dir, f"[C] {c_text}")
        print()

        # Mediator every N
        if i % MEDIATOR_EVERY == 0:
            m_prompt = (
                f"{M_SEED}\n\n"
                f"User topic:\n{user_topic}\n\n"
                f"Question:\n{q_text}\n\n"
                f"Creator answer:\n{c_text}\n\n"
                f"Mediator critique:"
            )
            say("m", f"[{MODEL_MEDIATOR}] <<<")
            m_text = ollama_stream(MODEL_MEDIATOR, m_prompt, timeout=TIMEOUT_SECS).strip()
            history.append(f"Mediator: {m_text}")
            write_transcript(run_dir, f"[M] {m_text}")
            print()

        # Small breath so the terminal is readable
        time.sleep(0.3)

    say("ok", "Done. Transcript saved under runs/<timestamp>/transcript.txt")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("err", "Interrupted by user.")
        sys.exit(130)

