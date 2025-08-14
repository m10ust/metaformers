#!/usr/bin/env python3
from sentence_transformers import SentenceTransformer
import psycopg2, json

TEXT = "hello world"
SESSION = "s1"

# 768-dim model
model = SentenceTransformer("sentence-transformers/all-mpnet-base-v2")
vec = model.encode(TEXT).astype("float32").tolist()

conn = psycopg2.connect(dbname="metaformers", host="localhost")
cur = conn.cursor()

cur.execute("""
INSERT INTO public.contexts (session_id, role, text, embedding)
VALUES (%s, %s, %s, %s)
RETURNING id, vector_dims(embedding)
""", (SESSION, "user", TEXT, vec))

row = cur.fetchone()
conn.commit()
cur.close(); conn.close()

print("Inserted row id:", row[0], "dims:", row[1])
