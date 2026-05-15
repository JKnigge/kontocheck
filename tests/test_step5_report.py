"""
tests/test_step5_report.py — manual test script for reporting/report.py

Builds synthetic MatchResult objects covering all six status verdicts and
renders a real Markdown report into a temporary output folder. Checks the
file is written, each major section renders, and chronological ordering
is preserved.

The test mocks config, pipeline.extractor, storage.db_client and ollama
so it runs without a live .env, database, or Ollama instance.

Run from the project root:
    python tests/test_step5_report.py
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import types
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is on the path regardless of where the script is launched
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Set up an isolated output folder for the test ─────────────────────────────

TEMP_OUTPUT = Path(tempfile.mkdtemp(prefix="kontocheck_report_test_"))

# ── Mock config ───────────────────────────────────────────────────────────────

mock_config = types.ModuleType("config")
mock_config.OLLAMA_URL          = "http://localhost:11434"
mock_config.OLLAMA_MODEL        = "test-model"
mock_config.DATE_TIER1_DAYS     = 5
mock_config.DATE_TIER2_DAYS     = 14
mock_config.REGPAYMENT_USER_ID  = 1
mock_config.OUTPUT_FOLDER       = TEMP_OUTPUT
mock_config.ensure_folders      = lambda: TEMP_OUTPUT.mkdir(parents=True, exist_ok=True)
sys.modules["config"] = mock_config

# ── Mock pipeline.extractor ───────────────────────────────────────────────────

@dataclass
class Transaction:
    date:        date
    description: str
    amount:      Decimal
    direction:   str
    raw_text:    str = ""

mock_extractor = types.ModuleType("pipeline.extractor")
mock_extractor.Transaction = Transaction
sys.modules["pipeline.extractor"] = mock_extractor

# ── Mock storage.db_client ────────────────────────────────────────────────────

mock_db = MagicMock()
mock_storage = types.ModuleType("storage")
mock_storage.db_client = mock_db
sys.modules["storage"] = mock_storage
sys.modules["storage.db_client"] = mock_db

# ── Load matcher (uses ollama.Client at module load) ──────────────────────────

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_matcher_path = os.path.join(_root, "pipeline", "matcher.py")
_spec_m = importlib.util.spec_from_file_location("pipeline.matcher", _matcher_path)
matcher = importlib.util.module_from_spec(_spec_m)
sys.modules["pipeline.matcher"] = matcher
with patch("ollama.Client"):
    _spec_m.loader.exec_module(matcher)

# ── Load reporting.report ─────────────────────────────────────────────────────

_report_path = os.path.join(_root, "reporting", "report.py")
_spec_r = importlib.util.spec_from_file_location("reporting.report", _report_path)
report = importlib.util.module_from_spec(_spec_r)
sys.modules["reporting.report"] = report
_spec_r.loader.exec_module(report)


# ── Test harness ──────────────────────────────────────────────────────────────

passed = 0
failed = 0


def check(description: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {description}")
    else:
        failed += 1
        print(f"  FAIL  {description}")
        if detail:
            print(f"        → {detail}")


def section(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── Synthetic MatchResults covering all six status types ─────────────────────

def make_tx(description, amount, direction="debit", tx_date=date(2024, 4, 15)):
    return Transaction(
        date=tx_date,
        description=description,
        amount=Decimal(str(amount)),
        direction=direction,
    )


results = [
    matcher.MatchResult(
        transaction=make_tx("REWE SAGT DANKE", 43.20, tx_date=date(2024, 4, 5)),
        status=matcher.MATCHED,
        matched_source="receipt",
        matched_id=1,
        matched_name="REWE GmbH",
        matched_file="20240405-REWE.pdf",
        date_gap_days=2,
    ),
    matcher.MatchResult(
        transaction=make_tx("TELEKOM DEUTSCHLAND", 39.99, tx_date=date(2024, 4, 10)),
        status=matcher.MATCHED_LARGE_DELAY,
        matched_source="receipt",
        matched_id=2,
        matched_name="Telekom",
        matched_file="20240320-Telekom.pdf",
        date_gap_days=10,
        notes=["Date gap: 10 days between receipt date and bank booking"],
    ),
    matcher.MatchResult(
        transaction=make_tx("AMAZON PAYMENTS", 29.99, tx_date=date(2024, 4, 12)),
        status=matcher.MATCHED_UNUSUAL_DELAY,
        matched_source="receipt",
        matched_id=3,
        matched_name="Amazon EU SARL",
        matched_file="20240310-Amazon.pdf",
        date_gap_days=33,
        notes=["Date gap: 33 days between receipt date and bank booking"],
    ),
    matcher.MatchResult(
        transaction=make_tx("UNBEKANNT REF 123", 15.00, tx_date=date(2024, 4, 18)),
        status=matcher.MATCHED_UNREVIEWED,
        matched_source="receipt",
        matched_id=4,
        matched_name="Unbekannt GmbH",
        matched_file="20240416-Unbekannt.pdf",
        date_gap_days=2,
        notes=["Receipt was flagged by belegbot and may not have been manually reviewed"],
    ),
    matcher.MatchResult(
        transaction=make_tx("TELEKOM MOBILFUNK", 42.99, tx_date=date(2024, 4, 22)),
        status=matcher.AMOUNT_MISMATCH,
        matched_source="regpayment",
        matched_id=12,
        matched_name="Handyvertrag",
        notes=["Amount mismatch: expected €39.99, actual €42.99 — update regpayment table if correct"],
    ),
    matcher.MatchResult(
        transaction=make_tx("UNBEKANNTE BUCHUNG", 7.50, tx_date=date(2024, 4, 25)),
        status=matcher.NO_MATCH,
    ),
]

source_file = Path("statement-2024-04.pdf")


try:
    # ── Generate report ─────────────────────────────────────────────────────

    section("Generating report")
    output_path = report.generate(results, source_file)
    print(f"  Wrote: {output_path}")

    check("output file exists",                output_path.exists())
    check("filename is kontocheck-2024-04.md", output_path.name == "kontocheck-2024-04.md",
          f"got: {output_path.name}")

    text = output_path.read_text(encoding="utf-8")

    # ── Header ──────────────────────────────────────────────────────────────

    section("Header")
    check("title with period",       "# kontocheck — Statement 2024-04" in text)
    check("source file referenced",  "statement-2024-04.pdf" in text)
    check("analysed timestamp",      "**Analysed:**" in text)
    check("✅ count = 1",            "✅ 1" in text)
    check("⚠️ count = 4",            "⚠️ 4" in text)
    check("❌ count = 1",            "❌ 1" in text)

    # ── Transaction table ───────────────────────────────────────────────────

    section("Transaction table")
    check("transactions heading",    "## Transactions" in text)
    check("table header row",
          "| Date | Description | Amount | Direction | Status | Details |" in text)
    check("REWE row with amount",    "REWE SAGT DANKE" in text and "€43.20" in text)
    check("debit direction shown",   "| debit |" in text)
    check("credit not falsely shown when none exist", "| credit |" not in text)
    check("matched file shown inline","`20240405-REWE.pdf`" in text)

    # ── Attention section ───────────────────────────────────────────────────

    section("Attention section")
    check("attention heading",       "## ⚠️ Items requiring attention" in text)
    check("large delay subsection",  "### 2024-04-10 — TELEKOM DEUTSCHLAND" in text)
    check("unusual delay subsection","### 2024-04-12 — AMAZON PAYMENTS" in text)
    check("unreviewed subsection",   "### 2024-04-18 — UNBEKANNT REF 123" in text)
    check("amount_mismatch subsection","### 2024-04-22 — TELEKOM MOBILFUNK" in text)
    check("date gap rendered",       "**Date gap:** 33 days" in text)
    check("expected/actual rendered","expected €39.99" in text and "actual €42.99" in text)
    check("unreviewed note carried", "manually reviewed" in text)

    # ── Unmatched section ───────────────────────────────────────────────────

    section("Unmatched section")
    check("unmatched heading",       "## ❌ Unmatched transactions" in text)
    check("unmatched item listed",   "UNBEKANNTE BUCHUNG" in text and "€7.50" in text)

    # ── Statistics ──────────────────────────────────────────────────────────

    section("Statistics")
    check("statistics heading",      "## Statistics" in text)
    check("matched count line",      "✅ Matched: 1" in text)
    check("large delay count line",  "⚠️ Matched — large delay: 1" in text)
    check("unusual delay count line","⚠️ Matched — unusual delay: 1" in text)
    check("unreviewed count line",   "⚠️ Matched — please verify: 1" in text)
    check("amount mismatch count",   "⚠️ Amount mismatch: 1" in text)
    check("no match count",          "❌ No match found: 1" in text)
    check("total matched amount",
          f"**Total matched amount:** €{Decimal('43.20') + Decimal('39.99') + Decimal('29.99') + Decimal('15.00') + Decimal('42.99'):.2f}" in text)
    check("total unmatched amount",  "**Total unmatched amount:** €7.50" in text)

    # ── Chronological ordering in the table ────────────────────────────────

    section("Chronological ordering")
    table_part = text.split("## Transactions")[1].split("---")[0]
    table_rows = [ln for ln in table_part.splitlines() if ln.startswith("| 2024-")]
    dates_in_table = [row.split("|")[1].strip() for row in table_rows]
    check("table rows in date order",
          dates_in_table == sorted(dates_in_table),
          f"got: {dates_in_table}")

    # ── Empty results edge case ────────────────────────────────────────────

    section("Empty results")
    empty_path = report.generate([], Path("empty.pdf"))
    check("empty results: file written",  empty_path.exists())
    check("empty results: timestamp filename",
          empty_path.name.startswith("kontocheck-") and len(empty_path.stem) > len("kontocheck-2024-04"),
          f"got: {empty_path.name}")

    # ── Description with pipe character is escaped ─────────────────────────

    section("Cell sanitization")
    pipe_results = [
        matcher.MatchResult(
            transaction=make_tx("WEIRD | DESC", 1.00, tx_date=date(2024, 5, 1)),
            status=matcher.NO_MATCH,
        ),
    ]
    pipe_path = report.generate(pipe_results, Path("pipe.pdf"))
    pipe_text = pipe_path.read_text(encoding="utf-8")
    # The pipe-containing row must contain an escaped pipe so the column count
    # isn't corrupted by the description text.
    check("pipe in description is escaped",
          r"WEIRD \| DESC" in pipe_text,
          "pipe character was not escaped — Markdown table would be malformed")

finally:
    # Clean up the temp output folder
    shutil.rmtree(TEMP_OUTPUT, ignore_errors=True)


# ── Summary ───────────────────────────────────────────────────────────────────

total = passed + failed
print(f"\n{'═' * 60}")
print(f"  Results: {passed}/{total} passed", end="")
if failed:
    print(f"  —  {failed} FAILED  ←")
else:
    print("  — all tests passed ✅")
print(f"{'═' * 60}\n")

sys.exit(0 if failed == 0 else 1)
