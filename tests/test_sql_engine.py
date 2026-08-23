"""
Tests for chatbot.sql.engine - promoted from its old __main__ demo block
(a FakeClient exercising the happy path and the no-match guard path).
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.chatbot.sql.engine import answer_question, build_schema_text, clean_sql, is_safe_select

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "databases" / "omantel_demo.db"

# The demo database was removed when the corpus was cleaned up for hosting.
# It belonged to the Omantel fork this project started from; Daleel has no
# database and chatbot.sql is not on the request path.
#
# The two tests that need it are skipped rather than deleted, because the
# other eleven — the SQL safety guard, which is the part that actually
# mattered — still run and still pass without it.
needs_demo_db = pytest.mark.skipif(
    not DB_PATH.exists(),
    reason="data/databases/omantel_demo.db was removed with the Omantel fork's "
           "demo data; chatbot.sql is legacy and off the request path.",
)


class FakeClient:
    """Returns canned SQL for the SQL-writing call, and a canned sentence for
    the phrasing call - proves the wiring without spending a real API call."""

    class chat:
        class completions:
            @staticmethod
            def create(model, messages, temperature):
                prompt = messages[0]["content"]
                if "SQL generator" in prompt:  # the SQL-writing call
                    if "Galaxy" in prompt:
                        text = "SELECT plan_name, monthly_price_omr FROM plans WHERE plan_name = 'Galaxy 99'"
                    else:
                        text = "SELECT plan_name, monthly_price_omr FROM plans WHERE plan_name = 'Smart 20'"
                else:  # the phrasing call
                    text = "Smart 20 costs 20 OMR per month."
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT * FROM plans", True),
        ("select plan_name from plans where plan_name = 'Smart 20'", True),
        ("DROP TABLE plans", False),
        ("SELECT * FROM plans; DROP TABLE plans", False),
        ("INSERT INTO plans VALUES ('x', 'y', 1, 1, 1)", False),
        ("SELECT created_at FROM plans", True),  # 'create' as a substring of a column name must NOT trip the guard
        ("SELECT * FROM plans WHERE notes = 'please drop this plan'", False),  # keyword inside a value, no ';' and starts with SELECT - only rule (c) can catch this
    ],
)
def test_is_safe_select(sql, expected):
    assert is_safe_select(sql) is expected


@needs_demo_db
def test_answer_question_happy_path():
    schema = build_schema_text(str(DB_PATH))
    result = answer_question("how much is Smart 20?", schema, FakeClient, str(DB_PATH))
    assert result["rows"] == [("Smart 20", 20.0)]
    assert "20" in result["answer"]


@needs_demo_db
def test_answer_question_no_match_guard():
    schema = build_schema_text(str(DB_PATH))
    result = answer_question("how much is Galaxy 99?", schema, FakeClient, str(DB_PATH))
    assert result["rows"] == []
    assert "couldn't find" in result["answer"].lower()


def test_clean_sql_plain_query_unchanged():
    # Arrange: a plain SQL string, no markdown, no prefix, no trailing text
    raw = "SELECT * FROM plans"

    # Act: call the real function
    result = clean_sql(raw)

    # Assert: nothing should have changed
    assert result == "SELECT * FROM plans"


def test_clean_sql_strips_code_fence_and_sql_label():
    # Arrange: what an LLM often actually returns - a fenced code block
    raw = "```sql\nSELECT * FROM plans\n```"

    result = clean_sql(raw)

    assert result == "SELECT * FROM plans"


def test_clean_sql_strips_sql_colon_prefix():
    raw = "sql: SELECT * FROM plans"

    result = clean_sql(raw)

    assert result == "SELECT * FROM plans"


def test_clean_sql_cuts_off_at_first_semicolon():
    # Arrange: a stacked/trailing statement after the first ';'
    raw = "SELECT * FROM plans; DROP TABLE plans"

    result = clean_sql(raw)

    # Assert: only the part before the ';' survives
    assert result == "SELECT * FROM plans"
