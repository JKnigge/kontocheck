"""
manual_tests/test_matcher.py — manual test script for pipeline/matcher.py

Tests all six status paths and the 1-to-1 constraint without requiring
a real database or Ollama connection. Both dependencies are mocked so
the test is fast, deterministic, and runnable in isolation.

Run from the project root:
    python manual_tests/test_matcher.py

Each test prints PASS or FAIL with a description of what was checked.
A final summary line shows total passed/failed.
"""

import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

# Ensure project root is on the path regardless of where the script is launched
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Minimal config mock ───────────────────────────────────────────────────────
# Must be in place before importing matcher, since matcher imports config
# at module level to create the Ollama client.

import types
mock_config = types.ModuleType("config")
mock_config.OLLAMA_URL        = "http://localhost:11434"
mock_config.OLLAMA_MODEL      = "test-model"
mock_config.DATE_TIER1_DAYS   = 5
mock_config.DATE_TIER2_DAYS   = 14
mock_config.REGPAYMENT_USER_ID = 1
sys.modules["config"] = mock_config

# ── Mock pipeline.extractor ───────────────────────────────────────────────────
# We define Transaction locally and register it under the module name that
# matcher.py imports from, so Python finds our version instead of the real one.

from dataclasses import dataclass

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

# ── Now safe to import matcher directly ───────────────────────────────────────
# We import the file directly using importlib so we bypass the pipeline package
# entirely — avoiding the conflict between our mocked pipeline.extractor and
# the real pipeline package on disk.

import importlib.util

_matcher_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "pipeline", "matcher.py",
)
_spec = importlib.util.spec_from_file_location("pipeline.matcher", _matcher_path)
_mod  = importlib.util.module_from_spec(_spec)

with patch("ollama.Client"):
    _spec.loader.exec_module(_mod)

matcher = _mod
# Point matcher's db_client reference at our mock
matcher.db_client = mock_db


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


# ── Shared test fixtures ──────────────────────────────────────────────────────

def make_receipt(
    id: int,
    issuer: str,
    amount: float,
    days_before_bank: int,
    confidence: str = "high",
    manually_checked = 1,
    file_name: str = "20240101-Test.pdf",
) -> dict:
    """Build a fake receipts table row."""
    bank_date = date(2024, 4, 15)
    receipt_date = bank_date - timedelta(days=days_before_bank)
    return {
        "id": id,
        "issuer": issuer,
        "receipt_date": receipt_date,
        "total_amount": Decimal(str(amount)),
        "confidence": confidence,
        "manually_checked": manually_checked,
        "file_name": file_name,
    }


def make_regpayment(
    id: int,
    reason: str,
    amount_cents: int,
    start_date: date = date(2023, 1, 1),
    end_date = None,
) -> dict:
    """Build a fake regpayment table row."""
    return {
        "id": id,
        "reason": reason,
        "amount": amount_cents,
        "startDate": start_date,
        "endDate": end_date,
    }


def make_tx(
    description: str,
    amount: float,
    direction: str = "debit",
    tx_date: date = date(2024, 4, 15),
) -> Transaction:
    return Transaction(
        date=tx_date,
        description=description,
        amount=Decimal(str(amount)),
        direction=direction,
    )


# ── Test 1: MATCHED (receipt, within tier 1) ──────────────────────────────────

section("Test 1 — MATCHED: receipt found within tier 1 date window")

mock_db.get_receipt_candidates.return_value = [
    make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=3)
]
mock_db.get_regpayment_candidates.return_value = []
mock_db.get_regpayment_candidates_by_date.return_value = []

with patch.object(matcher, "_check_name_similarity", return_value="match"):
    results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])

r = results[0]
check("status is MATCHED",         r.status == matcher.MATCHED,    f"got: {r.status}")
check("matched_source is receipt", r.matched_source == "receipt",  f"got: {r.matched_source}")
check("matched_id is 1",           r.matched_id == 1,              f"got: {r.matched_id}")
check("matched_name is REWE GmbH", r.matched_name == "REWE GmbH", f"got: {r.matched_name}")
check("date_gap_days is 3",        r.date_gap_days == 3,           f"got: {r.date_gap_days}")
check("no notes",                  r.notes == [],                  f"got: {r.notes}")


# ── Test 2: MATCHED_LARGE_DELAY (receipt, tier 2) ────────────────────────────

section("Test 2 — MATCHED_LARGE_DELAY: receipt found but date gap in tier 2")

mock_db.get_receipt_candidates.return_value = [
    make_receipt(id=2, issuer="Telekom", amount=39.99, days_before_bank=10)
]

with patch.object(matcher, "_check_name_similarity", return_value="match"):
    results = matcher.match_all([make_tx("TELEKOM DEUTSCHLAND", 39.99)])

r = results[0]
check("status is MATCHED_LARGE_DELAY", r.status == matcher.MATCHED_LARGE_DELAY, f"got: {r.status}")
check("date_gap_days is 10",           r.date_gap_days == 10,                    f"got: {r.date_gap_days}")
check("date gap noted",                any("10 days" in n for n in r.notes),     f"got: {r.notes}")


