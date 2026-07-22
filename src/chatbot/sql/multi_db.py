"""
Layer A: many SQLite databases behind one text-to-SQL pipeline.

Train analogy: one connection is the engine; each extra .db is a cabin
coupled on with ATTACH (read-only). One query can then ride through all
cabins using 'alias.table' names. Reuses the guard + SQL writer from
text_to_sql.py, so nothing about safety changes.
"""

import os
import sqlite3
from chatbot.sql.engine import is_safe_select, question_to_sql


def _alias_for(path, taken):
    """Turn a file path into a safe, unique SQL alias (plans.db -> plans)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    alias = "".join(ch if ch.isalnum() else "_" for ch in stem)
    base, i = alias, 1
    while alias in taken:
        i += 1
        alias = f"{base}_{i}"
    taken.add(alias)
    return alias


def assign_aliases(db_paths):
    """One alias per DB, decided once so schema + connection always agree."""
    taken, aliases = set(), {}
    for p in db_paths:
        aliases[p] = _alias_for(p, taken)
    return aliases


def build_schema_text_multi(db_paths):
    """Combined 'menu' across all DBs; every table labelled alias.table."""
    aliases = assign_aliases(db_paths)
    lines = []
    for path, alias in aliases.items():
        lines.append(f"# Database '{alias}' (reference its tables as {alias}.<table>)")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        for (table,) in cur.fetchall():
            lines.append(f"Table: {alias}.{table}")
            lines.append("Columns:")
            cur.execute(f"PRAGMA table_info({table})")
            for col in cur.fetchall():
                name, ctype = col[1], col[2]
                hint = ""
                if ctype.upper() == "TEXT":
                    cur.execute(f"SELECT DISTINCT {name} FROM {table} LIMIT 6")
                    vals = [str(r[0]) for r in cur.fetchall()]
                    if len(vals) <= 5:
                        hint = "  - values: " + ", ".join(vals)
                lines.append(f"  {name} ({ctype}){hint}")
            lines.append("")
        conn.close()
    return "\n".join(lines).strip()


def connect_all_readonly(db_paths):
    """Engine + coupled cabins: one connection, every DB ATTACHed read-only."""
    aliases = assign_aliases(db_paths)
    conn = sqlite3.connect(":memory:", uri=True)          # the engine
    for path, alias in aliases.items():
        conn.execute(f"ATTACH DATABASE 'file:{path}?mode=ro' AS {alias}")  # couple a cabin
    return conn


def run_sql_multi(sql, db_paths):
    """Same two safety walls as run_sql, now across all attached DBs."""
    if not is_safe_select(sql):
        raise ValueError(f"Blocked unsafe SQL: {sql!r}")
    conn = connect_all_readonly(db_paths)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def answer_question_multi(question, schema_text, client, db_paths,
                          model="Qwen/Qwen2.5-7B-Instruct"):
    """Full pipeline, multi-DB version."""
    sql = question_to_sql(question, schema_text, client, model)
    try:
        rows = run_sql_multi(sql, db_paths)
    except Exception as e:
        return {"answer": "Sorry, I couldn't answer that from the database.",
                "sql": sql, "rows": None, "error": str(e)}
    if not rows:
        return {"answer": "I couldn't find anything matching that in the database.",
                "sql": sql, "rows": []}
    phrasing_prompt = (
        "Answer the user's question in one short, friendly sentence, using ONLY "
        "the data provided. Mention it comes from Omantel's records. Do not invent.\n\n"
        f"Question: {question}\nData: {rows}\nAnswer:"
    )
    resp = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": phrasing_prompt}], temperature=0)
    return {"answer": resp.choices[0].message.content.strip(), "sql": sql, "rows": rows}


if __name__ == "__main__":
    DBS = ["/home/claude/phase4/plans.db", "/home/claude/phase4/billing.db"]

    print("=== Combined schema the LLM sees ===\n")
    print(build_schema_text_multi(DBS))

    print("\n=== A query that rides through BOTH cabins (a cross-DB JOIN) ===")
    sql = ("SELECT p.plan_name, SUM(b.amount_omr) AS total_billed "
           "FROM plans.plans p JOIN billing.invoices b ON b.plan_name = p.plan_name "
           "GROUP BY p.plan_name")
    print("rows:", run_sql_multi(sql, DBS))

    print("\n=== Read-only still holds: writing to an attached cabin fails ===")
    conn = connect_all_readonly(DBS)
    try:
        conn.execute("INSERT INTO billing.invoices VALUES ('X','Y',0,'2026-01')")
    except Exception as e:
        print("  refused:", type(e).__name__, "-", e)
    finally:
        conn.close()
