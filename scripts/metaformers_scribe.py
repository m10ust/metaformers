#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Metaformers — Scribe (ANSI-clean, chunked summarization, macOS-friendly)

What it does
------------
- Locates the latest run folder (or a specific one via --run).
- Reads questioner/creator/mediator/master logs.
- Strips ANSI + braille spinner glyphs (those ⠙⠹⠸…).
- Builds a compact transcript + extracts actions (writes/commands).
- Summarizes in CHUNKS via a local model (default: dolphin3:latest).
- Writes runs/<RUN_ID>/summary.md and prints a short TL;DR.

Why it avoids SSD blowups
-------------------------
- Streams + chunks the text (default ~6k chars per chunk).
- No giant concatenations; minimal temp strings.
- Hard cap on summary size written (~500 KB by default).

Usage
-----
  ./metaformers_scribe.py                # summarize latest run
  ./metaformers_scribe.py --run runs/20250812-232110
  ./metaformers_scribe.py --model qwen2.5:7b-instruct
"""

from __future__ import annotations

import os, sys, re, json, argparse
from pathlib import Path
from datetime import datetime
import subprocess
import shutil

# ---------- Config ----------
DEFAULT_SUMMARIZER = os.environ.get("SCRIBE_MODEL", "dolphin3:latest")
OLLAMA_BIN = os.environ.get("OLLAMA_BIN", "/usr/local/bin/ollama")

# Chunking + output safety
CHARS_PER_CHUNK = int(os.environ.get("SCRIBE_CHUNK_SIZE", "6000"))
MAX_SUMMARY_BYTES = int(os.environ.get("SCRIBE_MAX_BYTES", str(500 * 1024)))  # ~500 KB

# ---------- ANSI / Spinner cleanup ----------
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07")
_ANSI_C1  = re.compile(r"\x1b[@-Z\\-_]")
_BACKSPACE_OVERSTRIKE = re.compile(r".\x08")
_SPINNER_BLOCK_RE = re.compile(r'(?:[\u2800-\u28FF]\s*){2,}')  # braille runs

def strip_ansi(s: str) -> str:
    s = s.replace("\r", "")
    while True:
        new = _BACKSPACE_OVERSTRIKE.sub("", s)
        if new == s: break
        s = new
    s = _ANSI_OSC.sub("", s)
    s = _ANSI_CSI.sub("", s)
    s = _ANSI_C1.sub("", s)
    return s

def strip_spinners(s: str) -> str:
    s = _SPINNER_BLOCK_RE.sub('', s)
    s = re.sub(r'^(?:[\u2800-\u28FF]\s*)+', '', s, flags=re.M)
    return s

def sanitize(s: str) -> str:
    return strip_spinners(strip_ansi(s))

# ---------- Filesystem helpers ----------
def ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def find_latest_run(base: Path) -> Path | None:
    runs = sorted((p for p in (base / "runs").glob("*") if p.is_dir()),
                  key=lambda p: p.name, reverse=True)
    return runs[0] if runs else None

def pick_logs(run_dir: Path) -> dict[str, Path]:
    logs = run_dir / "logs"
    if not logs.is_dir():
        return {}
    # Accept multiple naming styles from your various scripts
    cand = {}
    # master
    for pat in ("master_*.log", "ai_master_*.log", "master*.log"):
        found = list(logs.glob(pat))
        if found: cand["master"] = sorted(found)[-1]; break
    # questioner
    for pat in ("questioner_*.log", "llama2_questioner_*.log"):
        found = list(logs.glob(pat))
        if found: cand["questioner"] = sorted(found)[-1]; break
    # creator
    for pat in ("creator_*.log", "gpt_oss_creator_*.log"):
        found = list(logs.glob(pat))
        if found: cand["creator"] = sorted(found)[-1]; break
    # mediator
    for pat in ("mediator_*.log", "dolphin3_mediator_*.log"):
        found = list(logs.glob(pat))
        if found: cand["mediator"] = sorted(found)[-1]; break
    return cand

def slurp_clean(path: Path, max_bytes: int = 3_000_000) -> str:
    if not path.exists():
        return ""
    # Read bounded to avoid huge files
    data = path.read_text(encoding="utf-8", errors="ignore")
    if len(data) > max_bytes:
        data = data[-max_bytes:]  # last N bytes (most recent)
    return sanitize(data)

# ---------- Summarization ----------
def ollama_summarize(model: str, prompt: str, timeout: int = 60) -> str:
    """
    Run `ollama run <model>` with a hard timeout and non-TTY env so it
    doesn't emit spinners/ANSI. Returns cleaned text or a warning.
    """
    if not shutil.which(OLLAMA_BIN):
        return f"[fatal] ollama not found at {OLLAMA_BIN}"

    env = os.environ.copy()
    env.update({
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CLICOLOR": "0",
        "COLORTERM": "0",
        "OLLAMA_SHELL": "0"
    })

    try:
        # Use subprocess.run with timeout; avoid .read() blocking
        res = subprocess.run(
            [OLLAMA_BIN, "run", model],
            input=(prompt + "\n").encode("utf-8", errors="ignore"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout
        )
        out = res.stdout.decode("utf-8", errors="ignore")
        err = res.stderr.decode("utf-8", errors="ignore")
        text = sanitize(out).strip()
        if res.returncode != 0:
            return f"[warn] summarizer exit={res.returncode}\n{sanitize(err)}\n{text}"
        return text or "[warn] empty summary"
    except subprocess.TimeoutExpired as te:
        partial = te.stdout.decode("utf-8", errors="ignore") if te.stdout else ""
        return "[warn] summarizer timeout\n" + sanitize(partial)

def chunked(items: list[str], max_chars: int) -> list[str]:
    chunks, cur = [], []
    total = 0
    for s in items:
        if total + len(s) > max_chars and cur:
            chunks.append("".join(cur))
            cur, total = [s], len(s)
        else:
            cur.append(s); total += len(s)
    if cur:
        chunks.append("".join(cur))
    return chunks

def simple_fallback_summary(transcript: str, actions: list[str], cap: int = 1600) -> str:
    body = sanitize(transcript)[-cap:]
    acts = "\n".join(f"- {a}" for a in actions[:40])
    return (
        "## TL;DR\nLocal fallback summary used (no/slow summarizer).\n\n"
        "## What happened\n"
        f"{body}\n\n"
        "## Files/Commands\n"
        f"{acts}\n"
    )

# ---------- Transcript building ----------
def extract_actions(text: str) -> list[str]:
    actions = []
    # Files written (case-insensitive), capture the rest of the line
    for m in re.finditer(r"\[(?:write|wrote file)[^\]]*\]\s+(.+)$", text, re.I | re.M):
        actions.append(f"WRITE: {m.group(1).strip()}")
    # Commands executed: match lines that have a `$` command prompt anywhere after a timestamp/prefix
    for m in re.finditer(r"^\s*(?:\[[^\n\]]+\]\s*)?\$\s+(.+)$", text, re.M):
        cmd = m.group(1).strip()
        if cmd:
            actions.append(f"CMD: {cmd}")
    # Also capture explicit 'Ran command:' variants anywhere in the line
    for m in re.finditer(r"Ran command:\s+(.+)$", text, re.I | re.M):
        actions.append(f"CMD: {m.group(1).strip()}")
    # Disk reports and similar telemetry
    for m in re.finditer(r"\[(?:disk|report|size)[^\]]*\]\s+([^\n]+)", text, re.I):
        actions.append(f"DISK: {m.group(1).strip()}")
    return actions[:400]  # slightly higher cap

def build_transcript(logs: dict[str, Path]) -> tuple[str, list[str]]:
    # Prefer creator (rich content); then questioner/mediator; fall back to master
    parts = []
    actions_all = []
    for key in ("questioner","creator","mediator","master"):
        p = logs.get(key)
        if not p: continue
        t = slurp_clean(p)
        if not t: continue
        parts.append(f"\n### [{key.upper()}]\n{t}\n")
        actions_all.extend(extract_actions(t))
    transcript = ("\n".join(parts)).strip()
    return transcript, actions_all

# ---------- Main ----------
def main():
    ap = argparse.ArgumentParser(description="Metaformers Scribe")
    ap.add_argument("--run", type=str, help="Path to a specific run dir (contains logs/)")
    ap.add_argument("--model", type=str, default=DEFAULT_SUMMARIZER, help="Summarizer model (ollama tag)")
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("SCRIBE_TIMEOUT","20")), help="Seconds per chunk summarization")
    ap.add_argument("--no-summarize", action="store_true", help="Skip model; write only cleaned transcript + actions")
    args = ap.parse_args()

    print(f"[{ts()}] Summarizer model: {args.model}  (timeout/chunk={args.timeout}s)")

    root = Path.cwd()
    run_dir = Path(args.run) if args.run else find_latest_run(root)
    if not run_dir:
        print("[fatal] no runs/ found.", file=sys.stderr); sys.exit(1)

    logs = pick_logs(run_dir)
    if not logs:
        print(f"[fatal] no logs found in {run_dir}/logs", file=sys.stderr); sys.exit(2)

    transcript, actions = build_transcript(logs)
    if not transcript:
        print(f"[fatal] logs are empty in {run_dir}/logs", file=sys.stderr); sys.exit(3)

    print(f"[{ts()}] Found logs in {run_dir}/logs")
    print(f"[{ts()}] Extracted actions: {len(actions)}")

    if args.no_summarize:
        out_path = run_dir / "summary.md"
        minimal = simple_fallback_summary(transcript, actions, cap=2000)
        data = minimal.encode("utf-8", errors="ignore")
        if len(data) > MAX_SUMMARY_BYTES:
            minimal = minimal[:MAX_SUMMARY_BYTES] + "\n\n[truncated]\n"
        out_path.write_text(minimal, encoding="utf-8")
        print(f"[{ts()}] Wrote summary (no-summarize): {out_path}")
        first_para = minimal.strip().split("\n\n", 1)[0].strip()
        print("\n=== TL;DR ===")
        print(first_para)
        return

    # Chunk transcript to keep summarizer sane
    items = transcript.splitlines(keepends=True)
    chunks = chunked(items, CHARS_PER_CHUNK)

    print(f"[{ts()}] Prepared {len(chunks)} chunk(s) for summarization (chunk ~{CHARS_PER_CHUNK} chars).")

    # Summarize each chunk, then summarize the summaries
    summaries = []
    for i, ch in enumerate(chunks, 1):
        print(f"[{ts()}] Summarizing chunk {i}/{len(chunks)} (timeout {args.timeout}s)...")
        summaries.append(ollama_summarize(args.model, (
            "You are a technical summarizer. Summarize the following log slice into:\n"
            "• Key points (bulleted)\n• Notable actions/commands/files\n• Open questions or TODOs\n"
            "Keep 80–160 words. Focus on signal, drop ANSI/spinners if any remain.\n\n"
            f"== SLICE {i}/{len(chunks)} ==\n{ch}"
        ), timeout=args.timeout))
        if not summaries[-1] or summaries[-1].startswith("[warn]"):
            print(f"[{ts()}]  ↳ chunk {i} fell back / warn.")
        else:
            print(f"[{ts()}]  ↳ chunk {i} ok ({len(summaries[-1])} chars).")

    if len(chunks) == 0 or all((not s) or s.startswith("[warn]") for s in summaries):
        print(f"[{ts()}] Summaries unavailable; using local fallback.")
        final = simple_fallback_summary(transcript, actions, cap=2400)
    else:
        mega = "\n\n".join(f"### Slice {i}\n{txt}" for i, txt in enumerate(summaries, 1))
        actions_block = "\n".join(f"- {a}" for a in actions)
        final_prompt = (
            "You are a concise editor. Fuse the slice summaries below into ONE coherent report with:\n"
            "1) TL;DR (1–2 sentences)\n2) What happened (bullets)\n3) Files touched / commands (bullets, dedupe)\n"
            "4) Any failures / warnings / blockers\n5) Next steps (bullets)\n"
            "Keep total under ~250–350 words.\n\n=== SLICE SUMMARIES ===\n"
            f"{mega}\n\n=== EXTRACTED ACTIONS (raw) ===\n{actions_block}\n"
        )
        final = ollama_summarize(args.model, final_prompt, timeout=args.timeout)
        if not final or final.startswith("[warn]"):
            print(f"[{ts()}] Final fuse warn; using local fallback.")
            final = simple_fallback_summary(transcript, actions, cap=2400)

    print(f"[{ts()}] Chunks: {len(chunks)}  Model: {args.model}  Timeout/chunk: {args.timeout}s")
    out_path = run_dir / "summary.md"
    data = final.encode("utf-8", errors="ignore")
    if len(data) > MAX_SUMMARY_BYTES:
        final = final[:MAX_SUMMARY_BYTES] + "\n\n[truncated to protect disk]\n"

    out_path.write_text(final, encoding="utf-8")
    print(f"[{ts()}] Wrote summary: {out_path}")
    print("\n=== TL;DR ===")
    # print first paragraph as a quick TL;DR
    first_para = final.strip().split("\n\n", 1)[0].strip()
    print(first_para)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n[{ts()}] Aborted by user.")