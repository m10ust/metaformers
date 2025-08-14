#!/usr/bin/env python3
import subprocess
import os
import datetime
import re
import sys

# --- Config ---
OLLAMA_BIN = os.environ.get("OLLAMA_BIN", "/usr/local/bin/ollama")
MODEL_QUESTIONER = os.environ.get("QUESTIONER_MODEL", "llama2-uncensored:latest")
MODEL_CREATOR = os.environ.get("CREATOR_MODEL", "gpt-oss:latest")

# Blacklist of dangerous commands/patterns (everything else permitted)
DENY_PATTERNS = [
    r"\bsudo\b",
    r"\brm\s+-rf\s+/\b",
    r"\brm\s+-rf\s+\.\.(?:/|\s|$)",
    r"\brm\s+-rf\s+~\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bdiskutil\b",
    r"\blaunchctl\b",
    r"\bcsrutil\b",
    r"\bkillall?\b\s+(?:WindowServer|kernel_task|launchd)",
    r"\bdd\s+if=.*\s+of=/dev/(?:disk|rdisk)\d+",
    r"\bmkfs\b|\bnewfs\b",
    r"\bmount\b|\bumount\b",
    r"\bscutil\b",
    r"\bpfctl\b",
    r"\biptables\b|\bipfw\b",
    r"\bchmod\b\s+.+\s+/(?:\s|$)",
    r"\bchown\b\s+.+\s+/(?:\s|$)",
    r">\s*/(?:etc|bin|sbin|usr|System|Library)/",
]
def is_command_allowed(cmd: str) -> bool:
    c = cmd.strip()
    if not c or c.startswith("#"):
        return False  # treat as non-executable/comment
    # Stop obvious code-fence or markdown noise
    if c.startswith("```") or c.startswith("###"):
        return False
    for pat in DENY_PATTERNS:
        if re.search(pat, c):
            return False
    return True

ROOT = os.getcwd()
RUNS_DIR = os.path.join(ROOT, "runs")
LOCAL_META = os.path.join(ROOT, "local-meta")
os.makedirs(RUNS_DIR, exist_ok=True)
os.makedirs(LOCAL_META, exist_ok=True)

RUN_ID = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
RUN_DIR = os.path.join(RUNS_DIR, RUN_ID)
LOG_DIR = os.path.join(RUN_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

Q_LOG = os.path.join(LOG_DIR, "questioner.log")
C_LOG = os.path.join(LOG_DIR, "creator.log")
ACTION_LOG = os.path.join(LOG_DIR, "actions.log")

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}")
    with open(ACTION_LOG, "a") as f:
        f.write(f"[{ts}] {msg}\n")

def ollama_run(model, prompt):
    try:
        result = subprocess.run(
            [OLLAMA_BIN, "run", model],
            input=prompt.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300
        )
        if result.returncode != 0:
            err = result.stderr.decode()
            log(f"Error from {model}: {err}")
            raise RuntimeError(f"Ollama error: {err}")
        return result.stdout.decode()
    except Exception as e:
        log(f"Ollama run failed: {e}")
        return ""

def safe_write_file(path, content):
    abs_path = os.path.abspath(path)
    allowed_prefix = os.path.abspath(LOCAL_META)
    if not abs_path.startswith(allowed_prefix):
        log(f"Refusing to write outside {LOCAL_META}: {path}")
        return False
    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        log(f"Wrote file: {abs_path}")
        return True
    except Exception as e:
        log(f"Failed to write {abs_path}: {e}")
        return False

