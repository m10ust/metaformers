# memory_chat.py
import os, psycopg2
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer, AutoModelForCausalLM

EMB = SentenceTransformer("all-MiniLM-L6-v2")  # 384-dim (matches your ingest)
PG = dict(
    host=os.getenv("PGHOST","127.0.0.1"),
    port=os.getenv("PGPORT","5432"),
    dbname=os.getenv("PGDATABASE","metaformers"),
    user=os.getenv("PGUSER","meta"),
    password=os.getenv("PGPASSWORD")
)

def db():
    return psycopg2.connect(**PG)

def embed(text:str):
    v = EMB.encode([text], convert_to_numpy=True)[0].tolist()
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

def recall(conn, session_id:str, query:str, k:int=5):
    q = embed(query)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, role, text
            FROM public.contexts
            WHERE session_id = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s;
            """,
            (session_id, q, k)
        )
        return cur.fetchall()  # [(id, role, text), ...]

def remember(conn, session_id:str, role:str, text:str):
    v = embed(text)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.contexts (session_id, role, text, embedding)
            VALUES (%s,%s,%s,%s::vector)
            RETURNING id;
            """,
            (session_id, role, text, v)
        )
        new_id = cur.fetchone()[0]
    conn.commit()
    return new_id

# --- toy local LLM (swap with your runner) ---
tok = AutoTokenizer.from_pretrained("facebook/opt-350m")
lm  = AutoModelForCausalLM.from_pretrained("facebook/opt-350m")

def generate(prompt, max_new_tokens=256):
    ids = tok(prompt, return_tensors="pt").input_ids
    out = lm.generate(ids, max_new_tokens=max_new_tokens, do_sample=True, top_p=0.9)
    return tok.decode(out[0], skip_special_tokens=True)

if __name__ == "__main__":
    session = "demo_session_1"  # use a UUID per chat/thread
    conn = db()
    print("Type 'exit' to quit.")
    while True:
        user = input("\nYou: ").strip()
        if user.lower() == "exit": break

        # 1) recall memory
        memories = recall(conn, session, user, k=5)
        context = "\n".join(f"{r.upper()}: {t}" for _, r, t in memories)

        # 2) build prompt with memory
        prompt = (
            f"{context}\n"
            f"USER: {user}\n"
            f"ASSISTANT: (Use the context above if relevant; otherwise answer directly.)"
        )

        # 3) model answer
        answer = generate(prompt)

        # 4) write both sides back to memory
        remember(conn, session, "user", user)
        remember(conn, session, "assistant", answer)

        print("\nAssistant:", answer.split("ASSISTANT:",1)[-1].strip())
    conn.close()
