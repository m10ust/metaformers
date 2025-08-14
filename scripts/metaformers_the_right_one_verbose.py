#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metaformers — The Right One (Verbose)
macOS-friendly builders that try to discover *the right prompts* and execute
local build steps. Uses Ollama models for Questioner/Creator/Mediator.
"""

import os
import sys
import time
import shlex
import subprocess
import json
from datetime import datetime, timezone
from threading import Thread, Event
from typing import Optional, Tuple, List, Dict

# ==========================
# Global config (env override)
# ==========================
OLLAMA_BIN: str = os.environ.get("OLLAMA_BIN", "/usr/local/bin/ollama")
AI_QUESTIONER: str = os.environ.get("AI_QUESTIONER", "llama2-uncensored:latest")
AI_CREATOR: str    = os.environ.get("AI_CREATOR",    "gpt-oss:20b")
AI_MEDIATOR: str   = os.environ.get("AI_MEDIATOR",   "dolphin3:latest")

ITERATIONS: int = int(os.environ.get("ITERATIONS", "20"))
MEDIATOR_EVERY: int = int(os.environ.get("MEDIATOR_EVERY", "5"))
TIMEOUT_SECS: int = int(os.environ.get("OLLAMA_TIMEOUT", "900"))
REAL_OPS: bool = os.environ.get("DRY_RUN", "0") != "1"   # default: real ops ON

CREATOR_THINK_SECS: int = int(os.environ.get("CREATOR_THINK_SECS", "30"))

# Conservative decoding for Creator (overridable via env)
CREATOR_OLLAMA_OPTS: Dict[str, str] = {
    "temperature": os.environ.get("CREATOR_TEMP", "0.2"),
    "num_ctx": os.environ.get("CREATOR_NUM_CTX", "4096"),
}

ROOT: str = os.getcwd()
RUNS_DIR: str = os.path.join(ROOT, "runs")
LOCAL_META_DIR: str = os.path.join(ROOT, "local-meta")
os.makedirs(RUNS_DIR, exist_ok=True)
os.makedirs(LOCAL_META_DIR, exist_ok=True)

# ========== COLORS ==========
C = {
    "ts": "\033[38;5;245m",
    "ok": "\033[38;5;42m",
    "warn": "\033[38;5;214m",
    "err": "\033[38;5;196m",
    "info": "\033[38;5;39m",
    "cmd": "\033[38;5;63m",
    "write": "\033[38;5;135m",
    "model": "\033[38;5;207m",
    "reset": "\033[0m",
}

def _ts() -> str:
    """Return an ISO8601 UTC timestamp with ANSI color."""
    return f"{C['ts']}[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {C['reset']}"

def log_info(msg: str) -> None:
    print(f"{_ts()}{C['info']}{msg}{C['reset']}")

def log_ok(msg: str) -> None:
    print(f"{_ts()}{C['ok']}{msg}{C['reset']}")

def log_warn(msg: str) -> None:
    print(f"{_ts()}{C['warn']}{msg}{C['reset']}")

def log_err(msg: str) -> None:
    print(f"{_ts()}{C['err']}{msg}{C['reset']}")

def log_model(msg: str) -> None:
    print(f"{_ts()}{C['model']}{msg}{C['reset']}")

def log_cmd(msg: str) -> None:
    print(f"{_ts()}{C['cmd']}{msg}{C['reset']}")

def log_write(pth: str) -> None:
    print(f"{_ts()}{C['write']}[write] {pth}{C['reset']}")


# ========== SAFETY: COMMAND BLACKLIST ==========
BLACKLIST_SUBSTR: List[str] = [
    " rm -rf /", " rm -fr /", " rm -rf -- /", " :(){ :|:& };:",
    "sudo ", "shutdown", "reboot", "halt",
    "diskutil", "launchctl", "scutil", "ifconfig", "route ", "pfctl",
    "mkfs", "newfs", "mount ", "umount ",
    "kill -9 ", "killall ",
    "dd if=", " of=/dev/",
    "chown -R /", "chmod 777 -R /",
    ">/dev/", ">>/dev/",
    "curl | sh", "wget | sh",
]

def is_blacklisted(cmd: str) -> Optional[str]:
    """
    Return a matched blacklisted substring if the command is unsafe; otherwise None.
    """
    low = " " + cmd.strip().lower() + " "
    for bad in BLACKLIST_SUBSTR:
        if bad in low:
            return bad.strip()
    return None


# ========== SHELL EXEC ==========
def run_shell(command: str, timeout: int = 300) -> Tuple[bool, str, str, int]:
    """
    Run a shell command with streaming stdout/stderr and timeout.
    Returns (ok, stdout, stderr, rc).
    """
    log_cmd(f"$ {command}")
    if not REAL_OPS:
        log_warn("[dry-run] skipped")
        return True, "", "", 0

    bad = is_blacklisted(command)
    if bad:
        log_err(f"[blocked] command contains blacklisted pattern: {bad}")
        return False, "", f"blocked: {bad}", 127

    proc = subprocess.Popen(
        command, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    start = time.time()
    out_lines: List[str] = []
    err_lines: List[str] = []
    try:
        while True:
            if proc.poll() is not None:
                break
            line = proc.stdout.readline()
            if line:
                print(line.rstrip())
                out_lines.append(line)
            eline = proc.stderr.readline()
            if eline:
                print(f"[STDERR] {eline.rstrip()}")
                err_lines.append(eline)
            if time.time() - start > timeout:
                proc.kill()
                return False, "".join(out_lines), "".join(err_lines), 124
            time.sleep(0.01)
    finally:
        try:
            rest_out, rest_err = proc.communicate(timeout=0.2)
        except Exception:
            rest_out, rest_err = ("", "")
        if rest_out:
            out_lines.append(rest_out)
        if rest_err:
            err_lines.append(rest_err)

    rc = proc.returncode or 0
    ok = (rc == 0)
    if ok:
        log_ok(f"[exit {rc}]")
    else:
        log_err(f"[exit {rc}]")
    return ok, "".join(out_lines), "".join(err_lines), rc


def _timer_countdown(seconds: int, stop: Event) -> None:
    """
    Print a live countdown like '⏳ 30s' once per second on the same line,
    until seconds elapse or the stop event is set.
    """
    if seconds <= 0:
        return
    for s in range(seconds, 0, -1):
        if stop.is_set():
            break
        print(f"\r⏳ {s:02d}s ", end="", flush=True)
        time.sleep(1)
    # Clear the line end when done
    print("\r", end="", flush=True)

# ========== OLLAMA ==========
def have_ollama() -> bool:
    """Check if ollama binary exists and is executable."""
    return os.path.exists(OLLAMA_BIN) and os.access(OLLAMA_BIN, os.X_OK)

def ollama_run(model: str, prompt: str, timeout: int = TIMEOUT_SECS, think_secs: int = 0, options: Optional[Dict[str, str]] = None) -> str:
    """
    Run an Ollama model with the given prompt; stream output for verbosity.
    Optional 'options' maps to '-o key=value' pairs.
    The visible countdown (if any) stops immediately upon receiving the first token.
    """
    if not have_ollama():
        log_err(f"Ollama not found at: {OLLAMA_BIN}")
        return ""
    log_model(f"[{model}] <<<")
    log_info("Streaming model output…")

    cmd = [OLLAMA_BIN, "run"]
    if options:
        for k, v in options.items():
            cmd += ["-o", f"{k}={v}"]
    cmd += [model]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1
        )
    except Exception as e:
        log_err(f"Ollama launch failed: {e}")
        return ""

    # Send prompt
    try:
        proc.stdin.write(prompt)
        proc.stdin.flush()
        proc.stdin.close()
    except Exception:
        pass

    # Start a live timer while the model is thinking; stop on first token
    stop_evt = Event()
    timer_thread = None
    if think_secs and think_secs > 0:
        timer_thread = Thread(target=_timer_countdown, args=(think_secs, stop_evt), daemon=True)
        timer_thread.start()

    start = time.time()
    chunks: List[str] = []
    first_token = False

    try:
        while True:
            if proc.poll() is not None:
                break

            line = proc.stdout.readline()
            if line:
                if not first_token:
                    first_token = True
                    stop_evt.set()
                    print("\r", end="", flush=True)
                    log_info("[creator] first token received.")
                print(line.rstrip())
                chunks.append(line)

            el = proc.stderr.readline()
            if el:
                if not first_token:
                    first_token = True
                    stop_evt.set()
                    print("\r", end="", flush=True)
                    log_info("[creator] first token (stderr) received.")
                print(f"[STDERR] {el.rstrip()}")

            if time.time() - start > timeout:
                proc.kill()
                log_err("[timeout] ollama exceeded")
                break

            time.sleep(0.01)
    finally:
        try:
            rest, rest_err = proc.communicate(timeout=0.2)
        except Exception:
            rest, rest_err = ("", "")
        if rest:
            print(rest.rstrip())
            chunks.append(rest)
        if rest_err:
            print(f"[STDERR] {rest_err.rstrip()}")

        try:
            stop_evt.set()
        except Exception:
            pass
        if timer_thread is not None:
            try:
                timer_thread.join(timeout=0.2)
            except Exception:
                pass

    return "".join(chunks)


# ========== Parsing utilities ==========
def _section(text: str, header: str) -> str:
    """
    Return the text under a markdown '## {header}' section until the next '## ' or EOF.
    Case-insensitive match on header.
    """
    lines = text.splitlines()
    start = -1
    target = f"## {header}".lower()
    for i, ln in enumerate(lines):
        if ln.strip().lower() == target:
            start = i + 1
            break
    if start == -1:
        return ""
    buf: List[str] = []
    for j in range(start, len(lines)):
        if j > start and lines[j].startswith("## "):
            break
        buf.append(lines[j])
    return "\n".join(buf).strip()

def parse_topic(plan: str) -> str:
    """
    Extract a one-line topic from the '## Topic' section, else first non-header line.
    """
    sec = _section(plan, "Topic")
    for ln in sec.splitlines():
        s = ln.strip()
        if s:
            return s.strip('"')
    for ln in plan.splitlines():
        s = ln.strip()
        if s and not s.startswith("##"):
            return s
    return ""

def parse_files(plan: str) -> List[Dict[str, str]]:
    """
    Returns a list of dicts: [{"path": "./local-meta/...", "content": "..."}]
    Recognizes:
      - list lines: "- ./local-meta/path : purpose"
      - fenced blocks:
          ```path=./local-meta/path
          ...contents...
          ```
          ```./local-meta/path
          ...contents...
          ```
          ```file: ./local-meta/path
          ...contents...
          ```
    If a path appears only in list, its content is empty until a matching block appears.
    """
    files: Dict[str, str] = {}

    # list lines
    for ln in _section(plan, "Files").splitlines():
        s = ln.strip()
        if not s or s.startswith("|"):
            continue
        if s.startswith("-"):
            body = s.lstrip("-").strip()
            if body.startswith("./"):
                pth = body.split(":", 1)[0].strip()
                files.setdefault(pth, "")

    # fenced blocks with various headers
    lines = plan.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("```"):
            hdr = ln.strip("`").strip()
            pth = ""
            if "path=" in hdr:
                try:
                    _, path_part = hdr.split("path=", 1)
                    pth = path_part.strip()
                except ValueError:
                    pth = ""
            elif hdr.startswith("./local-meta/"):
                pth = hdr
            elif hdr.lower().startswith("file:"):
                pth = hdr.split(":", 1)[1].strip()
            i += 1
            buf: List[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            if pth:
                files[pth] = "\n".join(buf)
        i += 1

    out: List[Dict[str, str]] = []
    for pth, content in files.items():
        if pth:
            out.append({"path": pth, "content": content})
    return out

def parse_commands(plan: str) -> List[str]:
    """
    Optional '## Commands' section.
    Accepts list items beginning with '-' and code fences labeled ```bash or ```sh.
    Returns a list of shell strings.
    """
    sec = _section(plan, "Commands")
    cmds: List[str] = []
    for ln in sec.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("-"):
            s = s.lstrip("-").strip()
            if s and not s.startswith("#"):
                cmds.append(s)
    # fenced code (accept bash, sh, or unlabeled)
    lines = sec.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("```"):
            label = ln.strip("`").strip().lower()
            if ("bash" in label) or ("sh" in label) or (label == ""):
                i += 1
                buf: List[str] = []
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    if lines[i].strip() and not lines[i].strip().startswith("#"):
                        buf.append(lines[i])
                    i += 1
                for c in buf:
                    c = c.strip()
                    if c:
                        cmds.append(c)
        i += 1
    return cmds


# ========== File operations ==========
def safe_write_file(repo_root: str, rel_or_abs: str, content: str) -> bool:
    """
    Safely write a file inside the repository root. Returns True on success.
    """
    pth = rel_or_abs
    if pth.startswith("./"):
        pth = os.path.join(repo_root, pth[2:])
    elif not os.path.isabs(pth):
        pth = os.path.join(repo_root, pth)
    pth = os.path.realpath(pth)
    root_real = os.path.realpath(repo_root)
    if not pth.startswith(root_real + os.sep):
        log_err(f"[write-blocked] path escapes repo: {pth}")
        return False
    os.makedirs(os.path.dirname(pth), exist_ok=True)
    try:
        with open(pth, "w", encoding="utf-8") as f:
            f.write(content)
        log_write(pth)
        return True
    except Exception as e:
        log_err(f"[write-failed] {pth}: {e}")
        return False


RIGHT_PROMPT_SEED = (
    "You are the Questioner in a three‑AI loop that is searching for THE RIGHT PROMPTS to make a local, offline LLM (Ollama + Python + Postgres on macOS) build and improve itself. "
    "Propose ONE sharp, technical research question (single sentence ending with '?') that, if answered, would lead to an executable plan with concrete files and commands. "
    "Stay on-topic: prompts for data curation, evaluation suites (ECE, exact‑match/Rouge‑L), routing, quantization, LoRA/adapters, reproducible bash/python steps. Avoid generic productivity/self‑help."
)

def prompt_questioner(prev_topic: str) -> str:
    """
    Build the Questioner prompt; include previous topic if any. Forces a single interrogative sentence.
    """
    tail = (
        RIGHT_PROMPT_SEED
        + "\nOutput exactly one question in quotes, no commentary. Keep it under 25 words."
    )
    if prev_topic:
        return (
            f"Our last topic was: {prev_topic}\n"
            f"{tail}"
        )
    return tail

def prompt_creator(topic: str) -> str:
    """
    Force the Creator to output ONLY compact JSON with fields:
      files: [{ "path": "./local-meta/...", "content": "..." }, ...]
      commands: ["...", ...]
    No prose, no code fences, no 'Thinking...'.
    """
    return (
        f"Topic: {topic}\n"
        "You are the Creator. Output ONLY valid minified JSON (single line) with this exact schema:\n"
        "{\"files\":[{\"path\":\"./local-meta/FILE\",\"content\":\"...\"}],\"commands\":[\"...\"]}\n"
        "Rules:\n"
        "- Do NOT include any text before/after the JSON.\n"
        "- Use macOS-safe relative paths under ./local-meta.\n"
        "- If unsure, return a minimal plan creating hello.txt and running '/bin/echo ok'.\n"
        "Example:\n"
        "{\"files\":[{\"path\":\"./local-meta/hello.txt\",\"content\":\"hello from builders\"}],\"commands\":[\"/bin/echo ok\"]}\n"
    )
def parse_creator_json(s: str) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Try to parse a JSON plan into (files, commands).
    Accepts the first brace-to-brace JSON object found if extra junk surrounds it.
    """
    s = s.strip()
    try:
        obj = json.loads(s)
        return obj.get("files", []) or [], obj.get("commands", []) or []
    except Exception:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        chunk = s[start:end+1]
        try:
            obj = json.loads(chunk)
            return obj.get("files", []) or [], obj.get("commands", []) or []
        except Exception:
            return [], []
    return [], []

