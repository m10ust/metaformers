#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mass ingestion for Metaformers logs -> PostgreSQL (pgvector)
- Embedding model: all-MiniLM-L6-v2 (384 dims)
- Cleans NUL bytes, control chars, ANSI, spinner glyphs
- Batches inserts with psycopg2.extras.execute_values
- Uses PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD if set
"""

import os, re, sys, glob, math, warnings
from typing import Iterable, List, Tuple
warnings.filterwarnings("ignore", module="urllib3")  # hush LibreSSL gripe

import psycopg2
import psycopg2.extras as extras
from sentence_transformers import SentenceTransformer

# ---------------- Config ----------------
EMB_MODEL = os.getenv("EMB_MODEL", "all-MiniLM-L6-v2")  # 384 dims
EMB_DIM = 384
BATCH = int(os.getenv("INGEST_BATCH", "64"))
CHUNK_CHARS = int(os.getenv("INGEST_CHUNK", "2000"))  # split big files

# -------------- Cleaning ----------------
ANSI_RE = re.compile(r"(?:\x1B\[[0-9;?]*[ -/]*[@-~])|(?:\x1B[@-Z\\-_])|(?:\x1B\][^\x07]*\x07)")
SPINNER_RE = re.compile(r"[\u2800-\u28FF◐◓◑◒⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+")

def clean_text(s: str) -> str:
    if not s: return ""
    s = s.replace("\x00", "")               # NULs -> drop (PG barfs on 0x00)
    s = ANSI_RE.sub("", s)                  # strip ANSI
    s = SPINNER_RE.sub("", s)               # strip spinner glyphs
    s = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", s)  # other control chars
    s = s.replace("\r", "")
    return s.strip()

def chunk_text(s: str, size: int) -> List[str]:
    if len(s) <= size: return [s]
    return [s[i:i+size] for i in range(0, len(s), size)]

# --------------- DB stuff ---------------
def db_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "metaformers"),
        user=os.getenv("PGUSER", "meta"),
        password=os.getenv("PGPASSWORD", None),
    )

UPSERT_SQL = """
INSERT INTO public.contexts (session_id, role, text, embedding)
VALUES %s
RETURNING id;
"""

def emb_to_vector_literal(vec) -> str:
    # pgvector expects [a,b,c] literal; ensure round-trippable string
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"

# --------------- Main ingest ------------
def iter_files(patterns: List[str]) -> List[str]:
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat, recursive=True))
    # de-dupe, keep only .txt files that exist
    uniq = []
    seen = set()
    for p in files:
        if not os.path.isfile(p): continue
        if not p.lower().endswith(".txt"): continue
        if p in seen: continue
        seen.add(p); uniq.append(p)
    return sorted(uniq)

def embed_texts(model: SentenceTransformer, texts: List[str]):
    # sentence-transformers returns numpy array; keep as Python lists
    return model.encode(texts, convert_to_numpy=True, normalize_embeddings=False).tolist()

def main(argv: List[str]):
    if len(argv) < 2:
        print("Usage: python ingest.py 'runs/**/*.txt' ['more/**/*.txt' ...]", file=sys.stderr)
        sys.exit(1)

    paths = iter_files(argv[1:])
    if not paths:
        print("No .txt files matched.", file=sys.stderr)
        sys.exit(1)

    # Load model once
    print(f"[init] loading embeddings: {EMB_MODEL} ({EMB_DIM} dims)")
    model = SentenceTransformer(EMB_MODEL)

    # DB
    conn = db_conn()
    cur = conn.cursor()

    inserted_total = 0
    for fp in paths:
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read()
        except Exception as e:
            print(f"[skip] {fp} (read error: {e})", file=sys.stderr)
            continue

        txt = clean_text(raw)
        if not txt:
            print(f"[skip] {fp} (empty after clean)")
            continue

        # chunk, embed in batches, insert
        chunks = chunk_text(txt, CHUNK_CHARS)
        n = len(chunks)
        session = os.path.basename(os.path.dirname(fp)) or "root"
        role = "user"  # neutral default for raw logs

        # process in mini-batches to keep memory stable
        for i in range(0, n, BATCH):
            batch_texts = chunks[i:i+BATCH]
            embs = embed_texts(model, batch_texts)

            # sanity: enforce correct dims
            for e in embs:
                if len(e) != EMB_DIM:
                    raise ValueError(f"Embedding dim {len(e)} != {EMB_DIM}")

            rows: List[Tuple[str,str,str,str]] = []
            for j, (t, e) in enumerate(zip(batch_texts, embs), start=i):
                vec_lit = emb_to_vector_literal(e)  # "[...]" string
                rows.append((
                    f"{session}",         # session_id
                    role,                 # role
                    t,                    # text
                    vec_lit               # embedding literal as text
                ))

            # execute_values with a cast on the 4th param to vector(EMB_DIM)
            tpl = "(%s,%s,%s,%s::vector)"
            try:
                extras.execute_values(cur, UPSERT_SQL, rows, template=tpl, page_size=len(rows))
                ids = cur.fetchall()
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[fail] {fp} batch {i}-{i+len(rows)-1}: {e}", file=sys.stderr)
                continue

            inserted_total += len(rows)
            # nicelog: file + chunk window
            first_id = ids[0][0] if ids else "?"
            print(f"[ok] {fp} #{i:04d}-{i+len(rows)-1:04d} -> first_id={first_id}")

        # one per file summary
        print(f"[ok] {fp} total_chunks={n}")

    cur.close(); conn.close()
    print(f"[done] inserted {inserted_total} rows")

if __name__ == "__main__":
    main(sys.argv)