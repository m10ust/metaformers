#!/usr/bin/env python3
import os, sys, json, time, re, requests, psycopg2
import warnings
try:
    from urllib3.exceptions import NotOpenSSLWarning
except Exception:
    NotOpenSSLWarning = None

# silence urllib3 LibreSSL warning & any stray -W env noise
os.environ.pop("PYTHONWARNINGS", None)
if NotOpenSSLWarning is not None:
    warnings.simplefilter("ignore", NotOpenSSLWarning)

from typing import List, Tuple
from sentence_transformers import SentenceTransformer

# -------- settings --------
MODEL_NAME = os.getenv("LOCAL_MODEL", "llama2-uncensored:latest")   # Ollama model tag
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
SESSION_ID = os.getenv("SESSION_ID", f"session-{int(time.time())}")
TOP_K = int(os.getenv("TOP_K", "6"))
MIN_SIM = float(os.getenv("MIN_SIM", "0.35"))  # cosine distance in pgvector (0 identical; smaller is closer)
BLOCK_TERMS_ENV = os.getenv("BLOCK_TERMS", "")
DEFAULT_BLOCK_TERMS = [t.strip().lower() for t in BLOCK_TERMS_ENV.split(",") if t.strip()]

# -------- db --------
def db_conn():
    return psycopg2.connect(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "metaformers"),
        user=os.getenv("PGUSER", "meta"),
        password=os.getenv("PGPASSWORD", None),
    )

# -------- cleaning --------
ANSI_RE = re.compile(r"(?:\x1B\[[0-9;?]*[ -/]*[@-~])|(?:\x1B[@-Z\\-_])|(?:\x1B\][^\x07]*\x07)")
SPINNER_RE = re.compile(r"[\u2800-\u28FF◐◓◑◒⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+")
NOISE_RE = re.compile(
    r"(?:^|\n)\s*(Topic:|CREATOR:|MEDIATOR:|REVIEWER:|=== Turn|\[\d{4}-\d\d-\d\dT|Thinking\.\.\.|BEGIN|END|<<|>>|\[llama|\x1b)",
    re.IGNORECASE,
)
NEG_PATTERNS = [
    re.compile(r"(?:nothing|not)\s+(?:about|to\s+do\s+with)\s+(.+)", re.I),
    re.compile(r"other\s+than\s+(.+)", re.I),
    re.compile(r"avoid\s+talking\s+about\s+(.+)", re.I),
]
def extract_block_terms(user_text: str) -> list:
    terms = []
    if not user_text:
        return terms
    for pat in NEG_PATTERNS:
        m = pat.search(user_text)
        if m:
            chunk = m.group(1)
            # split on commas or 'and'
            parts = re.split(r",|\band\b", chunk, flags=re.I)
            terms.extend([p.strip().lower() for p in parts if p.strip()])
    return terms

def clean_text(s: str) -> str:
    if not s: return ""
    s = s.replace("\x00","")
    s = ANSI_RE.sub("", s)
    s = SPINNER_RE.sub("", s)
    s = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", s)
    return s.strip()

def to_vec_literal(vec: List[float]) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"

# -------- memory ops --------
EMB_MODEL_ID = os.getenv("EMB_MODEL", "all-MiniLM-L6-v2")  # 384-d
EMB = SentenceTransformer(EMB_MODEL_ID)

