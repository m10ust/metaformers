#!/usr/bin/env python3
# metafomers_builders_python_v2.py — builders mode with blacklist + 20-step curriculum
# macOS-friendly (BSD awk/sed assumptions), broad command freedom with explicit blacklist.
# Creator: gpt-oss via Ollama. Questioner: llama2-uncensored (titles). 20 iterations.

import os, re, sys, json, shlex, datetime, subprocess, textwrap, time
from typing import Optional, List, Tuple
from pathlib import Path

# ---------- Config ----------
ROOT        = Path(os.getcwd())
RUNS_DIR    = ROOT / "runs"
LOCAL_META  = ROOT / "local-meta"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_META.mkdir(parents=True, exist_ok=True)

RUN_ID   = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
RUN_DIR  = RUNS_DIR / RUN_ID
LOG_DIR  = RUN_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

Q_LOG       = LOG_DIR / "questioner.log"
C_LOG       = LOG_DIR / "creator.log"
ACTION_LOG  = LOG_DIR / "actions.log"

OLLAMA_BIN       = os.environ.get("OLLAMA_BIN", "ollama")
MODEL_QUESTIONER = os.environ.get("QUESTIONER_MODEL", "llama2-uncensored:latest")
MODEL_CREATOR    = os.environ.get("CREATOR_MODEL", "gpt-oss:latest")

ITERATIONS = 20
NETWORK_ALLOWED = True  # allow pip/git/curl by default
VERBOSE = os.environ.get("VERBOSE", "1") == "1"  # default: echo to terminal

# ---------- Blacklist & safety ----------
BLACKLIST_SUBSTRINGS = [
    " sudo ", "sudo ", ";sudo", "&&sudo", "|sudo",
    " rm -rf /", " rm -fr /", " rm -rf /*", " rm -fr /*",
    " /System", " /Library", " /bin/", " /sbin/", " /usr/bin/", " /usr/sbin/",
    " chflags ", " csrutil ", " nvram ", " kext", " spctl ",
    " launchctl ", " killall ", " pkill ", " kill -9 ",
    " diskutil ", " tmutil ", " asr ", " bless ",
    " ifconfig ", " networksetup ", " route ", " ipfw ", " pfctl ",
    " scutil ", " defaults write /Library", " defaults delete /Library",
    "> /etc", ">> /etc", " /etc/", " /private/etc/",
    " rm -rf ~/.", " rm -fr ~/."  # overbroad wipes
]
# For risky ops, restrict to LOCAL_META only
RISKY_BASENAMES = {"rm", "cp", "mv", "ln", "chmod", "chown", "truncate", "dd", "tar"}

CURRICULUM = [
    "Create repo skeleton under ./local-meta (README, .gitignore, env notes).",
    "Bootstrap Python venv under ./local-meta/venv and install core deps (numpy, psutil, pandas).",
    "Install LLM tooling: ensure ollama CLI works; create small eval fixtures JSONL.",
    "Pull a small open-source model via Ollama; write a test query script.",
    "Add prompt templating utility and base templates.",
    "Implement local dataset loader + sampler; store tiny fixtures.",
    "Add latency/ECE micro-eval harness; write metrics JSON.",
    "Introduce a caching layer for prompts/responses (sqlite/json).",
    "Implement a simple router to pick templates.",
    "Add calibration: temperature/top-p sweep script.",
    "Create a CLI runner to execute N prompts and save results.",
    "Add structured logging (JSONL) and per-run folders.",
    "Stub a finetune/adapter hook (placeholder).",
    "Write minimal unit tests (pytest).",
    "Add leaderboard builder; compare runs.",
    "Iterate prompt templates (few-shot variations).",
    "Add self-check pass (consistency/entropy flag).",
    "Tighten safety/reporting; print disk usage.",
    "Package small zsh launcher to run pipeline.",
    "Write final summary & next-steps."
]

# ---------- Logging ----------
def ts():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def plog(path: Path, msg: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{ts()}] {msg}\n")

def log(msg: str):
    print(f"[{ts()}] {msg}")
    plog(ACTION_LOG, msg)

# ---------- Ollama ----------
def ollama_run(model: str, prompt: str, timeout=300) -> str:
    try:
        p = subprocess.run(
            [OLLAMA_BIN, "run", model],
            input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout
        )
        if p.returncode != 0:
            err = p.stderr.decode(errors="ignore")
            log(f"[ollama:{model}] ERROR: {err.strip()}")
            return ""
        return p.stdout.decode(errors="ignore")
    except Exception as e:
        log(f"[ollama:{model}] failed: {e}")
        return ""

# ---------- Safety helpers ----------
def inside_local_meta(path: Path) -> bool:
    try:
        return str(path.resolve()).startswith(str(LOCAL_META.resolve()))
    except Exception:
        return False

