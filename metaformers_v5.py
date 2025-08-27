#!/usr/bin/env python3
"""
Metaformers_v5 — Three-LLM Interactive Loop (Local Ollama)

Best-of-both-worlds fusion (v2 structure + v5 flexibility) **with Markdown logging**
and an auto-updating root-level `index.md` (the holy grail ToC of your runs).

NEW IN THIS VERSION
- After each run, update or create `index.md` with a top-most entry:
  - Link to runs/<timestamp>_<id>/master.md
  - Seed topic
  - Highlight (first Scriber summary, else first NextPrompt)

ROLES (strict order per turn)
  1) Questioner  — asks one relevant question to keep the loop coherent.
  2) Creator     — answers with conceptual insights; may end with `NextPrompt: ...`.
  3) MediatorQ   — one meta-question every N turns, after Creator.
  4) Scriber     — summarizes the Creator each turn.

LOGGING
  - Markdown transcript: runs/<ts>_<id>/master.md
  - Errors: runs/<ts>_<id>/errors.log (JSONL)
  - Terminal colors: robust Colorama init + ANSI fallback, TTY-aware

ENV FLAGS
  AUTO_CHAIN=1      -> prefer Creator's NextPrompt: as next seed
  ECHO_STDOUT=1     -> echo raw model blocks to terminal
  ECHO_MAX_CHARS=N  -> truncate echo blocks to N chars (0 = unlimited)
  OLLAMA_HOST, OLLAMA_PORT override defaults (127.0.0.1:11434)
  FORCE_COLOR=1     -> force color even if not a TTY
  NO_COLOR=1        -> disable color

CLI
  --seed "..."  --turns 8  --interval 2  \
  --q-model llama2-uncensored:latest  --c-model gpt-oss:20b  \
  --m-model dolphin3:latest           --s-model dolphin3:latest
"""
from __future__ import annotations

import os
import sys
import json
import uuid
import time
import signal
import atexit
import socket
import argparse
import subprocess
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# -----------------------------
# Echo controls
# -----------------------------
AUTO_CHAIN   = os.environ.get("AUTO_CHAIN", "0").lower() in ("1","true","yes","on")
ECHO_STDOUT  = os.environ.get("ECHO_STDOUT", "0").lower() not in ("0","false","no","off")
try:
    ECHO_MAX_CHARS = int(os.environ.get("ECHO_MAX_CHARS", "0"))
except Exception:
    ECHO_MAX_CHARS = 0

# -----------------------------
# Ollama host
# -----------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1")
OLLAMA_PORT = int(os.environ.get("OLLAMA_PORT", "11434"))
OLLAMA_BASE = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}"

# -----------------------------
# Default models
# -----------------------------
DEFAULTS = {
    "Questioner": "llama2-uncensored:latest",
    "Creator":    "gpt-oss:20b",
    "MediatorQ":  "dolphin3:latest",
    "Scriber":    "dolphin3:latest",
}

# -----------------------------
# Color handling (robust)
# -----------------------------
def _detect_color_enabled() -> bool:
    if os.environ.get("FORCE_COLOR", "").lower() in ("1","true","yes","on"):
        return True
    if os.environ.get("NO_COLOR", ""):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False

COLOR_ENABLED = _detect_color_enabled()

try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
    _RESET = Style.RESET_ALL if COLOR_ENABLED else ""
    ROLE_COLORS = {
        "Questioner": Fore.BLUE   if COLOR_ENABLED else "",
        "Creator":    Fore.GREEN  if COLOR_ENABLED else "",
        "MediatorQ":  Fore.YELLOW if COLOR_ENABLED else "",
        "Scriber":    Fore.CYAN   if COLOR_ENABLED else "",
        "Error":      Fore.RED    if COLOR_ENABLED else "",
    }
except Exception:  # ANSI fallback if colorama missing
    BLUE  = "\033[34m"
    GREEN = "\033[32m"
    YEL   = "\033[33m"
    CYAN  = "\033[36m"
    RED   = "\033[31m"
    RESET = "\033[0m"
    _RESET = RESET if COLOR_ENABLED else ""
    ROLE_COLORS = {
        "Questioner": BLUE  if COLOR_ENABLED else "",
        "Creator":    GREEN if COLOR_ENABLED else "",
        "MediatorQ":  YEL   if COLOR_ENABLED else "",
        "Scriber":    CYAN  if COLOR_ENABLED else "",
        "Error":      RED   if COLOR_ENABLED else "",
    }