def remember(role: str, text: str, conn):
    text = clean_text(text)
    if text.startswith("(model error)"):
        return
    if not text: return
    if NOISE_RE.search(text):
        return
    emb = EMB.encode([text], convert_to_numpy=True)[0].tolist()
    vec = to_vec_literal(emb)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.contexts (session_id, role, text, embedding)
            VALUES (%s, %s, %s, %s::vector)
            RETURNING id;
        """, (SESSION_ID, role, text, vec))
        rid = cur.fetchone()[0]
    conn.commit()
    return rid

def recall(query: str, conn, k: int = TOP_K, block_terms: List[str] = None) -> List[Tuple[str, str]]:
    block_lc = set((block_terms or []) + DEFAULT_BLOCK_TERMS)
    block_lc = {b for b in (t.lower() for t in block_lc) if b}
    q = EMB.encode([query], convert_to_numpy=True)[0].tolist()
    qvec = to_vec_literal(q)
    with conn.cursor() as cur:
        # Overfetch within this session; we'll filter by distance in Python
        cur.execute(f"""
            SELECT role, text, (embedding <=> %s::vector) AS dist
            FROM public.contexts
            WHERE session_id = %s
            ORDER BY dist ASC
            LIMIT {k*6}
        """, (qvec, SESSION_ID))
        rows = cur.fetchall()

    # Filter to items within the similarity gate and drop noisy/log-like chunks
    kept: List[Tuple[str, str, float]] = []
    for role, txt, dist in rows:
        if dist is None:
            continue
        if float(dist) > MIN_SIM:
            continue
        if not txt:
            continue
        if NOISE_RE.search(txt):
            continue
        low = txt.lower()
        if any(bt in low for bt in block_lc):
            continue
        kept.append((role, txt, float(dist)))

    # Sort again just in case and take top-k
    kept.sort(key=lambda r: r[2])
    return [(role, text) for role, text, _ in kept[:k]]

# -------- chat to local model (Ollama) --------
def chat_ollama(model: str, system_prompt: str, messages: List[dict]) -> str:
    payload = {
        "model": model,
        "messages": ([{"role":"system","content":system_prompt}] if system_prompt else []) + messages,
        "stream": False
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data.get("message", {}).get("content", "").strip()

def format_context(snips: List[Tuple[str, str]], block_terms: List[str] = None) -> str:
    if not snips:
        return ""
    block_lc = {b.lower() for b in (block_terms or []) if b}
    blocks = []
    for role, txt in snips:
        if not txt or NOISE_RE.search(txt):
            continue
        low = txt.lower()
        if any(bt in low for bt in block_lc):
            continue
        txt = clean_text(txt)[:1200]  # cap each snippet to keep prompts lean
        if not txt:
            continue
        blocks.append(f"{role.upper()}: {txt}")
    return "\n---\n".join(blocks)

def main():
    print(f"[init] embeddings: {EMB_MODEL_ID} (384)   model: {MODEL_NAME}   session: {SESSION_ID}")
    print("Type your message. Ctrl+C to quit.\n")
    conn = db_conn()

    SYSTEM = (
        "You are a normal chat assistant.\n"
        "You may be shown prior snippets from THIS chat session only.\n"
        "Ignore content that looks like logs, plans, or role headers such as "
        '"Topic:", "CREATOR:", "MEDIATOR:", "REVIEWER:", "=== Turn", timestamps, '
        '"Thinking...", or model tags in brackets. Speak plainly to the user.'
    )

    try:
        while True:
            user = input("You> ").strip()
            if not user: 
                continue

            # detect per‑turn negative topics (e.g., "nothing to do with X" / "other than X")
            block_terms = extract_block_terms(user)
            
            # fetch memory with blocklist
            mem = recall(user, conn, block_terms=block_terms)
            ctx = format_context(mem, block_terms=block_terms)
            
            # strengthen the system message for this turn if there are block terms
            system_turn = SYSTEM
            if block_terms:
                system_turn = SYSTEM + "\nFor this turn, avoid these topics entirely: " + ", ".join(sorted(set(block_terms)))

            prompt = user if not ctx else f"[CONTEXT]\n{ctx}\n\n[USER]\n{user}"

            # send to model
            try:
                reply = chat_ollama(MODEL_NAME, system_turn, [
                    {"role":"user","content": prompt}
                ])
            except Exception as e:
                reply = f"(model error) {e}"

            print(f"\nAI> {reply}\n")

            # persist both sides
            remember("user", user, conn)
            remember("assistant", reply, conn)

    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
