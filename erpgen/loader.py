"""Loaders: push mapped payloads into ERPNext.

Two paths:
  * RestLoader.upsert     — per-record REST inserts, idempotent (dedup upstream
                            in erpgen.dedup), per-row audit logging
  * DataImportLoader      — bulk CSV via the Data Import machinery (background
                            job); feed it the already-deduped CSV for idempotency
"""
from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .client import ERPNextClient
from .logger import RunLogger

INTERNAL_KEYS = ("__row", "__skip_reason")


@dataclass
class ImportResult:
    doctype: str
    mode: str
    status: str = "pending"
    success: int = 0
    skipped: int = 0
    failed: int = 0
    total: int = 0
    data_import_name: Optional[str] = None
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "doctype": self.doctype,
            "mode": self.mode,
            "status": self.status,
            "success": self.success,
            "skipped": self.skipped,
            "failed": self.failed,
            "total": self.total,
            "data_import_name": self.data_import_name,
            "errors": self.errors[:20],
        }


class DataImportLoader:
    def __init__(self, client: ERPNextClient) -> None:
        self.client = client

    def load(
        self,
        doctype: str,
        csv_text: str,
        import_type: str = "Insert New Records",
        submit_after_import: bool = False,
        skipped: int = 0,
        timeout: int = 300,
        logger: Optional[RunLogger] = None,
    ) -> ImportResult:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(csv_text)
            tmp = fh.name

        try:
            upload = self.client.upload_file(tmp, is_private=1)
            di = self.client.create_data_import(
                doctype,
                upload["file_url"],
                import_type=import_type,
                submit_after_import=submit_after_import,
            )
            if logger:
                logger.log(event="data_import_start", name=di["name"], file=upload["file_url"])
            started = self.client.start_import(di["name"])
            if not started:
                raise RuntimeError(f"Import not enqueued for {di['name']}")
            status = self.client.wait_for_import(di["name"], timeout=timeout)
            result = ImportResult(
                doctype=doctype,
                mode=import_type,
                status=status.get("status", "unknown"),
                success=int(status.get("success") or 0),
                skipped=skipped,
                failed=int(status.get("failed") or 0),
                total=int(status.get("total_records") or 0) + skipped,
                data_import_name=di["name"],
            )
            logs = self.client.import_logs(di["name"])
            for log in logs:
                messages = log.get("messages")
                rows = []
                try:
                    rows = json.loads(log.get("row_indexes")) if log.get("row_indexes") else []
                except Exception:
                    rows = []
                first_row = rows[0] if rows else None
                if log.get("success"):
                    if logger and log.get("docname"):
                        logger.row(first_row, str(log.get("docname")), "created",
                                   docname=log.get("docname"))
                    continue
                if messages:
                    try:
                        parsed = json.loads(messages) if isinstance(messages, str) else messages
                        texts = [m.get("message", "") for m in parsed]
                    except Exception:
                        texts = [str(messages)]
                    entry = {
                        "row": log.get("row_indexes"),
                        "docname": log.get("docname"),
                        "messages": texts,
                    }
                    result.errors.append(entry)
                    if logger:
                        logger.row(
                            first_row,
                            log.get("docname") or "",
                            "failed",
                            docname=log.get("docname"),
                            message="; ".join(texts),
                        )
            if logger:
                logger.log(
                    event="data_import_end", name=di["name"],
                    status=result.status, success=result.success, failed=result.failed,
                )
            return result
        finally:
            Path(tmp).unlink(missing_ok=True)


@dataclass
class RowResult:
    name: Optional[str]
    ok: bool
    skipped: bool = False
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "skipped": self.skipped,
            "error": self.error,
        }


class RestLoader:
    def __init__(self, client: ERPNextClient) -> None:
        self.client = client

    def upsert(
        self,
        doctype: str,
        payloads: list[dict],
        key_field: str,
        existing: Optional[set[str]] = None,
        submit: bool = False,
        delay: float = 0.0,
        logger: Optional[RunLogger] = None,
    ) -> list[RowResult]:
        """Insert payloads whose key is not in `existing`; log everything.

        Payloads are expected to be pre-deduped by erpgen.dedup; `existing` is
        a belt-and-braces second check against races.
        """
        existing = existing or set()
        results: list[RowResult] = []
        for payload in payloads:
            row = payload.pop("__row", None)
            payload.pop("__skip_reason", None)
            key = str(payload.get(key_field) or "").strip()
            if key and key in existing:
                results.append(RowResult(name=key, ok=True, skipped=True))
                if logger:
                    logger.row(row, key, "skipped", docname=key,
                               message="already exists (race guard)")
                continue
            try:
                doc = self.client.insert(doctype, payload)
                if submit:
                    doc = self.client.submit(doctype, doc["name"])
                results.append(RowResult(name=doc.get("name"), ok=True))
                if logger:
                    logger.row(row, key, "created", docname=doc.get("name"))
            except Exception as e:  # noqa: BLE001 — collect per-row failures
                results.append(RowResult(name=key or payload.get("name"), ok=False, error=str(e)))
                if logger:
                    logger.row(row, key, "failed", message=str(e))
            if delay:
                time.sleep(delay)
        return results
