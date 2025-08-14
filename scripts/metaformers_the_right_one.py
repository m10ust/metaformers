#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Moai Council — Right Question Finder (lightweight)
- Local-only, uses Ollama CLI via subprocess.
- Iterative loop: Questioner -> Creator -> (optional) Mediator -> Judge.
- Goal: converge on a single “Right Question” prompt per iteration.
- Outputs: ./moai_runs/<timestamp>/{log.jsonl, top_questions.txt, last_prompt.txt}

Models assumed (adjust names if your tags differ):
  - Questioner:  llama2-uncensored:latest
  - Creator:     gpt-oss:20b
  - Mediator:    dolphin3:latest
  - Judge:       llama2-uncensored:latest
"""

import json, os, sys, time, subprocess, datetime, textwrap, shutil
from typing import Tuple, Optional

# ------------------------- Config -------------------------
QUESTIONER = os.environ.get("MOAI_QUESTIONER", "llama2-uncensored:latest")
CREATOR    = os.environ.get("MOAI_CREATOR",    "gpt-oss:20b")
MEDIATOR   = os.environ.get("MOAI_MEDIATOR",   "dolphin3:latest")
JUDGE      = os.environ.get("MOAI_JUDGE",      "llama2-uncensored:latest")

OLLAMA_BIN = os.environ.get("OLLAMA_BIN", "/usr/local/bin/ollama")

ITERATIONS       = int(os.environ.get("MOAI_ITER", "6"))
MEDIATOR_EVERY   = int(os.environ.get("MOAI_MEDIATOR_EVERY", "3"))
TIMEOUT_SECONDS  = int(os.environ.get("MOAI_TIMEOUT", "120"))  # per model call
MAX_TOKENS_PRINT = 4000  # safe guard for logs

SEED_PROMPT = (
    "You are three AIs whose only mission is to discover THE RIGHT QUESTION — "
    "the most potent prompt that reliably unlocks metacognition, self-checking, "
    "and concrete improvement in a recursive LLM stack. You must generate "
    "prompts that are:\n"
    "1) Testable (has a measurable success criterion),\n"
    "2) Actionable (can be attempted by a local LLM using only bash/python),\n"
    "3) Safe (no exfiltration, no privileged ops),\n"
    "4) Minimal (one request, crisp wording, minimal boilerplate).\n"
    "Return a small shortlist of candidate 'right questions' only."
)

# ------------------------- Helpers -------------------------
def now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def ensure_ollama() -> None:
    if not shutil.which(OLLAMA_BIN):
        print(f"[{now_iso()}] [fatal] ollama not found at: {OLLAMA_BIN}", file=sys.stderr)
        sys.exit(3)

def ollama_run(model: str, prompt: str, timeout: int = TIMEOUT_SECONDS) -> str:
    """Run `ollama run <model>` with prompt on stdin; return stdout text."""
    try:
        proc = subprocess.run(
            [OLLAMA_BIN, "run", model],
            input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            return f"[model_error rc={proc.returncode}] {err.strip() or out.strip()}"
        return out
    except subprocess.TimeoutExpired:
        return "[model_error timeout]"

def clip(s: str, n: int = MAX_TOKENS_PRINT) -> str:
    return s if len(s) <= n else s[:n] + "\n[...truncated...]"

def plog(fh, event: str, **kv):
    rec = {"ts": now_iso(), "event": event, **kv}
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fh.flush()

# ------------------------- Prompt Templates -------------------------
def prompt_questioner(seed: str) -> str:
    return textwrap.dedent(f"""
    You are the Questioner. Starting from the goal below, propose 5 candidate RIGHT QUESTIONS (one line each).

    Goal:
    {seed}

    Rules for each question:
    - Make it falsifiable and testable locally (bash/python only).
    - It must elicit metacognition: self-checks, uncertainty, calibration, or ablation.
    - No multi-part instructions; one crisp request.
    - Avoid boilerplate, keep ≤ 30 words.

    Return only the 5 questions as a numbered list (1–5), nothing else.
    """).strip()

def prompt_creator(list_5: str) -> str:
    return textwrap.dedent(f"""
    You are the Creator. You will refine the Questioner’s 5 candidates and pick the top 3.

    Candidates:
    {list_5}

    For each of your TOP-3, output:
    - Q: <the single-line question, ≤30 words>
    - Rationale: (≤25 words)
    - Micro-test: (a tiny local test description with a measurable success criterion)

    Return EXACTLY 3 blocks in the above format, separated by blank lines. No extra commentary.
    """).strip()

def prompt_mediator(creator_blocks: str) -> str:
    return textwrap.dedent(f"""
    You are the Mediator. Challenge assumptions and improve wording.

    Review:
    {creator_blocks}

    Produce ONE meta-observation (≤40 words) and ONE revised single-line question (≤30 words) that sharpens testability and safety. Format:

    Meta: <one sentence>
    Revised: <question line>
    """).strip()

def prompt_judge(questions_block: str) -> str:
    return textwrap.dedent(f"""
    You are the Judge. Score each question (or revised one) for:
    - clarity (0–10)
    - testability (0–10)
    - novelty (0–10)
    Return strict JSON array of objects:
    [{{"q":"...", "clarity":int, "testability":int, "novelty":int, "total":int}}, ...]
    Text to score:
    {questions_block}
    """).strip()

# ------------------------- Parsing -------------------------
def extract_numbered(lines: str) -> list:
    out = []
    for ln in lines.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # match "1. ..." or "1) ..."
        if len(ln) > 2 and (ln[0].isdigit() and ln[1] in [')', '.']) :
            # strip "1. " or "1) "
            out.append(ln[2:].strip(" -"))
        else:
            # also accept bare lines if exactly 5 lines were returned
            out.append(ln)
    # keep at most 5 distinct non-empty lines
    dedup = []
    for q in out:
        if q and q not in dedup:
            dedup.append(q)
        if len(dedup) >= 5:
            break
    return dedup

def parse_creator_blocks(txt: str) -> list:
    """Return list of dicts: [{'q':..., 'rationale':..., 'micro':...}, ...]"""
    blocks = [b.strip() for b in txt.strip().split("\n\n") if b.strip()]
    out = []
    for b in blocks:
        q = r = m = ""
        for ln in b.splitlines():
            s = ln.strip()
            if s.lower().startswith("q:"):
                q = s[2:].strip()
            elif s.lower().startswith("rationale:"):
                r = s[len("rationale:"):].strip()
            elif s.lower().startswith("micro-test:"):
                m = s[len("micro-test:"):].strip()
        if q:
            out.append({"q": q, "rationale": r, "micro": m})
    return out[:3]

def parse_mediator(txt: str) -> Tuple[Optional[str], Optional[str]]:
    meta, rev = None, None
    for ln in txt.splitlines():
        s = ln.strip()
        if s.lower().startswith("meta:"):
            meta = s[5:].strip()
        elif s.lower().startswith("revised:"):
            rev = s[8:].strip()
    return meta, rev

def parse_judge_json(txt: str) -> list:
    try:
        start = txt.find("[")
        end = txt.rfind("]")
        if start == -1 or end == -1:
            return []
        return json.loads(txt[start:end+1])
    except Exception:
        return []

# ------------------------- Main Loop -------------------------
def main():
    ensure_ollama()

    run_id = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    root = os.getcwd()
    outdir = os.path.join(root, "moai_runs", run_id)
    os.makedirs(outdir, exist_ok=True)

    log_path = os.path.join(outdir, "log.jsonl")
    best_path = os.path.join(outdir, "top_questions.txt")
    last_prompt_path = os.path.join(outdir, "last_prompt.txt")

    with open(log_path, "a", encoding="utf-8") as log, open(best_path, "a", encoding="utf-8") as best:
        current_seed = SEED_PROMPT
        plog(log, "start", run_id=run_id, questioner=QUESTIONER, creator=CREATOR, mediator=MEDIATOR, judge=JUDGE)

        for it in range(1, ITERATIONS+1):
            plog(log, "iteration_begin", i=it)

            # 1) Questioner proposes 5 candidates
            q_prompt = prompt_questioner(current_seed)
            q_out = ollama_run(QUESTIONER, q_prompt)
            plog(log, "questioner_out", i=it, out=clip(q_out))

            candidates = extract_numbered(q_out)
            if not candidates:
                plog(log, "warn", i=it, msg="No candidates extracted; carrying seed forward.")
                candidates = ["What single change would most reliably increase self-correction in a local LLM with a measurable test?"]

            # 2) Creator refines to top-3 with micro-tests
            c_prompt = prompt_creator("\n".join(f"{i+1}. {c}" for i, c in enumerate(candidates)))
            c_out = ollama_run(CREATOR, c_prompt)
            plog(log, "creator_out", i=it, out=clip(c_out))

            top3 = parse_creator_blocks(c_out)
            if not top3:
                # fall back: keep first three raw candidates
                top3 = [{"q": c, "rationale": "", "micro": ""} for c in candidates[:3]]

            # 3) (Every N) Mediator proposes a single revision
            revised_q = None
            if it % MEDIATOR_EVERY == 0:
                block_str = "\n\n".join([f"Q: {b['q']}\nRationale: {b['rationale']}\nMicro-test: {b['micro']}" for b in top3])
                m_prompt = prompt_mediator(block_str)
                m_out = ollama_run(MEDIATOR, m_prompt)
                plog(log, "mediator_out", i=it, out=clip(m_out))
                meta, revised = parse_mediator(m_out)
                if revised:
                    revised_q = revised

            # 4) Judge — score all (3 + revised if present)
            scoring_block = "\n".join([b["q"] for b in top3])
            if revised_q:
                scoring_block += "\n" + revised_q

            j_prompt = prompt_judge(scoring_block)
            j_out = ollama_run(JUDGE, j_prompt)
            plog(log, "judge_raw", i=it, out=clip(j_out))

            scores = parse_judge_json(j_out)
            # pick best by total; fallback to first
            best_q = top3[0]["q"]
            best_total = -1
            for sc in scores:
                t = int(sc.get("total", 0))
                q = sc.get("q") or sc.get("question") or ""
                if t > best_total and q:
                    best_total = t
                    best_q = q

            # append to best file
            best.write(f"[{now_iso()}] Iter {it}: {best_q}\n")
            best.flush()
            plog(log, "iteration_choice", i=it, best_question=best_q, judged_total=best_total)

            # update seed = best question to “ask better next time”
            current_seed = (
                "Using this as the current best RIGHT QUESTION, propose 5 improved variants that increase testability, "
                "novelty, and clarity while staying ≤30 words each. Keep them strictly single-line.\n\n"
                f"Current best: {best_q}"
            )
            with open(last_prompt_path, "w", encoding="utf-8") as f:
                f.write(current_seed)

        plog(log, "done", outdir=outdir)
        print(f"[{now_iso()}] Done. Results in: {outdir}")
        print(f"  - Log: {log_path}")
        print(f"  - Top questions: {best_path}")
        print(f"  - Last seed prompt: {last_prompt_path}")

if __name__ == "__main__":
    main()