def safe_write_file(rel_path: str, content: str) -> bool:
    try:
        p = ROOT / rel_path
        if not inside_local_meta(p):
            log(f"[write] REFUSED outside ./local-meta → {rel_path}")
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        log(f"[write] {p}")
        if VERBOSE:
            print(f"[{ts()}] [write] {p}", flush=True)
        return True
    except Exception as e:
        log(f"[write] failed {rel_path}: {e}")
        return False

def blacklisted(cmd: str) -> Optional[str]:
    low = f" {cmd.strip()} ".lower()
    for bad in BLACKLIST_SUBSTRINGS:
        if bad in low:
            return bad.strip()
    return None

def risky_outside_localmeta(cmd: str) -> bool:
    try:
        parts = shlex.split(cmd)
    except Exception:
        return True
    if not parts: 
        return False
    base = Path(parts[0]).name
    if base not in RISKY_BASENAMES:
        return False
    for tok in parts[1:]:
        if tok.startswith("-"):
            continue
        if tok in {"|","&&","||",";"}:
            continue
        if "/" in tok or tok.startswith("./") or tok.startswith("../"):
            p = (ROOT / tok).resolve()
            if not inside_local_meta(p):
                return True
    return False

def safe_exec(cmd: str, timeout=600) -> Tuple[bool, str, str, int]:
    if not cmd.strip():
        return False, "", "empty command", 1
    if not NETWORK_ALLOWED and any(cmd.strip().startswith(x) for x in ("pip","pip3","git","curl","wget")):
        return False, "", "network disabled", 1
    hit = blacklisted(cmd)
    if hit:
        return False, "", f"blocked by blacklist: {hit}", 1
    if risky_outside_localmeta(cmd):
        return False, "", "risky path outside ./local-meta", 1
    try:
        if VERBOSE:
            print(f"[{ts()}] $ {cmd}", flush=True)
        # Stream stdout+stderr live; still capture for logs
        start = time.time()
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        out_lines: List[str] = []
        # Read line by line with timeout handling
        while True:
            if proc.stdout is None:
                break
            line = proc.stdout.readline()
            if line:
                out_lines.append(line)
                if VERBOSE:
                    print(line, end="", flush=True)
            elif proc.poll() is not None:
                break
            # timeout check
            if (time.time() - start) > timeout:
                proc.kill()
                return False, "".join(out_lines), "timeout", 124
        rc = proc.wait()
        out = "".join(out_lines)
        # Separate stderr is not available when streaming merged; log as empty
        err = ""
        return rc == 0, out, err, rc
    except Exception as e:
        return False, "", f"exec error: {e}", 1

# ---------- Parsing the Creator plan ----------
# Accept forms like:
# ```file ./local-meta/x
# ```file: ./local-meta/x
# ```file=./local-meta/x
FILE_BLOCK_RE = re.compile(
    r"```file[^\n\r]*?[ \t:=]+(?P<raw_path>[^\n\r]+)\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE
)
CMD_BLOCK_RE = re.compile(
    r"```(?:shell|bash|sh)\s*\n(?P<cmds>.*?)\n```",
    re.DOTALL | re.IGNORECASE
)
DDL_BLOCK_RE = re.compile(
    r"```sql\s*\n(?P<ddl>.*?)\n```",
    re.DOTALL | re.IGNORECASE
)

VALID_REL = re.compile(r"^\./local-meta/[A-Za-z0-9._/\-]+$")

def _clean_path(s: str) -> Optional[str]:
    s = s.strip().strip('`\"').strip()
    # pull first token that looks like ./local-meta/...
    m = re.search(r"(\./local-meta/[A-Za-z0-9._/\-]+)", s)
    if not m:
        return None
    p = m.group(1)
    if len(p) > 200:
        return None
    if not VALID_REL.match(p):
        return None
    return p

def parse_creator_output(txt: str) -> dict:
    files: List[Tuple[str, str]] = []
    seen = set()

    for m in FILE_BLOCK_RE.finditer(txt):
        raw_path = m.group("raw_path")
        body = m.group("body")
        path = _clean_path(raw_path)
        if not path:
            log(f"[parse] skip invalid file path: {raw_path!r}")
            continue
        body = body.rstrip()
        key = (path, hash(body))
        if key in seen:
            continue
        seen.add(key)
        files.append((path, body))

    cmds: List[str] = []
    for m in CMD_BLOCK_RE.finditer(txt):
        block = m.group("cmds")
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cmds.append(line)

    ddls: List[str] = []
    for m in DDL_BLOCK_RE.finditer(txt):
        block = m.group("ddl").strip()
        if block:
            ddls.append(block)

    return {"files": files, "cmds": cmds, "ddls": ddls}

# ---------- Prompts ----------
def prompt_topic(prev_plan: Optional[str]) -> str:
    if prev_plan:
        return (
            "Propose a short title (≤12 words) for the NEXT concrete sub-step "
            "to build a local LLM stack on one Mac mini (no cloud). Return ONLY the title.\n\n"
            "Previous creator plan for context:\n" + prev_plan
        )
    return ("Propose a short title (≤12 words) for the FIRST concrete sub-step "
            "to build a local LLM stack on one Mac mini (no cloud). Return ONLY the title.")

