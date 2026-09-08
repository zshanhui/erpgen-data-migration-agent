"""Minimal ERPNext REST API client — stdlib only (urllib + http.cookiejar).

Covers the surfaces verified against the local v16 demo:
  * session login (cookie jar)
  * resource CRUD: /api/resource/<Doctype> (GET/POST/PUT/DELETE)
  * submit/cancel via docstatus updates
  * whitelisted methods: /api/method/<module.path>  (POST, args in query)
  * file upload (multipart) and Data Import orchestration
  * DocType metadata introspection (the mapper's ground truth)
"""
from __future__ import annotations

import http.cookiejar
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


class ERPNextError(Exception):
    """Raised for HTTP/API failures, with the server's message included."""


def _quote(s: str) -> str:
    return urllib.parse.quote(s, safe="")


class ERPNextClient:
    def __init__(
        self,
        base_url: str,
        username: str = "Administrator",
        password: str = "admin",
        timeout: int = 60,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self._meta_cache: dict[str, dict] = {}
        self.login(username, password)

    # ------------------------------------------------------------- low level
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Any = None,
        form: Optional[dict] = None,
        files: Optional[dict[str, str]] = None,
        timeout: Optional[int] = None,
        raw: bool = False,
    ) -> Any:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers: dict[str, str] = {}
        body: Optional[bytes] = None

        if files:
            boundary = "----erpgen" + uuid4().hex
            buf: list[bytes] = []
            for field, filepath in files.items():
                p = Path(filepath)
                buf.append(f"--{boundary}\r\n".encode())
                buf.append(
                    f'Content-Disposition: form-data; name="{field}"; filename="{p.name}"\r\n'.encode()
                )
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                buf.append(f"Content-Type: {ctype}\r\n\r\n".encode())
                buf.append(p.read_bytes())
                buf.append(b"\r\n")
            for k, v in (form or {}).items():
                buf.append(f"--{boundary}\r\n".encode())
                buf.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
                buf.append(str(v).encode())
                buf.append(b"\r\n")
            buf.append(f"--{boundary}--\r\n".encode())
            body = b"".join(buf)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif form:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as resp:
                content = resp.read()
                if raw:
                    return content
                return json.loads(content) if content else None
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode(errors="replace")[:800]
            except Exception:
                pass
            raise ERPNextError(f"HTTP {e.code} {method} {path}: {detail}") from e

    def login(self, username: str, password: str) -> None:
        resp = self._request(
            "POST", "/api/method/login", form={"usr": username, "pwd": password}
        )
        if not (resp or {}).get("message"):
            raise ERPNextError(f"Login failed: {resp}")

    # ------------------------------------------------------------- resources
    def get(self, doctype: str, name: str) -> dict:
        resp = self._request("GET", f"/api/resource/{_quote(doctype)}/{_quote(name)}")
        return resp["data"]

    def list(
        self,
        doctype: str,
        filters: Optional[list] = None,
        fields: Optional[list] = None,
        limit: int = 0,
        order_by: Optional[str] = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit_page_length": limit or 0}
        if filters:
            params["filters"] = json.dumps(filters)
        if fields:
            params["fields"] = json.dumps(fields)
        if order_by:
            params["order_by"] = order_by
        resp = self._request("GET", f"/api/resource/{_quote(doctype)}", params=params)
        return resp.get("data", [])

    def insert(self, doctype: str, doc: dict) -> dict:
        resp = self._request(
            "POST", f"/api/resource/{_quote(doctype)}", json_body=doc
        )
        return resp["data"]

    def update(self, doctype: str, name: str, doc: dict) -> dict:
        resp = self._request(
            "PUT", f"/api/resource/{_quote(doctype)}/{_quote(name)}", json_body=doc
        )
        return resp["data"]

    def delete(self, doctype: str, name: str) -> None:
        self._request("DELETE", f"/api/resource/{_quote(doctype)}/{_quote(name)}")

    def submit(self, doctype: str, name: str) -> dict:
        return self.update(doctype, name, {"docstatus": 1})

    def cancel(self, doctype: str, name: str) -> dict:
        return self.update(doctype, name, {"docstatus": 2})

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        resp = self._request(
            "POST", f"/api/method/{method}", form=params or {}
        )
        return resp.get("message") if isinstance(resp, dict) else resp

    # ------------------------------------------------------------- metadata
    def doctype_meta(self, doctype: str) -> dict:
        if doctype not in self._meta_cache:
            self._meta_cache[doctype] = self.get("DocType", doctype)
        return self._meta_cache[doctype]

    # ------------------------------------------------------------- files
    def upload_file(self, local_path: str, is_private: int = 1) -> dict:
        resp = self._request(
            "POST",
            "/api/method/upload_file",
            files={"file": local_path},
            form={"is_private": str(is_private), "folder": "Home/Attachments"},
        )
        return resp["message"]

    # ------------------------------------------------------------- data import
    def download_template(
        self,
        doctype: str,
        export_fields: Optional[dict] = None,
        export_records: str = "blank_template",
        file_type: str = "CSV",
    ) -> bytes:
        params: dict[str, Any] = {
            "doctype": doctype,
            "export_records": export_records,
            "file_type": file_type,
        }
        if export_fields:
            params["export_fields"] = json.dumps(export_fields)
        return self._request(
            "GET",
            "/api/method/frappe.core.doctype.data_import.data_import.download_template",
            params=params,
            raw=True,
        )

    def create_data_import(
        self,
        doctype: str,
        file_url: str,
        import_type: str = "Insert New Records",
        submit_after_import: bool = False,
    ) -> dict:
        doc = {
            "reference_doctype": doctype,
            "import_type": import_type,
            "import_file": file_url,
            "submit_after_import": 1 if submit_after_import else 0,
        }
        return self.insert("Data Import", doc)

    def start_import(self, data_import_name: str) -> bool:
        return bool(
            self.call(
                "frappe.core.doctype.data_import.data_import.form_start_import",
                {"data_import": data_import_name},
            )
        )

    def import_status(self, data_import_name: str) -> dict:
        return self.call(
            "frappe.core.doctype.data_import.data_import.get_import_status",
            {"data_import_name": data_import_name},
        )

    def import_logs(self, data_import_name: str) -> list[dict]:
        return self.call(
            "frappe.core.doctype.data_import.data_import.get_import_logs",
            {"data_import": data_import_name},
        )

    def wait_for_import(
        self, data_import_name: str, timeout: int = 300, poll: float = 3.0
    ) -> dict:
        terminal = {"Success", "Error", "Timed Out"}
        elapsed = 0.0
        while elapsed < timeout:
            st = self.import_status(data_import_name)
            status = (st or {}).get("status")
            if status in terminal:
                return st or {}
            time.sleep(poll)
            elapsed += poll
        raise ERPNextError(
            f"Data import {data_import_name} did not finish within {timeout}s"
        )
