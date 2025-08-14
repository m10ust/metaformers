#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Metaformers — Choose Your Prompt (v2, with iterative memory)
- Prompts for topic, iterations, mediator cadence
- Four key improvements:
  1) Questioner only typo-fixes your topic, no rephrasing
  2) Creator receives a rolling "Context Memory" from prior turns
  3) Mediator’s question constrains (but does not replace) the topic
  4) Clean logs: strips ANSI/spinners
"""

import os, re, sys, json, signal, pathlib, datetime, subprocess
from typing import Optional, List

# --------------------------- Config ---------------------------

QUESTIONER = os.environ.get("MF_QUESTIONER", "llama2-uncensored:latest")
CREATOR    = os.environ.get("MF_CREATOR",    "gpt-oss:20b")
MEDIATOR   = os.environ.get("MF_MEDIATOR",   "dolphin3:latest")
OLLAMA_BIN = os.environ.get("OLLAMA_BIN", "/usr/local/bin/ollama")

MEDIATOR_EVERY_DEFAULT = int(os.environ.get("MF_MEDIATOR_EVERY", "4"))
MEMORY_WINDOW_TURNS    = int(os.environ.get("MF_MEMORY_WINDOW", "3"))  # NEW: how many past turns to remember

ROOT = pathlib.Path.cwd()
RUNS_DIR = ROOT / "runs"

# ---------------------- Timestamps ----------------------------

def ts_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def ts_compact() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")

# ---------------------- Cleanup -------------------------------

ANSI_RE = re.compile(r"(?:\x1B\[[0-9;?]*[ -/]*[@-~])|(?:\x1B[@-Z\\-_])|(?:\x1B\][^\x07]*\x07)")
SPINNER_RE = re.compile(r"[\u2800-\u28FF◐◓◑◒⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+")
LABEL_RE = re.compile(r'^\s*(?:>{1,3}\s*)?(?:topic|corrected\s*topic|final\s*topic|revised\s*topic|clean(?:ed)?\s*topic|prompt)\s*[:\-]\s*', re.I)
QUOTE_CHARS = '"\'`“”‘’'

def clean_text(s: str) -> str:
    if not s: return s
    s = ANSI_RE.sub("", s)
    s = SPINNER_RE.sub("", s)
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def strip_leading_labels(s: str) -> str:
    out = s or ""
    for _ in range(2):
        out = re.sub(LABEL_RE, "", out)
    return out.strip()

def strip_wrapping_quotes(s: str) -> str:
    out = (s or "").strip()
    while len(out) >= 2 and out[0] in QUOTE_CHARS and out[-1] in QUOTE_CHARS:
        out = out[1:-1].strip()
    while out and out[0] in QUOTE_CHARS: out = out[1:].strip()
    while out and out[-1] in QUOTE_CHARS: out = out[:-1].strip()
    return out

def normalize_topic(s: str) -> str:
    s = clean_text(s or "")
    s = strip_leading_labels(s)
    s = strip_wrapping_quotes(s)
    return s.strip()

def extract_marked(s: str) -> str:
    if not s: return ""
    m = re.search(r"<<<BEGIN>>>\s*(.*?)\s*<<<END>>>", s, flags=re.S)
    return normalize_topic(m.group(1)) if m else normalize_topic(s)

# Helper to strip planning/thinking blocks from transcript output
def strip_thinking_blocks(s: str) -> str:
    """
    Remove model planning chatter from transcript-only views.
    Strips everything between a 'Thinking...' marker and the matching '...done thinking'
    (case-insensitive, tolerant of punctuation/ellipsis and spacing). Applied ONLY to transcript output.
    """
    if not s:
        return s
    # handle ASCII '...' and Unicode '…', be forgiving with whitespace/punctuation
    pattern = re.compile(r"(?is)thinking[\.\…]*.*?[\.\…]*\s*done\s*thinking")
    return pattern.sub("", s).strip()

# ------------------------- Logging ----------------------------

class TeeLogger:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
    def write(self, line: str, also_stdout: bool = False):
        stamp = f"[{ts_iso()}] "
        text = stamp + line.rstrip("\n")
        self._fh.write(text + "\n"); self._fh.flush()
        if also_stdout: print(text)
    def close(self):
        try: self._fh.close()
        except Exception: pass

# ---------------------- Ollama -------------------------------

def check_ollama_or_die():
    if not pathlib.Path(OLLAMA_BIN).exists():
        print(f"[{ts_iso()}] [fatal] Ollama binary not found at: {OLLAMA_BIN}", file=sys.stderr)
        sys.exit(2)

def run_ollama(model: str, prompt: str, log: TeeLogger, show_prefix: str) -> str:
    cmd = [OLLAMA_BIN, "run", model]
    env = os.environ.copy(); env.setdefault("TERM", "dumb"); env.setdefault("NO_COLOR", "1")

    log.write("PROMPT:\n" + prompt)
    print(f"[{ts_iso()}] [{model}] <<<", flush=True)

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        assert proc.stdin is not None
        proc.stdin.write(prompt); proc.stdin.flush(); proc.stdin.close()
    except BrokenPipeError:
        pass

    raw_out = []
    while True:
        if proc.stdout is None: break
        line = proc.stdout.readline()
        if not line: break
        raw_out.append(line)
        cleaned = clean_text(line)
        if cleaned: print(cleaned)

    stderr = proc.stderr.read() if proc.stderr else ""
    ret = proc.wait()

    out_clean = clean_text("".join(raw_out)).strip()
    err_clean = clean_text(stderr).strip()
    if err_clean: log.write(f"[STDERR] {err_clean}", also_stdout=True)
    if ret != 0:  log.write(f"[warn] ollama exited with {ret}", also_stdout=True)

    log.write("OUTPUT:\n" + out_clean)
    return out_clean

# ---------------------- Prompt builders ----------------------

def build_questioner_prompt(user_topic: str) -> str:
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

def build_creator_prompt(topic: str, mediator_q: Optional[str], context_memory: str) -> str:
    """
    NEW: include Context Memory and require a 'Decisions & Diffs' line so each turn builds.
    """
    pre = "You are the Creator.\nGiven the Questioner’s topic, produce a concrete, actionable mini‑plan in this EXACT format:\n\n"
    ctx = ""
    if context_memory:
        ctx = (
            "Context Memory (from previous turns):\n"
            f"{context_memory}\n\n"
            "You MUST build on this context (refine, extend, resolve open items), not restart from scratch.\n"
        )
    if mediator_q:
        ctx += (
            "The Mediator previously asked this meta‑question — you MUST address it explicitly:\n"
            f"» {mediator_q}\n\n"
            "Include a single line at the top of your response:\n"
            "Mediator Answer: <one concise sentence answering the meta‑question>\n\n"
        )
    return (
        pre + ctx +
        "At the TOP, include:\n"
        "Decisions & Diffs: <one concise line describing what changed vs. last turn (new decisions, changes, TODOs)>\n\n"
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

# ---------------------- Memory helpers -----------------------

def compress_for_memory(creator_text: str) -> str:
    """
    Heuristic compression: keep the top lines that carry forward state.
    - First line that starts with 'Decisions & Diffs:' (if present)
    - Up to 2 lines from Conceptual Insight
    - First 2 numbered steps from Practical Mechanism
    - If available, a 'Mediator Answer:' line
    Output capped to ~140–180 words.
    """
    text = clean_text(creator_text or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep: List[str] = []
    # mediator answer / decisions
    for ln in lines:
        if ln.lower().startswith("mediator answer:"):
            keep.append(ln); break
    for ln in lines:
        if ln.lower().startswith("decisions & diffs:"):
            keep.append(ln); break
    # conceptual insight (2)
    ci = []; in_ci = False
    for ln in lines:
        if ln.lower().startswith("## conceptual insight"): in_ci = True; continue
        if in_ci and ln.lower().startswith("## "): break
        if in_ci and len(ci) < 2 and not ln.lower().startswith("mediator answer"):
            ci.append(ln)
    keep += ci
    # first 2 steps
    steps = [ln for ln in lines if re.match(r"^\s*\d+\.\s", ln)]
    keep += steps[:2]
    snippet = " ".join(keep)
    # rough cap
    words = snippet.split()
    if len(words) > 180:
        snippet = " ".join(words[:180]) + " …"
    return snippet

def render_memory_block(memory_notes: List[str]) -> str:
    if not memory_notes: return ""
    numbered = [f"{i+1}. {m}" for i, m in enumerate(memory_notes[-MEMORY_WINDOW_TURNS:])]
    return " • ".join(numbered)

def enforce_topic(original: str, candidate: str) -> str:
    orig = original.strip(); cand = (candidate or "").strip()
    if not cand: return orig
    if len(cand) < 0.7 * len(orig): return orig
    tok = re.compile(r"[A-Za-z0-9_]+")
    o = [t.lower() for t in tok.findall(orig)]
    c = [t.lower() for t in tok.findall(cand)]
    o4 = {t for t in o if len(t) >= 4}
    c4 = {t for t in c if len(t) >= 4}
    if not o4: return cand
    overlap = len(o4 & c4) / max(1, len(o4))
    return cand if overlap >= 0.6 else orig

# --------------------------- Main -----------------------------

def main():
    check_ollama_or_die()

    print(f"[{ts_iso()}] Meta Discussion — three local models, iterative memory.")
    user_topic = input("First prompt (what should they discuss?): ").strip()
    if not user_topic:
        print("No topic provided. Exiting."); sys.exit(1)

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

    established_topic = user_topic
    last_mediator_q: Optional[str] = None
    memory_notes: List[str] = []   # NEW: rolling memory

    for turn in range(1, iterations + 1):
        master.write(f"=== Turn {turn}/{iterations} ===", also_stdout=True)

        # 1) QUESTIONER — fix typos (turn 1 locks the canonical topic)
        q_prompt = build_questioner_prompt(established_topic)
        qlog.write("PROMPT:\n" + q_prompt)
        q_out_raw = run_ollama(QUESTIONER, q_prompt, qlog, f"[{QUESTIONER}]")
        q_out = enforce_topic(established_topic, extract_marked(q_out_raw) or established_topic)
        q_out = normalize_topic(q_out)
        if turn == 1:
            established_topic = q_out
        tlog.write(f"[{ts_iso()}] QUESTIONER:\n{q_out}\n")
        master.write(f"Topic: {established_topic}", also_stdout=True)

        # 2) CREATOR — gets Context Memory
        context_block = render_memory_block(memory_notes)
        c_prompt = build_creator_prompt(established_topic, mediator_q=last_mediator_q, context_memory=context_block)
        clog.write("PROMPT:\n" + c_prompt)
        c_out = run_ollama(CREATOR, c_prompt, clog, f"[{CREATOR}]") or "(no output)"
        c_out_transcript = strip_thinking_blocks(c_out)
        tlog.write(f"[{ts_iso()}] CREATOR:\n{c_out_transcript}\n")

        # Update rolling memory with compressed state from this Creator output
        mem = compress_for_memory(c_out)
        if mem:
            memory_notes.append(mem)
            # cap window
            if len(memory_notes) > MEMORY_WINDOW_TURNS:
                memory_notes = memory_notes[-MEMORY_WINDOW_TURNS:]

        # 3) MEDIATOR every N turns — constrain next Creator (do not replace topic)
        new_mediator_q: Optional[str] = None
        if mediator_every > 0 and (turn % mediator_every == 0):
            m_prompt = build_mediator_prompt(c_out)
            mlog.write("PROMPT:\n" + m_prompt)
            m_out = run_ollama(MEDIATOR, m_prompt, mlog, f"[{MEDIATOR}]")
            new_mediator_q = normalize_topic(m_out or "")
            if not new_mediator_q.endswith("?"):
                new_mediator_q = (new_mediator_q.rstrip(". ") + "?") if new_mediator_q else \
                                 "What implicit assumption, if false, would invalidate this plan?"
            tlog.write(f"[{ts_iso()}] MEDIATOR:\n{strip_thinking_blocks(new_mediator_q)}\n")
            master.write(f"[note] Mediator constraint queued for next turn: {new_mediator_q}", also_stdout=True)

        # Next turn must answer new mediator question (if any)
        last_mediator_q = new_mediator_q or None
        print()

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
