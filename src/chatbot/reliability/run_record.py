"""
run_record.py — collects every stage's StageResult for ONE request,
then writes one structured JSONL line per stage to a dedicated log file.

This is the 'story' of a request: which stages ran, which retried,
which failed, how long each took. It's separate from chat_log.csv.

It also finally lands the Phase 3 logging trio:
  - Date       -> the run-level 'ts' on every line
  - answer time-> each stage's 'duration_s' (already recorded by the runner)
  - Chunk ID   -> passed in as stage 'extra' from the retrieve stage's payload
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from chatbot.reliability.stage_runner import StageStatus

logger = logging.getLogger(__name__)

# Maps a stage outcome to a log severity: FAILED is a real error, a stage
# that only succeeded after a retry is worth a human noticing (WARNING),
# everything else (OK/EMPTY) is routine (INFO).
_LEVEL_BY_STATUS = {
    StageStatus.FAILED: logging.ERROR,
    StageStatus.RETRIED_OK: logging.WARNING,
}


class RunRecord:
    def __init__(self, query: str, log_path: str = "stage_log.jsonl",
                 request_id: str = None):
        self.request_id = request_id or uuid.uuid4().hex[:12]
        self.query = query
        self.log_path = log_path
        self.ts = datetime.now(timezone.utc).isoformat()
        self.stages = []  # list of (StageResult, extra_dict)

    def add(self, result, extra: dict = None):
        """Record one finished stage, plus optional small metadata to log."""
        self.stages.append((result, extra or {}))
        return result  # convenient pass-through

    def overall_status(self) -> str:
        statuses = [r.status for r, _ in self.stages]
        if StageStatus.FAILED in statuses:
            return "failed"
        if statuses and statuses[-1] is StageStatus.EMPTY:
            return "empty"
        return "ok"

    def failed_stage(self):
        """The name of the stage that broke, or None. The whole point."""
        for r, _ in self.stages:
            if r.status is StageStatus.FAILED:
                return r.name
        return None

    def write(self):
        """
        Append one JSONL line per stage to the local log file, AND emit the
        same information as a structured log record to stdout.

        The file (STAGE_LOG_FILE) lives on the container's local disk, which
        Render's Logs dashboard never reads and which does not survive a
        restart/redeploy - it's a local-dev convenience only. The stdout
        line is what actually shows up in Render, so it's the source of
        truth in production; both are built from the same fields on purpose
        so a request looks identical whichever one you're reading.
        """
        with open(self.log_path, "a", encoding="utf-8") as f:
            for result, extra in self.stages:
                line = {
                    "ts": self.ts,                      # Phase 3: Date
                    "request_id": self.request_id,
                    "query": self.query[:200],
                    "stage": result.name,
                    "status": result.status.value,
                    "attempts": result.attempts,
                    "duration_s": result.duration_s,    # Phase 3: answer time
                    "delays_used": result.delays_used,
                    "error": result.error,
                    **extra,                            # Phase 3: chunk_ids, route, etc.
                }
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

                # "ts" is renamed to "request_ts" here (not left as "ts") so
                # it can't collide with the log record's own "ts" field
                # (the JSON formatter stamps that from the actual log-emit
                # time, which is a distinct thing from the request's start).
                log_extra = {k: v for k, v in line.items() if k != "ts"}
                log_extra["request_ts"] = line["ts"]
                level = _LEVEL_BY_STATUS.get(result.status, logging.INFO)
                logger.log(level, "stage_completed", extra=log_extra)