def safe_exec(cmd):
    if not is_command_allowed(cmd):
        log(f"Skipping disallowed or non-executable line: {cmd}")
        return False
    try:
        result = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=180)
        log(f"Ran command: {cmd}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result.returncode == 0
    except Exception as e:
        log(f"Command failed: {cmd}\nError: {e}")
        return False

# --- Iterative Topic-Plan Cycle (20 iterations) ---
NUM_ITER = 20
topic_history = []
plan_history = []

for i in range(1, NUM_ITER + 1):
    log(f"=== Iteration {i}/{NUM_ITER} ===")

    # Step 1: Questioner proposes topic
    if i == 1:
        PROMPT_TOPIC = (
            "Propose a single, concrete topic for building or improving a local, "
            "self-improving LLM stack on one Mac mini (no cloud). "
            "Return ONLY a short title (≤12 words)."
        )
    else:
        # Provide previous plan to questioner to propose improved topic
        prev_plan = plan_history[-1] if plan_history else ""
        PROMPT_TOPIC = (
            "Given the previous plan below, propose a slightly improved or next-step topic "
            "to advance toward building a fully functional, local, self-improving LLM stack on one Mac mini using Ollama and Python. "
            "Return ONLY a short title (≤12 words).\n\n"
            "Previous Plan:\n"
            f"{prev_plan}"
        )
    topic_resp = ollama_run(MODEL_QUESTIONER, PROMPT_TOPIC)
    topic = topic_resp.splitlines()[0].strip() if topic_resp else "Local LLM Stack Enhancement"
    topic_history.append(topic)
    with open(Q_LOG, "a") as f:
        f.write(f"=== Iteration {i} ===\nPrompt:\n{PROMPT_TOPIC}\nResponse:\n{topic_resp}\n")
    log(f"Topic: {topic}")

    # Step 2: Creator produces plan given current topic
    PROMPT_PLAN = f"""You are the Creator AI. Given the topic below, output a precise, executable local plan.
macOS constraints: BSD awk/sed; write files ONLY under ./local-meta; shell is zsh.

Format EXACTLY:

### Topic
{topic}

### Files
For each file to write/update, use fenced blocks:
```file ./local-meta/relative/path.ext
<entire file content>
```

### Commands
List each shell command to run as a separate line (for zsh, macOS). Use standard tools freely (git, curl, pip, python, make, jq, awk, sed, etc.). Do NOT include destructive actions (sudo, diskutil, rm -rf /, reboot). Put only commands here (no prose). Stop the list with a blank line.
"""
    plan_resp = ollama_run(MODEL_CREATOR, PROMPT_PLAN)
    if not plan_resp:
        log(f"No plan received from creator model in iteration {i}. Exiting.")
        break
    plan_history.append(plan_resp)
    with open(C_LOG, "a") as f:
        f.write(f"=== Iteration {i} ===\nPrompt:\n{PROMPT_PLAN}\nResponse:\n{plan_resp}\n")

    # Step 3: Parse and write files
    file_blocks = []
    file_pattern = re.compile(r"```file\s+([^\s]+)\n(.*?)```", re.DOTALL)
    for match in file_pattern.finditer(plan_resp):
        rel_path, content = match.group(1).strip(), match.group(2)
        abs_path = os.path.abspath(os.path.join(ROOT, rel_path))
        if safe_write_file(abs_path, content):
            file_blocks.append((abs_path, content))
    if not file_blocks:
        log(f"No files written in iteration {i}. Plan may be incomplete.")

    # Step 4: Parse and execute commands
    cmds = []
    commands_section = re.search(r"### Commands\n(.*)", plan_resp, re.DOTALL)
    if commands_section:
        lines = commands_section.group(1).splitlines()
        for line in lines:
            s = line.strip()
            if not s:
                break  # stop at first blank line after commands
            if s.startswith("###") or s.startswith("```"):
                break
            if s.startswith("#"):
                continue
            cmds.append(s)
    ran_any = False
    for cmd in cmds:
        if safe_exec(cmd):
            ran_any = True
    if not ran_any:
        log(f"No allowed commands executed in iteration {i}.")

# --- End of script ---
log("Run complete.")
log("Summary of all topics proposed during the run:")
for idx, t in enumerate(topic_history, 1):
    log(f"Iteration {idx}: {t}")
