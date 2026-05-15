"""
reporting/report.py — Markdown report generation for kontocheck

Pure rendering from MatchResult objects — no LLM, no DB access.

Sections produced (in order):
  1. Header — source filename, analysis timestamp, count summary
  2. Transactions — chronological table of every entry
  3. ⚠️ Items requiring attention — one subsection per non-green entry
  4. ❌ Unmatched transactions — list of NO_MATCH entries
  5. Statistics — counts per status and total matched/unmatched amounts

Filename rules (TECHNICAL_SPEC §7.3):
  - Statement period derived from the earliest transaction's year-month:
        kontocheck-2026-04.md
  - Fallback to timestamp when results are empty (period cannot be derived):
        kontocheck-20260508-143200.md
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import config
from pipeline.matcher import (
    MatchResult,
    STATUS_DISPLAY,
    MATCHED,
    MATCHED_LARGE_DELAY,
    MATCHED_UNUSUAL_DELAY,
    MATCHED_UNREVIEWED,
    AMOUNT_MISMATCH,
    NO_MATCH,
)

logger = logging.getLogger(__name__)


# Group statuses for the count summary in the header
_WARNING_STATUSES = {
    MATCHED_LARGE_DELAY,
    MATCHED_UNUSUAL_DELAY,
    MATCHED_UNREVIEWED,
    AMOUNT_MISMATCH,
}


# ── Formatting helpers ────────────────────────────────────────────────────────

def _sanitize_cell(text: str) -> str:
    """Escape characters that break a Markdown table cell."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("|", r"\|")
        .strip()
    )


def _format_amount(amount: Decimal) -> str:
    return f"€{amount:.2f}"


def _sort_by_date(results: list[MatchResult]) -> list[MatchResult]:
    return sorted(results, key=lambda r: r.transaction.date)


# ── Section builders ──────────────────────────────────────────────────────────

def _statement_period(results: list[MatchResult]) -> str:
    """Render the period shown in the report title."""
    if not results:
        return datetime.now().strftime("%Y-%m")
    dates = [r.transaction.date for r in results]
    earliest, latest = min(dates), max(dates)
    if (earliest.year, earliest.month) == (latest.year, latest.month):
        return earliest.strftime("%Y-%m")
    return f"{earliest.strftime('%Y-%m')} – {latest.strftime('%Y-%m')}"


def _build_header(results: list[MatchResult], source_file: Path) -> list[str]:
    total = len(results)
    matched_count = sum(1 for r in results if r.status == MATCHED)
    warning_count = sum(1 for r in results if r.status in _WARNING_STATUSES)
    nomatch_count = sum(1 for r in results if r.status == NO_MATCH)

    return [
        f"# kontocheck — Statement {_statement_period(results)}",
        "",
        f"**Source file:** `{source_file.name}`",
        f"**Analysed:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Transactions:** {total} total  ✅ {matched_count}  "
        f"⚠️ {warning_count}  ❌ {nomatch_count}",
        "",
        "---",
        "",
    ]


def _row_details(r: MatchResult) -> str:
    """Build the 'Details' table cell: matched name, file, and any notes."""
    parts: list[str] = []
    if r.matched_name:
        parts.append(r.matched_name)
    if r.matched_file:
        parts.append(f"`{r.matched_file}`")
    head = " — ".join(parts)
    if r.notes:
        notes_str = "; ".join(r.notes)
        return f"{head} — {notes_str}" if head else notes_str
    return head


def _build_table(results: list[MatchResult]) -> list[str]:
    lines = [
        "## Transactions",
        "",
        "| Date | Description | Amount | Direction | Status | Details |",
        "|------|-------------|--------|-----------|--------|---------|",
    ]
    for r in _sort_by_date(results):
        tx = r.transaction
        lines.append(
            "| {date} | {desc} | {amount} | {direction} | {status} | {details} |".format(
                date=tx.date.isoformat(),
                desc=_sanitize_cell(tx.description),
                amount=_format_amount(tx.amount),
                direction=tx.direction,
                status=STATUS_DISPLAY.get(r.status, r.status),
                details=_sanitize_cell(_row_details(r)),
            )
        )
    lines.extend(["", "---", ""])
    return lines


def _build_attention(results: list[MatchResult]) -> list[str]:
    items = [r for r in _sort_by_date(results) if r.status in _WARNING_STATUSES]
    if not items:
        return []

    lines = ["## ⚠️ Items requiring attention", ""]
    for r in items:
        tx = r.transaction
        lines.append(f"### {tx.date.isoformat()} — {tx.description}")
        lines.append("")
        lines.append(f"- **Status:** {STATUS_DISPLAY.get(r.status, r.status)}")
        lines.append(f"- **Amount:** {_format_amount(tx.amount)} ({tx.direction})")
        if r.matched_name:
            lines.append(f"- **Matched:** {r.matched_name}")
        if r.matched_file:
            lines.append(f"- **File:** `{r.matched_file}`")
        if r.date_gap_days is not None:
            lines.append(f"- **Date gap:** {r.date_gap_days} days")
        for note in r.notes:
            lines.append(f"- **Note:** {note}")
        lines.append("")
    lines.extend(["---", ""])
    return lines


def _build_unmatched(results: list[MatchResult]) -> list[str]:
    items = [r for r in _sort_by_date(results) if r.status == NO_MATCH]
    if not items:
        return []

    lines = ["## ❌ Unmatched transactions", ""]
    for r in items:
        tx = r.transaction
        lines.append(
            f"- **{tx.date.isoformat()}** — {tx.description} — "
            f"{_format_amount(tx.amount)} ({tx.direction})"
        )
    lines.extend(["", "---", ""])
    return lines


def _build_statistics(results: list[MatchResult]) -> list[str]:
    counts = {status: 0 for status in STATUS_DISPLAY}
    matched_total = Decimal("0")
    unmatched_total = Decimal("0")

    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status == NO_MATCH:
            unmatched_total += r.transaction.amount
        else:
            matched_total += r.transaction.amount

    lines = ["## Statistics", ""]
    for status, display in STATUS_DISPLAY.items():
        lines.append(f"- {display}: {counts.get(status, 0)}")
    lines.append("")
    lines.append(f"**Total matched amount:** {_format_amount(matched_total)}")
    lines.append(f"**Total unmatched amount:** {_format_amount(unmatched_total)}")
    lines.append("")
    return lines


# ── Filename and output path ──────────────────────────────────────────────────

def _determine_output_path(results: list[MatchResult]) -> Path:
    """
    Build the destination path for the report file. Uses the earliest
    transaction month when available; falls back to a timestamp when the
    period cannot be determined (empty result list).
    """
    if results:
        earliest = min(r.transaction.date for r in results)
        filename = f"kontocheck-{earliest.strftime('%Y-%m')}.md"
    else:
        filename = f"kontocheck-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    return config.OUTPUT_FOLDER / filename


# ── Public interface ──────────────────────────────────────────────────────────

def generate(results: list[MatchResult], source_file: Path) -> Path:
    """
    Render a Markdown reconciliation report for the given match results,
    save it to OUTPUT_FOLDER, and return the path written.
    """
    config.ensure_folders()

    lines: list[str] = []
    lines.extend(_build_header(results, source_file))
    lines.extend(_build_table(results))
    lines.extend(_build_attention(results))
    lines.extend(_build_unmatched(results))
    lines.extend(_build_statistics(results))

    output_path = _determine_output_path(results)
    output_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")

    logger.info("Report written to %s", output_path)
    return output_path
