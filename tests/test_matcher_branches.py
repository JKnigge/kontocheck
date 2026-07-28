"""
tests/test_matcher_branches.py — pytest-style unit tests for matcher branching logic

Covers: _choose_candidate (LLM call + fallbacks) and match_all end-to-end
branching (conflict detection, name-only fallback, single claimant, belegbot
note, 1-to-1 postcondition, H4 date window, H1 credit direction).

These tests mock db_client and _choose_candidate (where appropriate) to
exercise branch logic without real DB or LLM calls.

Run:  python -m pytest tests/test_matcher_branches.py -v
"""

import importlib.util
import os
import sys
import types
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import-time mock of config, pipeline.extractor, storage, ollama — same
# pattern as test_step4_matcher.py.  Must happen before matcher is loaded.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

mock_config = types.ModuleType("config")
mock_config.OLLAMA_URL = "http://localhost:11434"
mock_config.OLLAMA_MODEL = "test-model"
mock_config.RECEIPT_DATE_WINDOW_DAYS = 28
mock_config.REGPAYMENT_USER_ID = 1
sys.modules["config"] = mock_config

from tests._helpers import Transaction, make_receipt, make_regpayment, make_tx

mock_extractor = types.ModuleType("pipeline.extractor")
mock_extractor.Transaction = Transaction
sys.modules["pipeline.extractor"] = mock_extractor

mock_db = MagicMock()
mock_storage = types.ModuleType("storage")
mock_storage.db_client = mock_db
sys.modules["storage"] = mock_storage
sys.modules["storage.db_client"] = mock_db

_spec = importlib.util.spec_from_file_location(
    "pipeline.matcher",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "pipeline", "matcher.py"),
)
_mod = importlib.util.module_from_spec(_spec)
with patch("ollama.Client"):
    _spec.loader.exec_module(_mod)

matcher = _mod
matcher.db_client = mock_db


@pytest.fixture(autouse=True)
def _reset_db_mock():
    """Reset all db_client return values and side effects before each test.
    reset_mock() does NOT clear side_effect by default, so we must
    explicitly reset it to prevent leakage between tests."""
    mock_db.reset_mock(return_value=True, side_effect=True)
    mock_db.get_regpayment_candidates_by_date.return_value = []
    mock_db.get_receipt_candidates_by_date.return_value = []


# ═══════════════════════════════════════════════════════════════════════════
# _choose_candidate  (new — replaces _check_name_similarity)
# ═══════════════════════════════════════════════════════════════════════════