# ── Test 3: MATCHED_UNUSUAL_DELAY (receipt, beyond tier 2) ───────────────────

section("Test 3 — MATCHED_UNUSUAL_DELAY: receipt found but date gap beyond tier 2")

mock_db.get_receipt_candidates.return_value = [
    make_receipt(id=3, issuer="Amazon", amount=29.99, days_before_bank=20)
]

with patch.object(matcher, "_check_name_similarity", return_value="match"):
    results = matcher.match_all([make_tx("AMAZON PAYMENTS", 29.99)])

r = results[0]
check("status is MATCHED_UNUSUAL_DELAY", r.status == matcher.MATCHED_UNUSUAL_DELAY, f"got: {r.status}")
check("date_gap_days is 20",             r.date_gap_days == 20,                      f"got: {r.date_gap_days}")


# ── Test 4: MATCHED_UNREVIEWED (receipt flagged by belegbot) ─────────────────

section("Test 4 — MATCHED_UNREVIEWED: receipt matched but not manually reviewed")

mock_db.get_receipt_candidates.return_value = [
    make_receipt(
        id=4, issuer="Unbekannt GmbH", amount=15.00, days_before_bank=2,
        confidence="low", manually_checked=None,
    )
]

with patch.object(matcher, "_check_name_similarity", return_value="match"):
    results = matcher.match_all([make_tx("UNBEKANNT REF 123", 15.00)])

r = results[0]
check("status is MATCHED_UNREVIEWED",  r.status == matcher.MATCHED_UNREVIEWED,           f"got: {r.status}")
check("unreviewed note present",       any("manually reviewed" in n for n in r.notes),   f"got: {r.notes}")


# ── Test 5: MATCHED via regpayment ───────────────────────────────────────────

section("Test 5 — MATCHED: regular payment found in regpayment table")

mock_db.get_receipt_candidates.return_value = []
mock_db.get_regpayment_candidates.return_value = [
    make_regpayment(id=10, reason="Miete", amount_cents=-95000)
]
mock_db.get_regpayment_candidates_by_date.return_value = []

with patch.object(matcher, "_check_name_similarity", return_value="match"):
    results = matcher.match_all([make_tx("HAUSVERWALTUNG MUSTER", 950.00, direction="debit")])

r = results[0]
check("status is MATCHED",              r.status == matcher.MATCHED,        f"got: {r.status}")
check("matched_source is regpayment",   r.matched_source == "regpayment",   f"got: {r.matched_source}")
check("matched_name is Miete",          r.matched_name == "Miete",          f"got: {r.matched_name}")
check("matched_id is 10",               r.matched_id == 10,                 f"got: {r.matched_id}")


# ── Test 6: MATCHED income via regpayment ────────────────────────────────────

section("Test 6 — MATCHED: income (credit) matched against positive regpayment amount")

mock_db.get_receipt_candidates.return_value = []
mock_db.get_regpayment_candidates.return_value = [
    make_regpayment(id=11, reason="Gehalt", amount_cents=250000)
]
mock_db.get_regpayment_candidates_by_date.return_value = []

with patch.object(matcher, "_check_name_similarity", return_value="match"):
    results = matcher.match_all([make_tx("ARBEITGEBER GMBH GEHALT", 2500.00, direction="credit")])

r = results[0]
check("status is MATCHED",            r.status == matcher.MATCHED,       f"got: {r.status}")
check("matched_source is regpayment", r.matched_source == "regpayment",  f"got: {r.matched_source}")
check("matched_name is Gehalt",       r.matched_name == "Gehalt",        f"got: {r.matched_name}")


# ── Test 7: AMOUNT_MISMATCH ───────────────────────────────────────────────────

section("Test 7 — AMOUNT_MISMATCH: regpayment name matches but amount differs")

mock_db.get_receipt_candidates.return_value = []
mock_db.get_regpayment_candidates.return_value = []   # exact amount not found
mock_db.get_regpayment_candidates_by_date.return_value = [
    make_regpayment(id=12, reason="Handyvertrag", amount_cents=-3999)  # €39.99 expected
]

with patch.object(matcher, "_check_name_similarity", return_value="match"):
    results = matcher.match_all([make_tx("TELEKOM MOBILFUNK", 42.99, direction="debit")])

r = results[0]
check("status is AMOUNT_MISMATCH",    r.status == matcher.AMOUNT_MISMATCH,              f"got: {r.status}")
check("matched_source is regpayment", r.matched_source == "regpayment",                 f"got: {r.matched_source}")
check("mismatch note present",        any("Amount mismatch" in n for n in r.notes),     f"got: {r.notes}")
check("expected amount in note",      any("39.99" in n for n in r.notes),               f"got: {r.notes}")
check("actual amount in note",        any("42.99" in n for n in r.notes),               f"got: {r.notes}")


