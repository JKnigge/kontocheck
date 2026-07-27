"""
tests/test_step4_matcher.py — manual test script for pipeline/matcher.py

Tests the three redesigned status paths (MATCH, UNCERTAIN, NO_MATCH) and
the 1-to-1 postcondition (conflict detection) without requiring a real
database or Ollama connection. Both dependencies are mocked so the test
is fast, deterministic, and runnable in isolation.

Run from the project root:
    python tests/test_step4_matcher.py

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

# Switch stdout/stderr to UTF-8 so the status emoji and box-drawing characters
# in the test output print cleanly on Windows. This test mocks `config`, so the
# automatic reconfigure in config.py is not in effect here.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ── Minimal config mock ───────────────────────────────────────────────────────
# Must be in place before importing matcher, since matcher imports config
# at module level to create the Ollama client.

import types
mock_config = types.ModuleType("config")
mock_config.OLLAMA_URL         = "http://localhost:11434"
mock_config.OLLAMA_MODEL       = "test-model"
mock_config.DATE_TIER1_DAYS    = 5
mock_config.DATE_TIER2_DAYS    = 14
mock_config.RECEIPT_DATE_WINDOW_DAYS = 28
mock_config.REGPAYMENT_USER_ID = 1
sys.modules["config"] = mock_config

# ── Mock pipeline.extractor ───────────────────────────────────────────────────

from tests._helpers import Transaction, make_receipt, make_regpayment, make_tx

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


def _reset_db():
    mock_db.reset_mock(return_value=True, side_effect=True)
    mock_db.get_receipt_candidates.return_value = []
    mock_db.get_regpayment_candidates.return_value = []
    mock_db.get_regpayment_candidates_by_date.return_value = []
    mock_db.get_receipt_candidates_by_date.return_value = []


# ── Test 1: MATCH (receipt) ───────────────────────────────────────────────────

section("Test 1 — MATCH: receipt found and LLM picks it")

_reset_db()
mock_db.get_receipt_candidates.return_value = [
    make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=3)
]

with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
    results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])

r = results[0]
check("status is MATCH",          r.status == matcher.MATCH,           f"got: {r.status}")
check("matched_source is receipt", r.matched_source == "receipt",       f"got: {r.matched_source}")
check("matched_id is 1",           r.matched_id == 1,                   f"got: {r.matched_id}")
check("matched_name is REWE GmbH", r.matched_name == "REWE GmbH",       f"got: {r.matched_name}")
check("no notes",                  r.notes == [],                       f"got: {r.notes}")


# ── Test 2: MATCH (receipt, large gap within window) ──────────────────────────

section("Test 2 — MATCH: receipt with 10-day gap still MATCH (no delay tiers)")

_reset_db()
mock_db.get_receipt_candidates.return_value = [
    make_receipt(id=2, issuer="Telekom", amount=39.99, days_before_bank=10)
]

with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
    results = matcher.match_all([make_tx("TELEKOM DEUTSCHLAND", 39.99)])

r = results[0]
check("status is MATCH (no delay tiers)", r.status == matcher.MATCH, f"got: {r.status}")
check("matched_id is 2",                  r.matched_id == 2,         f"got: {r.matched_id}")


# ── Test 3: MATCH (receipt, gap at window boundary) ───────────────────────────

section("Test 3 — MATCH: receipt at window boundary (28d) still MATCH")

_reset_db()
mock_db.get_receipt_candidates.return_value = [
    make_receipt(id=3, issuer="Amazon", amount=29.99, days_before_bank=28)
]

with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
    results = matcher.match_all([make_tx("AMAZON PAYMENTS", 29.99)])

r = results[0]
check("status is MATCH (boundary)", r.status == matcher.MATCH, f"got: {r.status}")
check("matched_id is 3",            r.matched_id == 3,         f"got: {r.matched_id}")


# ── Test 4: MATCH with belegbot-unreviewed note ───────────────────────────────

section("Test 4 — MATCH: receipt flagged by belegbot (unreviewed note)")

_reset_db()
mock_db.get_receipt_candidates.return_value = [
    make_receipt(
        id=4, issuer="Unbekannt GmbH", amount=15.00, days_before_bank=2,
        confidence="low", manually_checked=None,
    )
]

with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
    results = matcher.match_all([make_tx("UNBEKANNT REF 123", 15.00)])

r = results[0]
check("status is MATCH (not a separate status)", r.status == matcher.MATCH, f"got: {r.status}")
check("belegbot note present",   any("belegbot" in n.lower() for n in r.notes), f"got: {r.notes}")


# ── Test 5: MATCH via regpayment ───────────────────────────────────────────────

section("Test 5 — MATCH: regular payment found in regpayment table")

_reset_db()
mock_db.get_regpayment_candidates.return_value = [
    make_regpayment(id=10, reason="Miete", amount_cents=-95000)
]

with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
    results = matcher.match_all([make_tx("HAUSVERWALTUNG MUSTER", 950.00, direction="debit")])

r = results[0]
check("status is MATCH",              r.status == matcher.MATCH,        f"got: {r.status}")
check("matched_source is regpayment", r.matched_source == "regpayment", f"got: {r.matched_source}")
check("matched_name is Miete",        r.matched_name == "Miete",        f"got: {r.matched_name}")
check("matched_id is 10",             r.matched_id == 10,               f"got: {r.matched_id}")


# ── Test 6: MATCH income via regpayment ───────────────────────────────────────

section("Test 6 — MATCH: income (credit) matched against positive regpayment amount")

_reset_db()
mock_db.get_regpayment_candidates.return_value = [
    make_regpayment(id=11, reason="Gehalt", amount_cents=250000)
]

with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
    results = matcher.match_all([make_tx("ARBEITGEBER GMBH GEHALT", 2500.00, direction="credit")])

r = results[0]
check("status is MATCH",            r.status == matcher.MATCH,       f"got: {r.status}")
check("matched_source is regpayment", r.matched_source == "regpayment", f"got: {r.matched_source}")
check("matched_name is Gehalt",      r.matched_name == "Gehalt",     f"got: {r.matched_name}")


# ── Test 7: UNCERTAIN (name-only fallback, regpayment amount differs) ──────────

section("Test 7 — UNCERTAIN: regpayment name matches but amount differs")

_reset_db()
mock_db.get_regpayment_candidates_by_date.return_value = [
    make_regpayment(id=12, reason="Handyvertrag", amount_cents=-3999)  # €39.99 expected
]

with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
    results = matcher.match_all([make_tx("TELEKOM MOBILFUNK", 42.99, direction="debit")])

r = results[0]
check("status is MATCH (amount-mismatch is now a note, not a status)",
      r.status == matcher.MATCH, f"got: {r.status}")
check("matched_source is regpayment", r.matched_source == "regpayment", f"got: {r.matched_source}")
check("amount differs note present",   any("amount differs" in n.lower() for n in r.notes), f"got: {r.notes}")
check("expected amount in note",       any("39.99" in n for n in r.notes), f"got: {r.notes}")
check("actual amount in note",         any("42.99" in n for n in r.notes), f"got: {r.notes}")


# ── Test 8: NO_MATCH ──────────────────────────────────────────────────────────

section("Test 8 — NO_MATCH: no candidate found anywhere")

_reset_db()

results = matcher.match_all([make_tx("UNBEKANNTE BUCHUNG", 7.50)])

r = results[0]
check("status is NO_MATCH",       r.status == matcher.NO_MATCH,  f"got: {r.status}")
check("matched_source is None",   r.matched_source is None,      f"got: {r.matched_source}")
check("matched_id is None",       r.matched_id is None,          f"got: {r.matched_id}")


# ── Test 9: Uncertain fallback ─────────────────────────────────────────────────

section("Test 9 — UNCERTAIN: LLM says uncertain on amount-matching candidates")

_reset_db()
mock_db.get_receipt_candidates.return_value = [
    make_receipt(id=5, issuer="Unbekannte Firma", amount=22.50, days_before_bank=2)
]

with patch.object(matcher, "_choose_candidate", return_value=("uncertain", [1])):
    results = matcher.match_all([make_tx("UNBEKANNTE FIRMA IRGENDWO", 22.50)])

r = results[0]
check("status is UNCERTAIN",       r.status == matcher.UNCERTAIN, f"got: {r.status}")
check("has candidates",             len(r.candidates) == 1,       f"got: {r.candidates}")
check("candidate id is 5",         r.candidates[0].id == 5,      f"got: {r.candidates[0].id}")


# ── Test 10: 1-to-1 constraint (conflict) ──────────────────────────────────────

section("Test 10 — 1-to-1 constraint: two txs claim same receipt → both UNCERTAIN")

_reset_db()
receipt = make_receipt(id=6, issuer="REWE GmbH", amount=43.20, days_before_bank=2)
mock_db.get_receipt_candidates.return_value = [receipt]

tx1 = make_tx("REWE SAGT DANKE",   43.20, tx_date=date(2024, 4, 15))
tx2 = make_tx("REWE MARKT 12345",  43.20, tx_date=date(2024, 4, 16))

with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
    results = matcher.match_all([tx1, tx2])

r1, r2 = results[0], results[1]
check("first transaction UNCERTAIN (conflict)",  r1.status == matcher.UNCERTAIN, f"got: {r1.status}")
check("second transaction UNCERTAIN (conflict)", r2.status == matcher.UNCERTAIN, f"got: {r2.status}")
check("first has conflict=True",                r1.conflict is True,            f"got: {r1.conflict}")
check("second has conflict=True",               r2.conflict is True,            f"got: {r2.conflict}")
check("first conflict_with lists second",        any("REWE MARKT" in s for s in r1.conflict_with), f"got: {r1.conflict_with}")
check("second conflict_with lists first",        any("REWE SAGT DANKE" in s for s in r2.conflict_with), f"got: {r2.conflict_with}")
check("neither claims the row (1-to-1 held)",    r1.matched_id is None and r2.matched_id is None, f"got: {r1.matched_id}, {r2.matched_id}")


# ── Test 11: _to_signed_cents conversion ─────────────────────────────────────

section("Test 11 — _to_signed_cents: correct sign and cent conversion")

check("debit €950.00 → -95000",   matcher._to_signed_cents(Decimal("950.00"), "debit")  == -95000)
check("credit €2500.00 → 250000", matcher._to_signed_cents(Decimal("2500.00"), "credit") == 250000)
check("debit €0.01 → -1",         matcher._to_signed_cents(Decimal("0.01"), "debit")    == -1)
check("debit €10.99 → -1099",     matcher._to_signed_cents(Decimal("10.99"), "debit")   == -1099)


# ── Test 12: Chronological ordering ──────────────────────────────────────────

section("Test 12 — Chronological ordering: results sorted by transaction date")

_reset_db()
receipt = make_receipt(id=7, issuer="Supermarkt", amount=20.00, days_before_bank=1)
mock_db.get_receipt_candidates.return_value = [receipt]

tx_later  = make_tx("SUPERMARKT", 20.00, tx_date=date(2024, 4, 20))
tx_earlier = make_tx("SUPERMARKT", 20.00, tx_date=date(2024, 4, 15))

# Pass in reverse order to verify match_all sorts before matching
with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
    results = matcher.match_all([tx_later, tx_earlier])

# Results are returned sorted by date
check("first result is earlier tx",  results[0].transaction.date == date(2024, 4, 15), f"got: {results[0].transaction.date}")
check("second result is later tx",    results[1].transaction.date == date(2024, 4, 20), f"got: {results[1].transaction.date}")

# Both txs claim the same receipt → both UNCERTAIN (conflict)
matched = [r for r in results if r.status == matcher.MATCH]
uncertain = [r for r in results if r.status == matcher.UNCERTAIN]
check("both UNCERTAIN (conflict over same receipt)", len(uncertain) == 2, f"got: {len(uncertain)} uncertain, {len(matched)} match")


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
