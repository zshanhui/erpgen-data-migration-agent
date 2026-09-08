"""Append-only audit logger for import runs.

Writes one JSON object per line to logs/import-<timestamp>.jsonl:
  run_start  — run metadata (doctype, source, mode, id field/column, plan)
  row        — one line per source row: status created|skipped|failed
  run_end    — totals + duration

Also keeps running counters and prints a human summary.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

STATUSES = ("created", "skipped", "failed")


class RunLogger:
    def __init__(self, log_dir: str | Path, tag: str = "import") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
        self.path = self.log_dir / f"{tag}-{self.stamp}.jsonl"
        self.counts: dict[str, int] = {s: 0 for s in STATUSES}
        self._fh = self.path.open("a", encoding="utf-8")
        self._start = time.time()

    def log(self, **event: Any) -> None:
        event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}
        self._fh.write(json.dumps(event, default=str) + "\n")
        self._fh.flush()

    def run_start(self, **meta: Any) -> None:
        self.log(event="run_start", **meta)

    def row(
        self,
        row_number: Optional[int],
        key: str,
        status: str,
        docname: Optional[str] = None,
        message: str = "",
    ) -> None:
        if status not in self.counts:
            raise ValueError(f"unknown row status: {status}")
        self.counts[status] += 1
        self.log(
            event="row",
            row=row_number,
            key=key,
            status=status,
            docname=docname,
            message=message,
        )

    def run_end(self, **extra: Any) -> Path:
        self.log(event="run_end", duration_s=round(time.time() - self._start, 3), **self.counts, **extra)
        self._fh.close()
        return self.path

    def summary(self, doctype: str, source: str) -> str:
        lines = [
            "Import summary",
            f"  doctype: {doctype} | source: {source}",
            f"  created: {self.counts['created']} | skipped: {self.counts['skipped']} | "
            f"failed: {self.counts['failed']}",
            f"  log: {self.path}",
        ]
        return "\n".join(lines)
