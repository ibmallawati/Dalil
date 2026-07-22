import sqlite3
import os
import re


def build_schema_text(db_path):
    """PIECE 1: read the DB's structure into a schema string for the LLM."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    lines = []
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cur.fetchall()]
    for table in tables:
        lines.append(f"Table: {table}")
        lines.append("Columns:")
        cur.execute(f"PRAGMA table_info({table})")
        for col in cur.fetchall():
            col_name, col_type = col[1], col[2]
            hint = ""
            if col_type.upper() == "TEXT":
                cur.execute(f"SELECT DISTINCT {col_name} FROM {table} LIMIT 6")
                values = [str(r[0]) for r in cur.fetchall()]
                if len(values) <= 5:
                    hint = "  - values: " + ", ".join(values)
            lines.append(f"  {col_name} ({col_type}){hint}")
        lines.append("")
    conn.close()
    return "\n".join(lines).strip()


def clean_sql(raw):
    """PIECE 2 helper: strip fences / labels so the SQL is runnable."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text[:3].lower() == "sql":
            text = text[3:].strip()
    if text[:4].lower() == "sql:":
        text = text[4:].strip()
    text = text.split(";")[0].strip()
    return text


def question_to_sql(question, schema_text, client, model="Qwen/Qwen2.5-7B-Instruct"):
    """PIECE 2: turn a natural-language question into a cleaned SELECT."""
    prompt = f"""You are a SQL generator for a SQLite database.
Using ONLY the schema below, write ONE SQLite SELECT query that answers the question.
Return ONLY the SQL. No explanation, no markdown, no code fences.

Schema:
{schema_text}

Question: {question}
SQL:"""
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return clean_sql(resp.choices[0].message.content)


# ---------- PIECE 3: safety guard + read-only execution ----------

def is_safe_select(sql):
    """WALL 1: allow only a single, read-only SELECT."""
    text = sql.strip()
    # (a) must begin with SELECT
    if not text.lower().startswith("select"):
        return False
    # (b) no stacked statements (a ';' in the middle = a second hidden query)
    if ";" in text.rstrip(";"):
        return False
    # (c) block write/DDL keywords -- as WHOLE WORDS, so a column called
    #     'created_at' is NOT mistaken for the CREATE keyword.
    forbidden = ["drop", "delete", "update", "insert", "alter",
                 "create", "replace", "truncate", "attach", "detach"]
    for word in forbidden:
        if re.search(rf"\b{word}\b", text, re.IGNORECASE):
            return False
    return True


def run_sql(sql, db_path):
    """WALL 2: run the SQL on a READ-ONLY connection. Writing is impossible."""
    if not is_safe_select(sql):
        raise ValueError(f"Blocked unsafe SQL: {sql!r}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)  # ro = read-only
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchall()
    finally:
        conn.close()




def answer_question(question, schema_text, client, db_path,
                    model="Qwen/Qwen2.5-7B-Instruct"):
    """PIECE 4: question -> SQL -> safe run -> friendly phrased answer."""
    # 1. LLM writes the SQL (piece 2)
    sql = question_to_sql(question, schema_text, client, model)

    # 2. run it safely (piece 3); never crash on a bad query
    try:
        rows = run_sql(sql, db_path)
    except Exception as e:
        return {"answer": "Sorry, I couldn't answer that from the database.",
                "sql": sql, "rows": None, "error": str(e)}

    # 3. no matching data -> say so, do NOT invent an answer
    if not rows:
        return {"answer": "I couldn't find anything matching that in the database.",
                "sql": sql, "rows": []}

    # 4. LLM phrases the rows into one friendly sentence
    phrasing_prompt = f"""Answer the user's question in one short, friendly sentence.
Use ONLY the data provided. Do not invent anything.

Question: {question}
Data (rows from the database): {rows}

Answer:"""
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": phrasing_prompt}],
        temperature=0,
    )
    answer = resp.choices[0].message.content.strip()
    return {"answer": answer, "sql": sql, "rows": rows}
