"""
chatbot.agent — the layers that decide, rather than the layers that fetch.

This package is the literal answer to the panel question "where is the
agent?". Each module here makes one decision before or around retrieval:

  registry.py   D6 Layer 1 — is this question even in scope? Deterministic,
                runs BEFORE embedding, and can refuse without retrieving.

Later layers (the clarifying question, the grounding self-check) join this
package as they are built.
"""