class TestChooseCandidate:
    """Tests for _choose_candidate. Mock _client.chat to control LLM output."""

    def _make_receipt_candidate(self, id=1, issuer="OBI GmbH", amount=Decimal("43.20")):
        return {"id": id, "issuer": issuer, "total_amount": amount}

    def test_match_single_candidate(self):
        """LLM returns 'match: 1' → ('match', [1])."""
        cands = [self._make_receipt_candidate()]
        with patch.object(matcher._client, "chat", return_value={
            "message": {"content": "match: 1"}
        }):
            verdict, indices = matcher._choose_candidate(
                make_tx("Kartenzahlung OBI", 43.20), cands, "receipt"
            )
        assert verdict == "match"
        assert indices == [1]

    def test_uncertain_multiple_candidates(self):
        """LLM returns 'uncertain: 1,2' → ('uncertain', [1, 2])."""
        cands = [
            self._make_receipt_candidate(id=1, issuer="OBI GmbH"),
            self._make_receipt_candidate(id=2, issuer="REWE Filiale"),
        ]
        with patch.object(matcher._client, "chat", return_value={
            "message": {"content": "uncertain: 1,2"}
        }):
            verdict, indices = matcher._choose_candidate(
                make_tx("UNBEKANNT", 43.20), cands, "receipt"
            )
        assert verdict == "uncertain"
        assert indices == [1, 2]

    def test_no_match(self):
        """LLM returns 'no_match' → ('no_match', [])."""
        cands = [self._make_receipt_candidate(id=1, issuer="Telekom")]
        with patch.object(matcher._client, "chat", return_value={
            "message": {"content": "no_match"}
        }), patch.object(matcher, "_has_brand_overlap", return_value=False):
            verdict, indices = matcher._choose_candidate(
                make_tx("REWE SAGT DANKE", 43.20), cands, "receipt"
            )
        assert verdict == "no_match"
        assert indices == []

    def test_llm_exception_returns_uncertain(self):
        """LLM raises exception → ('uncertain', [all indices])."""
        cands = [
            self._make_receipt_candidate(id=1),
            self._make_receipt_candidate(id=2),
        ]
        with patch.object(matcher._client, "chat", side_effect=Exception("connection error")):
            verdict, indices = matcher._choose_candidate(
                make_tx("OBI", 43.20), cands, "receipt"
            )
        assert verdict == "uncertain"
        assert indices == [1, 2]

    def test_no_match_with_brand_overlap_upgrades_to_uncertain(self):
        """LLM says 'no_match' + clear brand overlap → upgraded to 'uncertain'."""
        cands = [self._make_receipt_candidate(id=1, issuer="OBI GmbH")]
        with patch.object(matcher._client, "chat", return_value={
            "message": {"content": "no_match"}
        }), patch.object(matcher, "_has_brand_overlap", return_value=True):
            verdict, indices = matcher._choose_candidate(
                make_tx("Kartenzahlung OBI SAGT DANKE", 43.20), cands, "receipt"
            )
        assert verdict == "uncertain"
        assert indices == [1]

    def test_no_match_no_brand_overlap_stays_no_match(self):
        """LLM says 'no_match' + no brand overlap → stays 'no_match'."""
        cands = [self._make_receipt_candidate(id=1, issuer="REWE GmbH")]
        with patch.object(matcher._client, "chat", return_value={
            "message": {"content": "no_match"}
        }), patch.object(matcher, "_has_brand_overlap", return_value=False):
            verdict, indices = matcher._choose_candidate(
                make_tx("SEPA Lastschrift TELEKOM", 43.20), cands, "receipt"
            )
        assert verdict == "no_match"
        assert indices == []

    def test_uncertain_no_indices_keeps_all(self):
        """LLM returns 'uncertain' with no indices → keeps all candidates."""
        cands = [
            self._make_receipt_candidate(id=1),
            self._make_receipt_candidate(id=2),
        ]
        with patch.object(matcher._client, "chat", return_value={
            "message": {"content": "uncertain"}
        }):
            verdict, indices = matcher._choose_candidate(
                make_tx("UNBEKANNT", 43.20), cands, "receipt"
            )
        assert verdict == "uncertain"
        assert indices == [1, 2]


# ═══════════════════════════════════════════════════════════════════════════
# match_all end-to-end branching
# ═══════════════════════════════════════════════════════════════════════════

