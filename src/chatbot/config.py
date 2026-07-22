"""
config.py — single source of truth for environment-driven settings.

Every other module reads its configuration from `settings` here instead of
calling os.environ.get() directly, so the full set of things this app
depends on (tokens, model id, file locations) is visible in one place
instead of scattered across app.py / rag/retrieval.py / scripts.

Importing this module loads .env and validates HF_TOKEN immediately (fails
fast on missing config, same behavior as before the reorg).
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# src/chatbot/config.py -> src/chatbot -> src -> repo root
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"


@dataclass(frozen=True)
class Settings:
    hf_token: str
    model_id: str
    port: int
    log_level: str
    db_paths: list
    index_file: Path
    chunks_file: Path
    source_docs_dir: Path
    chat_log_file: Path
    stage_log_file: Path


def load_settings() -> Settings:
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        raise RuntimeError("HF_TOKEN is missing. Copy '.env.example' to '.env'.")

    default_db = DATA_DIR / "databases" / "omantel_demo.db"
    db_paths_raw = os.environ.get("DB_PATHS", str(default_db))
    db_paths = [p.strip() for p in db_paths_raw.split(",") if p.strip()]

    LOGS_DIR.mkdir(exist_ok=True)

    return Settings(
        hf_token=hf_token,
        model_id=os.environ.get("MODEL_ID", "Qwen/Qwen2.5-7B-Instruct"),
        port=int(os.environ.get("PORT", 7860)),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        db_paths=db_paths,
        index_file=DATA_DIR / "index" / "embeddings.npy",
        chunks_file=DATA_DIR / "index" / "chunks.json",
        source_docs_dir=DATA_DIR / "source_docs",
        chat_log_file=LOGS_DIR / "chat_log.csv",
        stage_log_file=LOGS_DIR / "stage_log.jsonl",
    )


settings = load_settings()