# ── Test 8: NO_MATCH ──────────────────────────────────────────────────────────

section("Test 8 — NO_MATCH: no candidate found anywhere")

mock_db.get_receipt_candidates.return_value = []
mock_db.get_regpayment_candidates.return_value = []
mock_db.get_regpayment_candidates_by_date.return_value = []

results = matcher.match_all([make_tx("UNBEKANNTE BUCHUNG", 7.50)])

r = results[0]
check("status is NO_MATCH",       r.status == matcher.NO_MATCH,  f"got: {r.status}")
check("matched_source is None",   r.matched_source is None,      f"got: {r.matched_source}")
check("matched_id is None",       r.matched_id is None,          f"got: {r.matched_id}")


# ── Test 9: Uncertain fallback ────────────────────────────────────────────────

section("Test 9 — Uncertain fallback: no definitive match, uncertain used as fallback")

mock_db.get_receipt_candidates.return_value = [
    make_receipt(id=5, issuer="Unbekannte Firma", amount=22.50, days_before_bank=2)
]
mock_db.get_regpayment_candidates.return_value = []
mock_db.get_regpayment_candidates_by_date.return_value = []

with patch.object(matcher, "_check_name_similarity", return_value="uncertain"):
    results = matcher.match_all([make_tx("UNBEKANNTE FIRMA IRGENDWO", 22.50)])

r = results[0]
check("status is not NO_MATCH",       r.status != matcher.NO_MATCH,                     f"got: {r.status}")
check("matched_source is receipt",    r.matched_source == "receipt",                    f"got: {r.matched_source}")
check("uncertain note present",       any("Uncertain" in n for n in r.notes),           f"got: {r.notes}")


# ── Test 10: 1-to-1 constraint ────────────────────────────────────────────────

section("Test 10 — 1-to-1 constraint: same receipt not matched to two transactions")

receipt = make_receipt(id=6, issuer="REWE GmbH", amount=43.20, days_before_bank=2)
mock_db.get_receipt_candidates.return_value = [receipt]
mock_db.get_regpayment_candidates.return_value = []
mock_db.get_regpayment_candidates_by_date.return_value = []

tx1 = make_tx("REWE SAGT DANKE",   43.20, tx_date=date(2024, 4, 15))
tx2 = make_tx("REWE MARKT 12345",  43.20, tx_date=date(2024, 4, 16))

with patch.object(matcher, "_check_name_similarity", return_value="match"):
    results = matcher.match_all([tx1, tx2])

r1, r2 = results[0], results[1]
check("first transaction matched",          r1.status == matcher.MATCHED,    f"got: {r1.status}")
check("second transaction is NO_MATCH",     r2.status == matcher.NO_MATCH,   f"got: {r2.status}")
check("different matched_id (1-to-1 held)", r2.matched_id is None,           f"got: {r2.matched_id}")


# ── Test 11: _to_signed_cents conversion ─────────────────────────────────────

section("Test 11 — _to_signed_cents: correct sign and cent conversion")

check("debit €950.00 → -95000",   matcher._to_signed_cents(Decimal("950.00"), "debit")  == -95000)
check("credit €2500.00 → 250000", matcher._to_signed_cents(Decimal("2500.00"), "credit") == 250000)
check("debit €0.01 → -1",         matcher._to_signed_cents(Decimal("0.01"), "debit")    == -1)
check("debit €10.99 → -1099",     matcher._to_signed_cents(Decimal("10.99"), "debit")   == -1099)


# ── Test 12: Chronological ordering ──────────────────────────────────────────

section("Test 12 — Chronological ordering: earlier transaction matched first")

# Two transactions on different dates, same amount.
# Only one receipt available. The earlier transaction should get it.
receipt = make_receipt(id=7, issuer="Supermarkt", amount=20.00, days_before_bank=1)
mock_db.get_receipt_candidates.return_value = [receipt]
mock_db.get_regpayment_candidates.return_value = []
mock_db.get_regpayment_candidates_by_date.return_value = []

tx_later  = make_tx("SUPERMARKT", 20.00, tx_date=date(2024, 4, 20))
tx_earlier = make_tx("SUPERMARKT", 20.00, tx_date=date(2024, 4, 15))

# Pass in reverse order to verify match_all sorts before matching
with patch.object(matcher, "_check_name_similarity", return_value="match"):
    results = matcher.match_all([tx_later, tx_earlier])

# Results are returned in the order transactions were sorted (by date),
# so index 0 = earlier, index 1 = later
matched   = [r for r in results if r.status == matcher.MATCHED]
unmatched = [r for r in results if r.status == matcher.NO_MATCH]

check("exactly one match",              len(matched) == 1,                                      f"got: {len(matched)}")
check("earlier transaction matched",    matched[0].transaction.date == date(2024, 4, 15),       f"got: {matched[0].transaction.date}")
check("later transaction unmatched",    unmatched[0].transaction.date == date(2024, 4, 20),     f"got: {unmatched[0].transaction.date}")


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