def fallback_json_plan(topic: str) -> Tuple[List[Dict[str, str]], List[str]]:
    files = [{"path": "./local-meta/hello.txt", "content": "hello from builders"}]
    cmds  = ["/bin/echo ok"]
    return files, cmds

def prompt_mediator(plan: str) -> str:
    """
    Build the Mediator prompt to pressure-test executability in ≤40 words.
    """
    return (
        "You are the Mediator. In ≤40 words, ask ONE incisive question that "
        "pressure-tests the plan's executability (files exist? commands idempotent? "
        "paths valid on macOS?). End with a single question mark.\n\n"
        "Context:\n" + plan[:4000]
    )


# ========== Main control loop ==========
def main() -> None:
    prev_topic: str = ""
    for it in range(1, ITERATIONS + 1):
        log_info(f"=== Iteration {it}/{ITERATIONS} ===")

        # 1) Questioner proposes a topic
        q_prompt = prompt_questioner(prev_topic)
        topic_out = ollama_run(AI_QUESTIONER, q_prompt, timeout=TIMEOUT_SECS).strip()
        topic = topic_out.splitlines()[0].strip() if topic_out else ""
        if topic.startswith('"') and topic.endswith('"') and len(topic) >= 2:
            topic = topic[1:-1].strip()
        if not topic:
            log_warn("Questioner returned empty topic; keeping previous.")
            topic = prev_topic or "Find the right prompt to bootstrap robust local builds."
        log_model(f"Topic: {topic}")
        prev_topic = topic

        # 2) Creator produces build plan (JSON only)
        c_prompt = prompt_creator(topic)
        log_info(f"[creator] starting generation… (countdown until first token: {CREATOR_THINK_SECS}s)")
        raw = ollama_run(AI_CREATOR, c_prompt, timeout=TIMEOUT_SECS, think_secs=CREATOR_THINK_SECS, options=CREATOR_OLLAMA_OPTS)
        files, cmds = parse_creator_json(raw)

        # One forced retry if plan is empty or only “Thinking…”
        if (not files and not cmds) or raw.strip().lower().startswith("thinking"):
            log_warn("Creator produced no actionable JSON; retrying with explicit minimal example.")
            c_prompt_retry = c_prompt + "\nRepeat the example JSON above, adjusted for the topic."
            raw = ollama_run(AI_CREATOR, c_prompt_retry, timeout=TIMEOUT_SECS, think_secs=CREATOR_THINK_SECS, options=CREATOR_OLLAMA_OPTS)
            files, cmds = parse_creator_json(raw)

        # Final fallback if still nothing
        if not files and not cmds:
            log_warn("Creator still empty; applying local JSON fallback plan.")
            files, cmds = fallback_json_plan(topic)

        # 4) Apply files
        wrote_any = False
        for f in files:
            pth = f.get("path", "")
            if not pth:
                continue
            content = f.get("content", "")
            if content.strip() == "":
                content = f"# Stub created by builders for {pth}\n"
            wrote_any = safe_write_file(ROOT, pth, content) or wrote_any
        if not wrote_any:
            log_warn("No files written this iteration. (Plan may be incomplete.)")

        # 5) Run commands
        if not cmds:
            log_warn("No commands to execute in plan.")
        for cmd in cmds:
            run_shell(cmd, timeout=300)

        # 6) Mediator every N
        if it % MEDIATOR_EVERY == 0:
            m_prompt = prompt_mediator(plan)
            _ = ollama_run(AI_MEDIATOR, m_prompt, timeout=TIMEOUT_SECS)

        # 7) Disk usage report
        ok, out, _err, _rc = run_shell(f"du -sh {shlex.quote(LOCAL_META_DIR)} || true", timeout=60)
        if ok and out:
            last = out.strip().splitlines()[-1]
            log_info(f"[disk] {last}")

        # small pacing to avoid hot loop if everything is instant
        time.sleep(0.5)

    log_ok("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_warn("Interrupted by user.")
        sys.exit(130)