def print_color(role: str, text: str) -> None:
    color = ROLE_COLORS.get(role, "")
    print(f"{color}{text}{_RESET}")

# -----------------------------
# Optional requests; urllib fallback
# -----------------------------
try:
    import requests  # type: ignore
except Exception:
    requests = None
    import urllib.request
    import urllib.error

# -----------------------------
# Utility helpers
# -----------------------------
def iso_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")

def yes_no(prompt: str, default: bool=True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        resp = input(f"{prompt} {suffix} ").strip().lower()
        if not resp: return default
        if resp in ("y","yes"): return True
        if resp in ("n","no"):  return False
        print_color("Error", "Please answer y/yes or n/no.")

def input_nonempty(prompt: str) -> str:
    while True:
        s = input(prompt).strip()
        if s: return s
        print_color("Error", "Input cannot be empty.")

def input_int(prompt: str, minimum: Optional[int]=None, allow_zero: bool=False) -> int:
    while True:
        raw = input(prompt).strip()
        try:
            val = int(raw)
            if (minimum is not None and val < minimum) or (not allow_zero and val == 0):
                raise ValueError
            return val
        except Exception:
            min_desc = f" >= {minimum}" if minimum is not None else ""
            if allow_zero: min_desc += " (0 allowed)"
            print_color("Error", f"Enter a valid integer{min_desc}.")

def echo_block(tag: str, content: str) -> None:
    if not ECHO_STDOUT: return
    out = content
    if ECHO_MAX_CHARS and len(out) > ECHO_MAX_CHARS:
        tail_nl = "\n" if out.endswith("\n") else ""
        omitted = len(out) - ECHO_MAX_CHARS
        out = out[:ECHO_MAX_CHARS] + f"... [truncated {omitted} chars]" + tail_nl
    print(f"[{iso_ts()}] [{tag}] <<<")
    print(out)

# -----------------------------
# Ollama helpers
# -----------------------------
def is_port_open(host: str, port: int, timeout: float=0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def list_ollama_models() -> List[str]:
    models: List[str] = []
    # Try CLI first
    try:
        proc = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout:
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            for ln in (lines[1:] if len(lines) > 1 else []):
                name = ln.split()[0]
                if name and name not in models:
                    models.append(name)
    except Exception:
        pass
    # Fallback to HTTP tags
    if not models and is_port_open(OLLAMA_HOST, OLLAMA_PORT):
        try:
            if requests:
                r = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
                r.raise_for_status()
                data = r.json()
            else:
                with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            for md in data.get("models", []):
                name = md.get("name")
                if name and name not in models:
                    models.append(name)
        except Exception:
            pass
    return sorted(models)

def choose_model(role: str, available: List[str], default_name: str) -> str:
    print(f"\nSelect model for {role} (default: {default_name})")
    if not available:
        print_color("Error", "No Ollama models detected. Pull models with `ollama pull <name>`.")
        sys.exit(1)
    for idx, name in enumerate(available, start=1):
        mark = " (default)" if name == default_name else ""
        print(f"  {idx}. {name}{mark}")
    while True:
        choice = input("Enter number or press Enter for default: ").strip()
        if not choice:
            if default_name in available: return default_name
            print_color("Error", f"Default '{default_name}' not installed; choose an index.")
            continue
        if not choice.isdigit():
            print_color("Error", "Enter a number or press Enter.")
            continue
        idx = int(choice)
        if 1 <= idx <= len(available):
            return available[idx-1]
        print_color("Error", "Choice out of range.")

def ollama_chat(model: str, messages: List[Dict[str, str]],
                temperature: float=0.7, top_p: float=0.95, timeout: int=180) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "top_p": top_p},
    }
    try:
        if requests:
            r = requests.post(f"{OLLAMA_BASE}/api/chat", json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        else:
            req = urllib.request.Request(
                f"{OLLAMA_BASE}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        msg = data.get("message", {})
        content = msg.get("content") or data.get("response", "")
        return content or ""
    except Exception as e:
        raise RuntimeError(f"Ollama chat failed for '{model}': {e}")

# -----------------------------
# Markdown Logger
# -----------------------------
class RunLogger:
    """Writes a human-friendly Markdown transcript + JSONL errors."""
    def __init__(self, run_dir: str, seed: str, config: dict):
        self.run_dir = run_dir
        self.master_md = os.path.join(run_dir, "master.md")
        self.errors_path = os.path.join(run_dir, "errors.log")
        with open(self.master_md, "w", encoding="utf-8") as f:
            f.write("# Metaformers Transcript\n\n")
            f.write(f"_Run: {os.path.basename(run_dir)} • Started: {iso_ts()}_\n\n")
            f.write("## Configuration\n\n")
            for k, v in config.items():
                f.write(f"- **{k}**: {v}\n")
            f.write("\n---\n\n")
            f.write("## Seed\n\n")
            f.write(f"{seed}\n\n")
            f.write("---\n\n")

    def error(self, turn: int, description: str, role: Optional[str]=None) -> None:
        entry = {"turn": turn, "error": description, "role": role, "timestamp": iso_ts()}
        with open(self.errors_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def turn_header(self, turn: int) -> None:
        with open(self.master_md, "a", encoding="utf-8") as f:
            f.write(f"# Turn {turn}\n\n")

    def role_block(self, role: str, content: str) -> None:
        with open(self.master_md, "a", encoding="utf-8") as f:
            f.write(f"## {role}\n\n")
            f.write(content.rstrip() + "\n\n")

    def next_topic(self, next_topic: str) -> None:
        with open(self.master_md, "a", encoding="utf-8") as f:
            f.write(f"**NextTopic →** {next_topic}\n\n")
            f.write("---\n\n")

    def validate_turn(self, turn: int, roles_present: List[str]) -> Tuple[bool, List[str]]:
        problems: List[str] = []
        try:
            with open(self.master_md, "r", encoding="utf-8") as f:
                text = f.read()
            if f"# Turn {turn}\n" not in text and f"# Turn {turn}\r\n" not in text:
                problems.append(f"Turn header missing for Turn {turn}")
            turn_pattern = re.compile(rf"# Turn {turn}([\s\S]*?)(?:\n# Turn {turn+1}|\Z)")
            m = turn_pattern.search(text)
            if not m:
                problems.append(f"Could not isolate Turn {turn} block")
            else:
                block = m.group(1)
                for role in roles_present:
                    if f"## {role}" not in block:
                        problems.append(f"Missing section for {role} in Turn {turn}")
        except Exception as e:
            problems.append(f"Validation read error: {e}")
        return (len(problems) == 0), problems

# -----------------------------
# Role prompts
# -----------------------------
def build_questioner_prompt(turn: int, seed_prompt: str,
                            prev_creator: Optional[str],
                            last_mediator: Optional[str],
                            chained_seed: Optional[str]) -> List[Dict[str,str]]:
    if chained_seed:
        sys_msg = ("You are Questioner. Output EXACTLY the provided topic, fixing only obvious typing, spacing, "
                   "and punctuation. If not a question, add a trailing '?'. One line only.")
        return [{"role": "system", "content": sys_msg},
                {"role": "user",   "content": chained_seed}]

    if turn == 1:
        sys_msg = "You are Questioner. Reformulate the seed into ONE precise, curious question. Keep it short. Do not answer."
        user_msg = f"Seed Prompt:\n{seed_prompt}\n\nProduce exactly one clear question."
        return [{"role": "system", "content": sys_msg},
                {"role": "user",   "content": user_msg}]

    context_lines = ["Previous Creator answer:", prev_creator or ""]
    if last_mediator:
        context_lines += ["", "Last Mediator prompt to consider:", last_mediator]
    context = "\n".join(context_lines)
    sys_msg = ("You are Questioner. Ask ONE follow-up that advances the thread, focuses on a key detail, "
               "challenges an assumption, or requests clarification for progress. Keep it under 30 words.")
    user_msg = context + "\n\nNow ask one concise, relevant question."
    return [{"role": "system", "content": sys_msg},
            {"role": "user",   "content": user_msg}]

def build_creator_prompt(question: str) -> List[Dict[str,str]]:
    sys_msg = ("You are Creator. Answer with conceptual insights and elaboration, organized and forward-looking. "
               "If natural, end with a single line 'NextPrompt: <succinct follow-up>' to guide the next turn.")
    user_msg = f"Question from Questioner:\n{question}"
    return [{"role": "system", "content": sys_msg},
            {"role": "user",   "content": user_msg}]

def build_mediator_prompt(turn: int, question: str, creator_answer: str) -> List[Dict[str,str]]:
    sys_msg = "You are MediatorQ. Provide exactly ONE brief meta-question or reflection (max 2 sentences) that would sharpen the next step."
    user_msg = (f"Turn {turn} context:\nQuestion:\n{question}\n\nCreator answered:\n{creator_answer}\n\n"
                "Give one meta-question/reflection to refine direction.")
    return [{"role": "system", "content": sys_msg},
            {"role": "user",   "content": user_msg}]

def build_scriber_prompt(creator_answer: str) -> List[Dict[str,str]]:
    sys_msg = "You are Scriber. Summarize the Creator's response in 2-4 tight bullet points. No fluff. Capture core claims and next steps."
    user_msg = f"Summarize this:\n{creator_answer}"
    return [{"role": "system", "content": sys_msg},
            {"role": "user",   "content": user_msg}]

# -----------------------------
# Run directory + index.md management
# -----------------------------
def prepare_run_dir() -> str:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    rid = uuid.uuid4().hex[:8]
    run_dir = os.path.join("runs", f"{ts}_{rid}")
    print(f"\nPlanned run directory: {run_dir}")
    if not yes_no("Create this directory and start logging?", default=True):
        print_color("Error", "Aborted by user before creating run directory.")
        sys.exit(1)
    os.makedirs(run_dir, exist_ok=False)
    print("Log files:")
    for fn in ("master.md","errors.log"):
        print(f"  - {os.path.join(run_dir, fn)}")
    return run_dir

def banner_legend():
    print("\n=== Terminal Color Legend ===")
    print_color("Questioner", "Questioner: Blue")
    print_color("Creator",    "Creator: Green")
    print_color("MediatorQ",  "MediatorQ: Yellow")
    print_color("Scriber",    "Scriber: Cyan")
    print_color("Error",      "Errors: Red")
    print("============================\n")

def _ensure_index_header(text: str) -> str:
    if "# Metaformers Knowledge Index" in text:
        return text
    return (
        "# Metaformers Knowledge Index\n\n"
        "_An evolving log of all experiments, runs, and insights._\n\n"
        "---\n\n"
        "## 📅 Recent Runs\n\n"
    ) + text

def _format_index_entry(run_dir: str, seed: str, highlight: Optional[str]) -> str:
    run_name = os.path.basename(run_dir)
    link = f"runs/{run_name}/master.md"
    seed_snip = seed.strip().replace("\n", " ")
    if len(seed_snip) > 120:
        seed_snip = seed_snip[:117] + "..."
    hl = (highlight or "").strip().replace("\n", " ")
    if len(hl) > 160:
        hl = hl[:157] + "..."
    lines = [
        f"- [{run_name}]({link})  ",
        f"  *Topic:* `{seed_snip}`",
    ]
    if hl:
        lines.append(f"  **Highlight:** {hl}")
    return "\n".join(lines) + "\n\n"

def update_root_index(run_dir: str, seed: str, highlight: Optional[str]) -> None:
    path = "index.md"
    entry = _format_index_entry(run_dir, seed, highlight)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(_ensure_index_header("") + entry)
        print("Updated index.md (created).")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
    except Exception:
        existing = ""
    existing = _ensure_index_header(existing)
    # Insert entry right after "## 📅 Recent Runs"
    anchor = "## 📅 Recent Runs"
    idx = existing.find(anchor)
    if idx == -1:
        new_text = _ensure_index_header("") + entry + existing
    else:
        # find end of the anchor line
        after = existing.find("\n", idx)
        if after == -1:
            after = len(existing)
        new_text = existing[:after+1] + entry + existing[after+1:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    print("Updated index.md.")

# -----------------------------
# CLI
# -----------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Metaformers_v5 — local multi-agent loop (Ollama) with Markdown logging + index.md")
    p.add_argument("--seed", type=str, help="Seed prompt/topic")
    p.add_argument("--turns", type=int, help="Number of turns >=1")
    p.add_argument("--interval", type=int, help="MediatorQ interval (0=off)")
    p.add_argument("--q-model", type=str, help="Model for Questioner")
    p.add_argument("--c-model", type=str, help="Model for Creator")
    p.add_argument("--m-model", type=str, help="Model for MediatorQ")
    p.add_argument("--s-model", type=str, help="Model for Scriber")
    p.add_argument("--temperature", type=float, default=0.7, help="Generation temperature")
    p.add_argument("--top-p", type=float, default=0.95, help="Top-p")
    return p.parse_args()

# -----------------------------
# Main orchestration
# -----------------------------
def main():
    def on_sigint(sig, frame):
        print_color("Error", "\nInterrupted (Ctrl+C). Exiting...")
        sys.exit(1)
    signal.signal(signal.SIGINT, on_sigint)
    atexit.register(lambda: None)

    print("Metaformers_v5 — Local Multi-Agent Orchestrator (Ollama)")
    print("Only local models are used. Ensure Ollama is running on 127.0.0.1:11434.")

    if not is_port_open(OLLAMA_HOST, OLLAMA_PORT):
        print_color("Error", f"Cannot reach Ollama at {OLLAMA_BASE}. Start it with `ollama serve`.")
        sys.exit(1)

    args = parse_args()
    available = list_ollama_models()
    if not available:
        print_color("Error", "No models detected. Use `ollama pull <model>` first.")
        sys.exit(1)

    seed_prompt = args.seed or input_nonempty("Enter seed prompt: ")
    turns = args.turns if args.turns is not None else input_int("Number of turns (>=1): ", minimum=1)
    mediator_interval = args.interval if args.interval is not None else input_int("MediatorQ interval (0=disable): ", minimum=0, allow_zero=True)

    q_model = args.q_model or choose_model("Questioner", available, DEFAULTS["Questioner"])
    c_model = args.c_model or choose_model("Creator",    available, DEFAULTS["Creator"])
    m_model = args.m_model or choose_model("MediatorQ",  available, DEFAULTS["MediatorQ"])
    s_model = args.s_model or choose_model("Scriber",    available, DEFAULTS["Scriber"])

    print("\nConfiguration summary:")
    print(f"  Seed: {seed_prompt[:80] + ('…' if len(seed_prompt)>80 else '')}")
    print(f"  Turns: {turns}")
    print(f"  MediatorQ interval: {mediator_interval if mediator_interval>0 else 'disabled'}")
    print(f"  Questioner: {q_model}")
    print(f"  Creator:    {c_model}")
    print(f"  MediatorQ:  {m_model}")
    print(f"  Scriber:    {s_model}")

    run_dir = prepare_run_dir()
    logger = RunLogger(
        run_dir,
        seed_prompt,
        config={
            "turns": turns,
            "mediator_interval": mediator_interval if mediator_interval>0 else "disabled",
            "Questioner": q_model,
            "Creator": c_model,
            "MediatorQ": m_model,
            "Scriber": s_model,
        },
    )
    banner_legend()

    prev_creator: Optional[str] = None
    last_mediator: Optional[str] = None
    chained_seed: Optional[str] = None

    # capture highlight for index.md (first Scriber; else first NextPrompt)
    first_highlight: Optional[str] = None
    first_nextprompt: Optional[str] = None

    for turn in range(1, turns+1):
        mediator_triggered = (mediator_interval > 0 and turn % mediator_interval == 0)
        logger.turn_header(turn)

        # Questioner
        try:
            q_msgs = build_questioner_prompt(turn, seed_prompt, prev_creator, last_mediator,
                                             chained_seed if AUTO_CHAIN else None)
            q_out = ollama_chat(q_model, q_msgs, temperature=args.temperature, top_p=args.top_p).strip()
            if not q_out:
                raise RuntimeError("Empty response from Questioner")
            print_color("Questioner", f"Q{turn}: {q_out}\n")
            echo_block(q_model, q_out)
            logger.role_block("Questioner", q_out)
        except Exception as e:
            desc = f"Questioner error: {e}"
            print_color("Error", desc)
            logger.error(turn, desc, role="Questioner")

        # Creator
        try:
            c_msgs = build_creator_prompt(q_out)
            c_out = ollama_chat(c_model, c_msgs, temperature=args.temperature, top_p=args.top_p).strip()
            if not c_out:
                raise RuntimeError("Empty response from Creator")
            print_color("Creator", f"C{turn}: {c_out}\n")
            echo_block(c_model, c_out)
            logger.role_block("Creator", c_out)
            prev_creator = c_out

            # capture first NextPrompt as possible highlight
            if first_nextprompt is None:
                for line in c_out.splitlines():
                    if line.strip().lower().startswith("nextprompt:"):
                        first_nextprompt = line.split(":", 1)[1].strip()
                        break

            if AUTO_CHAIN:
                nxt = None
                for line in c_out.splitlines():
                    if line.strip().lower().startswith("nextprompt:"):
                        nxt = line.split(":", 1)[1].strip()
                chained_seed = nxt if nxt else f"Refine: {q_out}"
        except Exception as e:
            desc = f"Creator error: {e}"
            print_color("Error", desc)
            logger.error(turn, desc, role="Creator")

        # MediatorQ (conditional)
        if mediator_triggered:
            try:
                m_msgs = build_mediator_prompt(turn, q_out, prev_creator or "")
                mediator_out = ollama_chat(m_model, m_msgs,
                                           temperature=max(0.2, args.temperature/2), top_p=args.top_p).strip()
                if not mediator_out:
                    raise RuntimeError("Empty response from MediatorQ")
                print_color("MediatorQ", f"M{turn}: {mediator_out}\n")
                echo_block(m_model, mediator_out)
                logger.role_block("MediatorQ", mediator_out)
                last_mediator = mediator_out
            except Exception as e:
                desc = f"MediatorQ error: {e}"
                print_color("Error", desc)
                logger.error(turn, desc, role="MediatorQ")

        # Scriber
        try:
            s_msgs = build_scriber_prompt(prev_creator or "")
            summary_out = ollama_chat(s_model, s_msgs,
                                      temperature=max(0.2, args.temperature/2), top_p=args.top_p).strip()
            if not summary_out:
                raise RuntimeError("Empty response from Scriber")
            print_color("Scriber", f"S{turn}: {summary_out}\n")
            echo_block(s_model, summary_out)
            logger.role_block("Scriber", summary_out)

            # first highlight = first Scriber summary
            if first_highlight is None:
                first_highlight = summary_out
        except Exception as e:
            desc = f"Scriber error: {e}"
            print_color("Error", desc)
            logger.error(turn, desc, role="Scriber")

        # Next topic hint (from Creator if present)
        next_topic = None
        if prev_creator:
            for line in prev_creator.splitlines()[::-1]:
                if line.strip().lower().startswith("nextprompt:"):
                    next_topic = line.split(":", 1)[1].strip()
                    break
        if not next_topic and AUTO_CHAIN and 'q_out' in locals():
            next_topic = f"Refine: {q_out}"
        if next_topic:
            logger.next_topic(next_topic)

        # Validation
        roles_present = ["Questioner", "Creator", "Scriber"] + (["MediatorQ"] if mediator_triggered else [])
        ok, problems = logger.validate_turn(turn, roles_present)
        if ok:
            print(f"Turn {turn} logged successfully.")
        else:
            print_color("Error", f"Logging validation issues on Turn {turn}:")
            for p in problems:
                print_color("Error", f"  - {p}")
                logger.error(turn, p, role="Validator")

    # --- Update root-level index.md with this run ---
    highlight = first_highlight or (f"Next: {first_nextprompt}" if first_nextprompt else None)
    update_root_index(run_dir, seed_prompt, highlight)

    print("\nAll done. Review the Markdown transcript in the run directory (master.md).")
    print("Also updated root index.md ✅\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)