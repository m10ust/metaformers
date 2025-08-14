import psycopg2, json
from sentence_transformers import SentenceTransformer

conn = psycopg2.connect(dbname="metaformers", host="localhost")  # add user/password if needed
cur  = conn.cursor()

model = SentenceTransformer("sentence-transformers/all-mpnet-base-v2")  # 768 dims
text  = "metacognition test snippet"
vec   = model.encode(text).astype("float32")               # ensure float32

cur.execute(
    "INSERT INTO public.contexts (session_id, role, text, embedding) VALUES (%s,%s,%s,%s::vector)",
    ("s1", "user", text, json.dumps(vec.tolist()))         # pass JSON array; cast to vector in SQL
)
conn.commit()

cur.execute(
    "SELECT id, text, embedding <=> %s::vector AS cos_dist "
    "FROM public.contexts ORDER BY embedding <=> %s::vector LIMIT 3",
    (json.dumps(vec.tolist()), json.dumps(vec.tolist()))
)
print(cur.fetchall())
cur.close(); conn.close()
