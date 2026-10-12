#!/usr/bin/env python3
"""List the flux lines in a folder of past close files, ready for picking a sample.

    python scan_history.py ~/flux-history --out candidates.csv

Reads every .csv, .tsv, .xlsx and .xlsm file under the folder (Excel files
need openpyxl), finds tables with an account column, prior and current
amounts and a commentary column, and writes one row per line that has
commentary:

    candidate_id, source, account, comparison_basis, prior, current, change, change_pct, commentary, repeats

A line without both amounts is left out: Jev reads the row's figures along
with the commentary, so it can't be labeled for calibration. The same
commentary appearing more than once is kept once, with the count in
`repeats`. Files that aren't tables (PDFs, Word files, notes) are listed for
Claude to read directly.

Reads only. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

TABLES = {".csv", ".tsv", ".xlsx", ".xlsm"}
DOCUMENTS = {".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".pptx", ".eml", ".msg"}
HEADER_SCAN_ROWS = 15

# In priority order: the first header that matches wins.
SYNONYMS = {
    "account": ("account", "account name", "gl account", "gl account name", "account description",
                "line item", "fs line", "financial statement line", "caption"),
    "prior": ("prior", "prior period", "prior month", "prior quarter", "prior year", "previous",
              "previous period", "previous month", "last month", "last quarter", "last year", "py", "pm",
              "budget", "plan", "forecast"),
    "current": ("current", "current period", "current month", "current quarter", "current year", "this month",
                "this quarter", "actual", "actuals", "cy", "cm"),
    "commentary": ("commentary", "comment", "comments", "explanation", "variance explanation",
                   "flux commentary", "flux explanation", "narrative", "notes", "note", "reason", "analysis"),
    "basis": ("comparison basis", "basis", "comparison"),
}
BASIS_FROM_PRIOR = (
    (("budget", "plan"), "vs Budget"),
    (("forecast",), "vs Forecast"),
    (("prior year", "last year", "py"), "YoY"),
    (("prior quarter", "last quarter"), "QoQ"),
)


def header_key(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%$ ]+", " ", str(value or "").lower())).strip()


def parse_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in ("n/a", "na", "-", "none"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"[,$\s]", "", text.strip("()"))
    try:
        number = float(text.rstrip("%"))
    except ValueError:
        return None
    if text.endswith("%"):
        number /= 100.0
    return -number if negative else number


def number_text(value: float) -> str:
    text = format(value, ".15g")
    return "0" if text == "-0" else text


def find_columns(row: Sequence[Any]) -> Optional[Dict[str, int]]:
    keys = [header_key(v) for v in row]
    found: Dict[str, int] = {}
    for role, names in SYNONYMS.items():
        for name in names:
            if name in keys:
                found[role] = keys.index(name)
                break
    if all(role in found for role in ("account", "prior", "current", "commentary")):
        return found
    return None


def cell(row: Sequence[Any], index: Optional[int]) -> Any:
    return row[index] if index is not None and index < len(row) else None


def basis_for(row: Sequence[Any], columns: Dict[str, int], prior_header: str) -> str:
    value = str(cell(row, columns.get("basis")) or "").strip()
    if value:
        return value
    for names, basis in BASIS_FROM_PRIOR:
        if prior_header in names:
            return basis
    return "MoM"


def tables_in(path: Path) -> Iterator[Tuple[str, List[List[Any]]]]:
    """(label, rows) for each sheet or delimited file."""
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as handle:
            yield path.name, [list(r) for r in csv.reader(handle, delimiter="\t" if suffix == ".tsv" else ",")]
        return
    try:
        import openpyxl  # type: ignore
    except ImportError:
        raise RuntimeError("openpyxl is not installed (pip install openpyxl), so Excel files can't be read")
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            yield "{0} > {1}".format(path.name, sheet.title), [list(r) for r in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def lines_in(label: str, rows: List[List[Any]]) -> Tuple[List[Dict[str, str]], int]:
    """Usable lines in one table, and how many had commentary but no figures."""
    for header_index, header in enumerate(rows[:HEADER_SCAN_ROWS]):
        columns = find_columns(header)
        if columns:
            break
    else:
        return [], 0
    prior_header = header_key(header[columns["prior"]])
    out: List[Dict[str, str]] = []
    no_figures = 0
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        commentary = str(cell(row, columns["commentary"]) or "").strip()
        if not commentary:
            continue
        prior, current = parse_number(cell(row, columns["prior"])), parse_number(cell(row, columns["current"]))
        account = str(cell(row, columns["account"]) or "").strip()
        if prior is None or current is None or not account:
            no_figures += 1
            continue
        change = current - prior
        out.append({
            "source": "{0} row {1}".format(label, offset),
            "account": account,
            "comparison_basis": basis_for(row, columns, prior_header),
            "prior": number_text(prior),
            "current": number_text(current),
            "change": number_text(change),
            "change_pct": "" if prior == 0 else "{0:.1%}".format(change / abs(prior)),
            "commentary": commentary,
        })
    return out, no_figures


def scan(folder: Path) -> Dict[str, Any]:
    candidates: List[Dict[str, str]] = []
    by_text: Dict[str, Dict[str, str]] = {}
    report: Dict[str, Any] = {"tables": [], "documents": [], "unreadable": [], "no_figures": 0}
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and not p.name.startswith((".", "~$"))):
        suffix = path.suffix.lower()
        if suffix in DOCUMENTS:
            report["documents"].append(str(path.relative_to(folder)))
            continue
        if suffix not in TABLES:
            continue
        try:
            for label, rows in tables_in(path):
                found, no_figures = lines_in(label, rows)
                report["no_figures"] += no_figures
                if found or no_figures:
                    report["tables"].append((label, len(found)))
                for line in found:
                    key = re.sub(r"\s+", " ", line["commentary"]).strip().lower()
                    if key in by_text:
                        by_text[key]["repeats"] = str(int(by_text[key]["repeats"]) + 1)
                        continue
                    line["repeats"] = "1"
                    by_text[key] = line
                    candidates.append(line)
        except Exception as exc:  # noqa: BLE001 - a bad file is reported, not fatal
            report["unreadable"].append("{0}: {1}".format(path.relative_to(folder), exc))
    for number, line in enumerate(candidates, start=1):
        line["candidate_id"] = "H{0:04d}".format(number)
    report["candidates"] = candidates
    return report


COLUMNS = ["candidate_id", "source", "account", "comparison_basis", "prior", "current", "change", "change_pct",
           "commentary", "repeats"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--out", type=Path, default=Path("candidates.csv"))
    args = parser.parse_args(argv)
    if not args.folder.is_dir():
        print("{0} is not a folder".format(args.folder), file=sys.stderr)
        return 1
    report = scan(args.folder)
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: line.get(k, "") for k in COLUMNS} for line in report["candidates"])
    for label, count in report["tables"]:
        print("table  {0}: {1} line(s) with commentary and figures".format(label, count))
    for name in report["documents"]:
        print("read   {0} (not a table; read it directly)".format(name))
    for problem in report["unreadable"]:
        print("skip   {0}".format(problem))
    if report["no_figures"]:
        print("{0} line(s) had commentary but not both amounts, so they can't be labeled".format(
            report["no_figures"]))
    repeated = sum(1 for line in report["candidates"] if line["repeats"] != "1")
    print("RAN: {0} candidate line(s) in {1}{2}".format(
        len(report["candidates"]), args.out,
        "; {0} commentary text(s) appear more than once".format(repeated) if repeated else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