def prompt_creator(topic: str, step_hint: str) -> str:
    safe_note = (
        "macOS constraints: BSD awk/sed; zsh shell. "
        "You may run typical dev commands (pip, git, curl, python, jq, awk, sed, rg, psql, ollama). "
        "Files MUST be written ONLY under ./local-meta/ . "
        "Use fenced blocks exactly as specified."
    )
    req = f"""
You are the Creator AI. Your sub-goal must align with:
- Topic: {topic}
- Curriculum hint: {step_hint}

{safe_note}

OUTPUT FORMAT (exact):

### Topic
{topic}

### Files
For each file, emit a fenced block:
```file ./local-meta/relative/path.ext
<entire file content>
```

### Commands
Emit ONE fenced shell block with the exact commands to run (one per line):
```shell
<cmd 1>
<cmd 2>
# comments allowed with leading '#'
```

### DDL
If you need Postgres changes, emit ONE fenced sql block; otherwise omit the DDL section:
```sql
CREATE TABLE IF NOT EXISTS local_meta_runs(id SERIAL PRIMARY KEY, created_at TIMESTAMP DEFAULT NOW());
```
"""
    return textwrap.dedent(req).strip()

# ---------- DB helper (optional) ----------
def psql_available() -> bool:
    return shutil.which("psql") is not None if "shutil" in globals() else __import__("shutil").which("psql") is not None

def apply_ddl(ddls: List[str]):
    if not ddls:
        return
    # Try psql via local socket with role meta if present; otherwise skip gracefully
    psql = __import__("shutil").which("psql")
    if not psql:
        log("[psql] Not available; skipping DDL.")
        return
    for ddl in ddls:
        escaped = ddl.replace('"', '""')
        cmd = f'psql -h /tmp -U meta -d metaformers -v ON_ERROR_STOP=1 -c "{escaped}"'
        ok, out, err, rc = safe_exec(cmd, timeout=120)
        if not ok:
            log(f"[psql] DDL failed: {err}")

# ---------- Disk report ----------
def du_local_meta() -> str:
    try:
        p = subprocess.run(["du","-sh",str(LOCAL_META)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if p.returncode == 0:
            return p.stdout.strip()
        return f"du error: {p.stderr.strip()}"
    except Exception as e:
        return f"du exception: {e}"

# ---------- Main loop ----------
def main():
    prev_plan_excerpt = None
    for i in range(1, ITERATIONS+1):
        log(f"=== Iteration {i}/{ITERATIONS} ===")

        # 1) Questioner proposes the sub-step title
        topic_prompt = prompt_topic(prev_plan_excerpt)
        topic = ollama_run(MODEL_QUESTIONER, topic_prompt, timeout=120).strip()
        topic = re.sub(r"\s+", " ", topic).strip().strip('"').strip("'")
        if not topic:
            topic = f"Step {i}: local LLM build task"
        plog(Q_LOG, f"Topic: {topic}")
        if VERBOSE:
            print(f"[{ts()}] [topic] {topic}", flush=True)

        # 2) Creator produces files/commands
        step_hint = CURRICULUM[min(i-1, len(CURRICULUM)-1)]
        creator_prompt = prompt_creator(topic, step_hint)
        creator_out = ollama_run(MODEL_CREATOR, creator_prompt, timeout=420)
        if not creator_out:
            log("Creator returned empty output; continuing.")
            continue
        plog(C_LOG, f"Topic: {topic}\n\n{creator_out}")

        # 3) Parse plan
        plan = parse_creator_output(creator_out)
        files_written = 0
        for rel, body in plan["files"]:
            if safe_write_file(rel, body):
                files_written += 1

        # 4) Apply DDL (optional)
        if plan["ddls"]:
            apply_ddl(plan["ddls"])

        # 5) Execute commands
        cmds_run = 0
        for cmd in plan["cmds"]:
            ok, out, err, rc = safe_exec(cmd)
            plog(ACTION_LOG, f"CMD: {cmd}\nRC: {rc}\nSTDOUT:\n{out}\nSTDERR:\n{err}\n")
            if ok:
                cmds_run += 1

        # 6) Disk usage
        log(f"[disk] {du_local_meta()}")

        # 7) Summary this iter
        if files_written == 0:
            log(f"No files written in iteration {i}. Plan may be incomplete.")
        if cmds_run == 0 and plan["cmds"]:
            log(f"No allowed commands executed in iteration {i} (blocked or failed).")

        # 8) Keep a short excerpt of creator plan for the next title
        prev_plan_excerpt = "\n".join(creator_out.splitlines()[:80])

    log("✅ Build loop complete.")

if __name__ == "__main__":
    main()