class TestMatchAll:
    """End-to-end tests for match_all branching logic.

    These tests mock _choose_candidate to control the LLM verdict and
    exercise the candidate gathering + Pass B reconciliation logic.
    """

    def test_single_receipt_match(self):
        """One tx, one receipt, LLM says match → MATCH."""
        r = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=3)
        mock_db.get_receipt_candidates_by_date.return_value = [r]
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])
        assert results[0].status == matcher.MATCH
        assert results[0].matched_source == "receipt"
        assert results[0].matched_id == 1
        assert results[0].matched_name == "REWE GmbH"

    def test_single_regpayment_match(self):
        """One tx, one regpayment, LLM says match → MATCH."""
        rp = make_regpayment(id=10, reason="Miete", amount_cents=-95000)
        mock_db.get_regpayment_candidates_by_date.return_value = [rp]
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([
                make_tx("HAUSVERWALTUNG MUSTER", 950.00, direction="debit"),
            ])
        assert results[0].status == matcher.MATCH
        assert results[0].matched_source == "regpayment"
        assert results[0].matched_id == 10
        assert results[0].matched_name == "Miete"

    def test_no_candidates_no_match(self):
        """No candidates from any source → NO_MATCH."""
        results = matcher.match_all([make_tx("UNBEKANNTE BUCHUNG", 7.50)])
        assert results[0].status == matcher.NO_MATCH
        assert results[0].matched_source is None
        assert results[0].matched_id is None

    def test_amount_match_uncertain(self):
        """LLM says uncertain on amount-matching candidates → UNCERTAIN."""
        r = make_receipt(id=1, issuer="Maybe REWE", amount=43.20, days_before_bank=3)
        mock_db.get_receipt_candidates_by_date.return_value = [r]
        with patch.object(matcher, "_choose_candidate", return_value=("uncertain", [1])):
            results = matcher.match_all([make_tx("REWE?", 43.20)])
        assert results[0].status == matcher.UNCERTAIN
        assert len(results[0].candidates) == 1
        assert results[0].candidates[0].id == 1

    def test_conflict_two_txs_claim_same_receipt(self):
        """Two txs claim the same receipt → both UNCERTAIN, conflict=True,
        contested candidate in both candidates[], each conflict_with lists
        the other."""
        r = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=2)
        mock_db.get_receipt_candidates_by_date.return_value = [r]

        tx1 = make_tx("REWE SAGT DANKE", 43.20, tx_date=date(2024, 4, 15))
        tx2 = make_tx("REWE MARKT 12345", 43.20, tx_date=date(2024, 4, 16))

        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([tx1, tx2])

        assert results[0].status == matcher.UNCERTAIN
        assert results[1].status == matcher.UNCERTAIN
        assert results[0].conflict is True
        assert results[1].conflict is True
        # matched_* cleared
        assert results[0].matched_id is None
        assert results[1].matched_id is None
        # Contested candidate in both
        assert any(c.id == 1 for c in results[0].candidates)
        assert any(c.id == 1 for c in results[1].candidates)
        # Each conflict_with lists the other
        assert len(results[0].conflict_with) == 1
        assert "REWE MARKT 12345" in results[0].conflict_with[0]
        assert len(results[1].conflict_with) == 1
        assert "REWE SAGT DANKE" in results[1].conflict_with[0]

    def test_single_claimant_stays_match(self):
        """One tx claims a row → stays MATCH (no false conflict)."""
        r = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=3)
        mock_db.get_receipt_candidates_by_date.return_value = [r]
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])
        assert results[0].status == matcher.MATCH
        assert results[0].conflict is False
        assert results[0].matched_id == 1

    def test_diagonal_pairing_no_conflict(self):
        """Two txs, two receipts — LLM picks different ones → both MATCH,
        no conflict (H3 diagonal pairing scenario).
        Linked: H3"""
        r_d0 = make_receipt(id=1, issuer="Amazon", amount=19.99, days_before_bank=2,
                            file_name="receipt_d0.pdf")
        r_d1 = make_receipt(id=2, issuer="Amazon", amount=19.99, days_before_bank=1,
                            file_name="receipt_d1.pdf")
        mock_db.get_receipt_candidates_by_date.return_value = [r_d0, r_d1]

        tx_d1 = make_tx("AMAZON PAYMENTS EU", 19.99, tx_date=date(2024, 4, 15))
        tx_d2 = make_tx("AMAZON PAYMENTS EU", 19.99, tx_date=date(2024, 4, 16))

        # tx_d1 picks candidate 2 (r_d1, closer), tx_d2 picks candidate 1 (r_d0)
        call_count = [0]
        def choose_side_effect(tx, candidates, source_label, conservative=False):
            call_count[0] += 1
            if call_count[0] == 1:
                return ("match", [2])
            return ("match", [1])

        with patch.object(matcher, "_choose_candidate", side_effect=choose_side_effect):
            results = matcher.match_all([tx_d1, tx_d2])

        assert results[0].status == matcher.MATCH
        assert results[1].status == matcher.MATCH
        assert results[0].matched_id == 2
        assert results[1].matched_id == 1
        assert results[0].conflict is False
        assert results[1].conflict is False

    def test_1to1_postcondition_holds(self):
        """After match_all, no two MATCH results share the same
        (matched_source, matched_id). Two txs each match a different
        receipt → both MATCH, unique matched_ids."""
        r1 = make_receipt(id=1, issuer="REWE", amount=43.20, days_before_bank=2)
        r2 = make_receipt(id=2, issuer="EDEKA", amount=50.00, days_before_bank=1)
        # get_receipt_candidates_by_date returns different lists based on amount
        def get_receipts(bank_date, window_days, amount=None):
            if amount == Decimal("43.20"):
                return [r1]
            return [r2]
        mock_db.get_receipt_candidates_by_date.side_effect = get_receipts

        tx1 = make_tx("REWE SAGT DANKE", 43.20, tx_date=date(2024, 4, 15))
        tx2 = make_tx("EDEKA MARKT", 50.00, tx_date=date(2024, 4, 16))

        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([tx1, tx2])

        match_results = [r for r in results if r.status == matcher.MATCH]
        assert len(match_results) == 2
        matched_keys = [(r.matched_source, r.matched_id) for r in match_results]
        assert len(matched_keys) == len(set(matched_keys)), (
            f"Duplicate matched keys: {matched_keys}"
        )

    def test_belegbot_unreviewed_note_on_match(self):
        """Receipt with manually_checked=None + confidence != 'high' →
        MATCH with belegbot-unreviewed note."""
        r = make_receipt(
            id=1, issuer="Unbekannt GmbH", amount=15.00, days_before_bank=2,
            confidence="low", manually_checked=None,
        )
        mock_db.get_receipt_candidates_by_date.return_value = [r]
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([make_tx("UNBEKANNT REF 123", 15.00)])
        assert results[0].status == matcher.MATCH
        assert any("belegbot" in n.lower() for n in results[0].notes)

    def test_belegbot_reviewed_no_note(self):
        """Receipt with manually_checked=1 → MATCH with no belegbot note."""
        r = make_receipt(
            id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=3,
            confidence="high", manually_checked=1,
        )
        mock_db.get_receipt_candidates_by_date.return_value = [r]
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])
        assert results[0].status == matcher.MATCH
        assert not any("belegbot" in n.lower() for n in results[0].notes)

    def test_name_only_fallback_uncertain(self):
        """No amount match → name-only fallback finds regpayment with
        different amount → LLM says uncertain → UNCERTAIN with
        amount_match=False."""
        rp = make_regpayment(id=12, reason="Handyvertrag", amount_cents=-3999)
        # Amount-path call (with signed_cents) returns []; name-only call
        # (without) returns [rp] so the regpayment surfaces in the fallback.
        def get_regpays(bank_date, signed_cents=None):
            return [] if signed_cents is not None else [rp]
        mock_db.get_regpayment_candidates_by_date.side_effect = get_regpays
        with patch.object(matcher, "_choose_candidate", return_value=("uncertain", [1])):
            results = matcher.match_all([
                make_tx("TELEKOM MOBILFUNK", 42.99, direction="debit"),
            ])
        assert results[0].status == matcher.UNCERTAIN
        assert len(results[0].candidates) == 1
        assert results[0].candidates[0].amount_match is False
        assert results[0].candidates[0].source == "regpayment"

    def test_name_only_fallback_amount_mismatch_is_uncertain(self):
        """No amount match → name-only fallback finds regpayment with
        different amount → LLM says match → UNCERTAIN (amount differs,
        requires manual review). Non-matching amounts must never get MATCH."""
        rp = make_regpayment(id=12, reason="Handyvertrag", amount_cents=-3999)
        def get_regpays(bank_date, signed_cents=None):
            return [] if signed_cents is not None else [rp]
        mock_db.get_regpayment_candidates_by_date.side_effect = get_regpays
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([
                make_tx("TELEKOM MOBILFUNK", 42.99, direction="debit"),
            ])
        assert results[0].status == matcher.UNCERTAIN
        assert results[0].matched_source is None
        assert results[0].matched_id is None
        assert len(results[0].candidates) == 1
        assert results[0].candidates[0].source == "regpayment"
        assert results[0].candidates[0].amount_match is False
        assert any("amount differs" in (c.note or "").lower()
                   for c in results[0].candidates)

    def test_name_only_fallback_amount_match_stays_match(self):
        """Name-only fallback where the chosen candidate's amount DOES
        match the transaction → stays MATCH (amount AND name match)."""
        rp = make_regpayment(id=12, reason="Handyvertrag", amount_cents=-4299)
        # Amount-path call (with signed_cents=-4299) returns []; name-only
        # call (without) returns [rp] whose amount happens to match.
        def get_regpays(bank_date, signed_cents=None):
            return [] if signed_cents is not None else [rp]
        mock_db.get_regpayment_candidates_by_date.side_effect = get_regpays
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([
                make_tx("TELEKOM MOBILFUNK", 42.99, direction="debit"),
            ])
        assert results[0].status == matcher.MATCH
        assert results[0].matched_source == "regpayment"
        assert results[0].matched_id == 12

    def test_amount_match_no_match_falls_through_to_name_only(self):
        """Amount-matching candidates exist but LLM says no_match →
        falls through to name-only fallback."""
        r = make_receipt(id=1, issuer="Wrong Store", amount=43.20, days_before_bank=3)
        rp = make_regpayment(id=10, reason="Miete", amount_cents=-3999)
        # Amount-path call (with amount kwarg) returns the receipt; the
        # name-only call (without amount) returns no receipts so only the
        # regpayment surfaces in the fallback.
        def get_receipts(bank_date, window_days, amount=None):
            return [r] if amount is not None else []
        mock_db.get_receipt_candidates_by_date.side_effect = get_receipts
        # Amount-path regpayment call (with signed_cents) returns []; the
        # name-only call (without) returns [rp].
        def get_regpays(bank_date, signed_cents=None):
            return [] if signed_cents is not None else [rp]
        mock_db.get_regpayment_candidates_by_date.side_effect = get_regpays

        # First call (amount candidates) → no_match; second call (name-only) → uncertain
        call_count = [0]
        def choose_side_effect(tx, candidates, source_label, conservative=False):
            call_count[0] += 1
            if call_count[0] == 1:
                return ("no_match", [])
            return ("uncertain", [1])

        with patch.object(matcher, "_choose_candidate", side_effect=choose_side_effect):
            results = matcher.match_all([make_tx("TELEKOM", 43.20)])

        assert results[0].status == matcher.UNCERTAIN
        assert len(results[0].candidates) == 1
        # The name-only candidate is the regpayment (rp), not the receipt
        assert results[0].candidates[0].source == "regpayment"

    # ── H4: date window enforcement ──────────────────────────────────────

    def test_stale_receipt_not_candidate(self):
        """2-year-old receipt of same amount must NOT be a candidate
        after H4 (date window enforcement). Stale candidates are filtered
        out of the candidate list before the LLM sees them, so the
        transaction falls through to NO_MATCH.
        Linked: H4"""
        old_receipt = make_receipt(id=1, issuer="REWE", amount=43.20, days_before_bank=730)
        # Amount-path call (with amount) returns the stale receipt so the
        # H4 Python filter removes it; the name-only call (without amount)
        # returns [] — in production the SQL date window excludes it too.
        def get_receipts(bank_date, window_days, amount=None):
            return [old_receipt] if amount is not None else []
        mock_db.get_receipt_candidates_by_date.side_effect = get_receipts
        with patch.object(matcher, "_choose_candidate") as mock_choose:
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])
        assert results[0].status == matcher.NO_MATCH
        mock_choose.assert_not_called()

    def test_fresh_receipt_within_window_still_matches(self):
        """Receipt within the date window must still match — the
        H4 filter must not over-fire on legitimate candidates.
        Linked: H4"""
        fresh = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=3)
        mock_db.get_receipt_candidates_by_date.return_value = [fresh]
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])
        assert results[0].status == matcher.MATCH
        assert results[0].matched_id == 1

    def test_receipt_at_window_boundary_matches(self):
        """A receipt exactly RECEIPT_DATE_WINDOW_DAYS old is still
        a valid candidate (boundary is inclusive).
        Linked: H4"""
        boundary = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=28)
        mock_db.get_receipt_candidates_by_date.return_value = [boundary]
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])
        assert results[0].matched_source == "receipt"
        assert results[0].matched_id == 1

    def test_receipt_one_day_beyond_window_filtered(self):
        """A receipt RECEIPT_DATE_WINDOW_DAYS+1 old is filtered out.
        Boundary is inclusive on the window side.
        Linked: H4"""
        stale = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=29)
        # Amount-path call returns the stale receipt so the H4 Python filter
        # removes it; the name-only call returns [] (SQL date window excludes
        # it in production).
        def get_receipts(bank_date, window_days, amount=None):
            return [stale] if amount is not None else []
        mock_db.get_receipt_candidates_by_date.side_effect = get_receipts
        with patch.object(matcher, "_choose_candidate") as mock_choose:
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])
        assert results[0].status == matcher.NO_MATCH
        mock_choose.assert_not_called()

    def test_mixed_stale_and_fresh_only_fresh_considered(self):
        """When the DB returns a stale and a fresh receipt, only the
        fresh one reaches _choose_candidate.
        Linked: H4"""
        stale = make_receipt(id=1, issuer="REWE", amount=43.20, days_before_bank=730)
        fresh = make_receipt(id=2, issuer="REWE GmbH", amount=43.20, days_before_bank=2)
        mock_db.get_receipt_candidates_by_date.return_value = [stale, fresh]
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])) as mock_choose:
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])
        assert results[0].status == matcher.MATCH
        assert results[0].matched_id == 2
        # Only one candidate reached _choose_candidate (the fresh one)
        assert mock_choose.call_count == 1
        args = mock_choose.call_args[0]
        cands = args[1]
        assert len(cands) == 1
        assert cands[0]["id"] == 2

    # ── H1: credit direction skips receipt matching ──────────────────────

    def test_credit_tx_skips_receipt_matching(self):
        """Credit-direction tx must NOT match receipts; falls through
        to regpayment-only matching.
        Linked: H1"""
        r = make_receipt(id=1, issuer="Amazon", amount=29.99, days_before_bank=1)
        mock_db.get_receipt_candidates_by_date.return_value = [r]
        mock_db.get_receipt_candidates_by_date.return_value = [r]
        with patch.object(matcher, "_choose_candidate") as mock_choose:
            results = matcher.match_all([
                make_tx("AMAZON REFUND", 29.99, direction="credit"),
            ])
        # get_receipt_candidates_by_date should NOT be called for credit txs
        mock_db.get_receipt_candidates_by_date.assert_not_called()
        # Without regpayment candidates, result is NO_MATCH
        assert results[0].status == matcher.NO_MATCH

    def test_income_tx_matched_via_regpayment(self):
        """Income tx (credit, salary) → MATCH via regpayment.
        Validates H1 doesn't break income matching.
        Linked: H1"""
        rp = make_regpayment(id=1, reason="Gehalt", amount_cents=250000)
        mock_db.get_regpayment_candidates_by_date.return_value = [rp]
        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([
                make_tx("ARBEITGEBER GMBH GEHALT", 2500.00, direction="credit"),
            ])
        assert results[0].status == matcher.MATCH
        assert results[0].matched_source == "regpayment"

    # ── Ordering and progress ────────────────────────────────────────────

    def test_chronological_ordering(self):
        """Results are returned sorted by transaction date."""
        mock_db.get_receipt_candidates_by_date.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        txs = [
            make_tx("TX3", 30.00, tx_date=date(2024, 4, 12)),
            make_tx("TX1", 10.00, tx_date=date(2024, 4, 10)),
            make_tx("TX2", 20.00, tx_date=date(2024, 4, 11)),
        ]
        results = matcher.match_all(txs)
        dates = [r.transaction.date for r in results]
        assert dates == sorted(dates)

    def test_progress_log_includes_correct_count(self):
        """match_all iterates with progress log including correct N/total.
        Light sanity check — verify all transactions are processed and
        results count matches input count.
        Linked: L14"""
        txs = [
            make_tx("TX1", 10.00, tx_date=date(2024, 4, 10)),
            make_tx("TX2", 20.00, tx_date=date(2024, 4, 11)),
            make_tx("TX3", 30.00, tx_date=date(2024, 4, 12)),
        ]
        results = matcher.match_all(txs)
        assert len(results) == 3
        assert all(r.status == matcher.NO_MATCH for r in results)


