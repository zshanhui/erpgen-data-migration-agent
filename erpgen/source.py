"""Source file readers: CSV (stdlib) and XLSX (optional openpyxl).

Produces a SourceTable with per-column profiles the mapper can reason over
(sample values, emptiness, inferred type, messiness flags).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DATE_PATTERNS = [
    (re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$"), "%Y-%m-%d"),
    (re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$"), "%Y/%m/%d"),
    (re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"), "%m/%d/%Y"),
    (re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$"), "%m-%d-%Y"),
    (re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$"), "%d.%m.%Y"),
]


@dataclass
class ColumnProfile:
    header: str
    non_empty: float  # 0..1
    unique: float  # 0..1 of non-empty
    sample: list  # up to 5 distinct values
    inferred_type: str = "text"  # int | float | date | bool | text
    messy: list[str] = field(default_factory=list)  # flags

    def as_dict(self) -> dict:
        return {
            "header": self.header,
            "non_empty": round(self.non_empty, 3),
            "unique": round(self.unique, 3),
            "sample": self.sample[:5],
            "inferred_type": self.inferred_type,
            "messy": self.messy,
        }


@dataclass
class SourceTable:
    name: str
    headers: list[str]
    rows: list[list]  # raw cells (strings preferred)
    profiles: list[ColumnProfile] = field(default_factory=list)
    #: source line number (1 = header) of each row, when rows were filtered or
    #: corrected so that a row's position no longer implies its line number
    row_numbers: Optional[list[int]] = None

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    def row_number(self, index: int) -> int:
        """The source line number of `rows[index]` (1 = header).

        Detection, logging and `{"row": N}` corrections all speak this
        coordinate, so it has to survive a `skip_row` without shifting.
        """
        return self.row_numbers[index] if self.row_numbers else index + 2

    def column_index(self, header: str) -> Optional[int]:
        try:
            return self.headers.index(header)
        except ValueError:
            return None

    def build_profiles(self) -> "SourceTable":
        n = len(self.rows) or 1
        for i, h in enumerate(self.headers):
            values = [r[i] for r in self.rows if i < len(r)]
            non_empty_vals = [v for v in values if v not in ("", None)]
            seen = set()
            uniq = 0
            sample: list = []
            for v in non_empty_vals:
                key = str(v)
                if key not in seen:
                    seen.add(key)
                    uniq += 1
                    if len(sample) < 5:
                        sample.append(v)
            prof = ColumnProfile(
                header=h,
                non_empty=len(non_empty_vals) / n,
                unique=(uniq / len(non_empty_vals)) if non_empty_vals else 0.0,
                sample=sample,
            )
            if non_empty_vals:
                prof.inferred_type, prof.messy = _infer(non_empty_vals)
            self.profiles.append(prof)
        return self


# --------------------------------------------------------------- inference
def _infer(values: list) -> tuple[str, list[str]]:
    messy: list[str] = []
    text = [str(v) for v in values if str(v).strip()]

    # currency / numeric flags
    currency_re = re.compile(r"[$€£]\s?[\d.,]+|[\d.,]+\s?(USD|EUR|GBP|CNY|RMB)")
    comma_decimal_re = re.compile(r"^\d{1,3}(\.\d{3})*,\d{2}$")
    if any(currency_re.search(v) for v in text):
        messy.append("currency_symbol")
    if any(comma_decimal_re.match(v) for v in text):
        messy.append("comma_as_decimal")

    # type
    if all(_is_int(v) for v in text):
        return "int", messy
    if all(_is_float(v) for v in text):
        return "float", messy
    if all(_is_bool(v) for v in text):
        return "bool", messy
    if all(_is_date(v) for v in text):
        return "date", messy
    if any(len(v) != len(v.strip()) for v in text):
        messy.append("whitespace")
    return "text", messy


def _is_int(v: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\d[\d,]*", v.strip()))


def _is_float(v: str) -> bool:
    s = v.strip().replace(",", "")
    try:
        float(s)
        return "." in s or "e" in s.lower()
    except ValueError:
        return False


def _is_bool(v: str) -> bool:
    return v.strip().lower() in ("yes", "no", "true", "false", "y", "n", "0", "1")


def _is_date(v: str) -> bool:
    s = v.strip()
    return any(p.match(s) for p, _ in DATE_PATTERNS)


# --------------------------------------------------------------- readers
def read_csv(path: str | Path) -> SourceTable:
    p = Path(path)
    with p.open("r", encoding="utf-8-sig", errors="replace") as fh:
        content = fh.read()
    delimiter = _sniff_delimiter(content)
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise ValueError(f"No data rows found in {p.name}")
    headers = [h.strip() for h in rows[0]]
    data = rows[1:]
    return SourceTable(name=p.name, headers=headers, rows=data).build_profiles()


def _sniff_delimiter(content: str) -> str:
    first = content.splitlines()[0] if content.splitlines() else ""
    for d in ("\t", ";", ","):
        if d in first:
            return d
    return ","


def read_xlsx(path: str | Path) -> SourceTable:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        raise ImportError(
            "Reading .xlsx requires openpyxl: pip install openpyxl  "
            "(or convert the file to CSV)"
        )
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    raw_rows: list[list] = []
    for row in ws.iter_rows(values_only=True):
        cells = [_cell_to_str(c) for c in row]
        if any(c.strip() for c in cells):
            raw_rows.append(cells)
    if not raw_rows:
        raise ValueError(f"No data rows found in {Path(path).name}")
    headers = [h.strip() for h in raw_rows[0]]
    return SourceTable(
        name=Path(path).name, headers=headers, rows=raw_rows[1:]
    ).build_profiles()


def _cell_to_str(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, dt.datetime):
        return cell.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(cell, (dt.date,)):
        return cell.strftime("%Y-%m-%d")
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell)


def read_source(path: str | Path) -> SourceTable:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".csv":
        return read_csv(p)
    if suffix in (".xlsx", ".xlsm"):
        return read_xlsx(p)
    raise ValueError(f"Unsupported source format: {suffix} (use .csv or .xlsx)")