# ═══════════════════════════════════════════════════════════════════════════
# 1-to-1 postcondition (replaces old L11 tests — commit-at-call-site gone)
# ═══════════════════════════════════════════════════════════════════════════

class TestOneToOnePostcondition:
    """The 1-to-1 constraint is now enforced as a postcondition in Pass B.
    These tests verify no two MATCH results share the same matched_id, and
    that conflicts surface as UNCERTAIN pairs."""

    def test_two_txs_same_receipt_both_uncertain(self):
        """Two txs claim the same receipt → both UNCERTAIN (neither wins).
        The 1-to-1 postcondition holds because neither claims the row."""
        r = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=2)
        mock_db.get_receipt_candidates_by_date.return_value = [r]

        tx1 = make_tx("REWE SAGT DANKE", 43.20, tx_date=date(2024, 4, 15))
        tx2 = make_tx("REWE MARKT 12345", 43.20, tx_date=date(2024, 4, 16))

        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([tx1, tx2])

        # No MATCH results → trivially no shared matched_id
        match_results = [r for r in results if r.status == matcher.MATCH]
        assert len(match_results) == 0
        assert all(r.status == matcher.UNCERTAIN for r in results)

    def test_two_txs_same_regpayment_both_match(self):
        """Two txs claim the same regpayment → both stay MATCH (Issue 2).

        Regular payments are recurring by nature: a statement covering
        ~1 month may list two transfers by the same payee (e.g. insurance
        collected on May 3 and June 3). Both legitimately match the same
        regpayment row, so the 1-to-1 postcondition does NOT apply to
        regpayments — only to receipts."""
        rp = make_regpayment(id=10, reason="Miete", amount_cents=-95000)
        mock_db.get_regpayment_candidates_by_date.return_value = [rp]

        tx1 = make_tx("HAUSVERWALTUNG MUSTER", 950.00, direction="debit",
                      tx_date=date(2024, 4, 15))
        tx2 = make_tx("HAUSVERWALTUNG MUSTER", 950.00, direction="debit",
                      tx_date=date(2024, 5, 3))

        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([tx1, tx2])

        assert all(r.status == matcher.MATCH for r in results)
        assert all(r.matched_id == 10 for r in results)
        assert all(r.conflict is False for r in results)

    def test_no_two_match_results_share_id(self):
        """When two txs each match a different receipt, both stay MATCH
        and their matched_ids are unique."""
        r1 = make_receipt(id=1, issuer="REWE", amount=43.20, days_before_bank=2)
        r2 = make_receipt(id=2, issuer="EDEKA", amount=50.00, days_before_bank=1)

        def get_receipts(bank_date, window_days, amount=None):
            if amount == Decimal("43.20"):
                return [r1]
            return [r2]
        mock_db.get_receipt_candidates_by_date.side_effect = get_receipts

        tx1 = make_tx("REWE SAGT DANKE", 43.20, tx_date=date(2024, 4, 15))
        tx2 = make_tx("EDEKA MARKT", 50.00, tx_date=date(2024, 4, 16))

        with patch.object(matcher, "_choose_candidate", return_value=("match", [1])):
            results = matcher.match_all([tx1, tx2])

        match_results = [r for r in results if r.status == matcher.MATCH]
        assert len(match_results) == 2
        ids = [r.matched_id for r in match_results]
        assert len(ids) == len(set(ids)), f"Duplicate matched_ids: {ids}"
