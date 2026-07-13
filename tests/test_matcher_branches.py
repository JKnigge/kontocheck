"""
tests/test_matcher_branches.py — pytest-style unit tests for matcher branching logic

Covers: _check_name_similarity, _try_match_receipt, _try_match_regpayment,
_try_regpayment_amount_mismatch, and match_all end-to-end branching.

These tests mock db_client and _check_name_similarity (where appropriate)
to exercise branch logic without real DB or LLM calls.

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
mock_config.DATE_TIER1_DAYS = 5
mock_config.DATE_TIER2_DAYS = 14
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
    """Reset all db_client return values before each test."""
    mock_db.reset_mock()
    mock_db.get_receipt_candidates.return_value = []
    mock_db.get_regpayment_candidates.return_value = []
    mock_db.get_regpayment_candidates_by_date.return_value = []


# ═══════════════════════════════════════════════════════════════════════════
# _check_name_similarity  (U42–U47)
# ═══════════════════════════════════════════════════════════════════════════

class TestCheckNameSimilarity:
    """Tests for _check_name_similarity. We mock _client.chat to control
    the LLM response, and mock _has_brand_overlap where needed."""

    def test_empty_candidate_name_no_llm_call(self):
        """U42: Empty candidate name → 'no_match', no LLM call."""
        with patch.object(matcher._client, "chat") as mock_chat:
            result = matcher._check_name_similarity("Kartenzahlung OBI", "")
            assert result == "no_match"
            mock_chat.assert_not_called()

    def test_llm_returns_match(self):
        """U43: LLM returns 'match' → returns 'match'."""
        with patch.object(matcher._client, "chat", return_value={
            "message": {"content": "match"}
        }):
            result = matcher._check_name_similarity(
                "Kartenzahlung OBI SAGT DANKE", "OBI GmbH"
            )
            assert result == "match"

    def test_llm_raises_exception_returns_uncertain(self):
        """U44: LLM raises exception → returns 'uncertain'."""
        with patch.object(matcher._client, "chat", side_effect=Exception("connection error")):
            result = matcher._check_name_similarity(
                "Kartenzahlung OBI SAGT DANKE", "OBI GmbH"
            )
            assert result == "uncertain"

    def test_llm_no_match_no_brand_overlap(self):
        """U45: LLM says 'no_match' + no brand overlap → 'no_match'."""
        with patch.object(matcher._client, "chat", return_value={
            "message": {"content": "no_match"}
        }), patch.object(matcher, "_has_brand_overlap", return_value=False):
            result = matcher._check_name_similarity(
                "SEPA Lastschrift TELEKOM", "REWE"
            )
            assert result == "no_match"

    def test_llm_no_match_with_brand_overlap_upgrades_to_uncertain(self):
        """U46: LLM says 'no_match' + clear brand overlap → upgraded to 'uncertain'."""
        with patch.object(matcher._client, "chat", return_value={
            "message": {"content": "no_match"}
        }), patch.object(matcher, "_has_brand_overlap", return_value=True):
            result = matcher._check_name_similarity(
                "Kartenzahlung OBI SAGT DANKE", "OBI GmbH"
            )
            assert result == "uncertain"

    def test_llm_no_match_compound_word_overlap_stays_no_match(self):
        """U47: LLM says 'no_match' + only compound-word (substring) overlap
        → should stay 'no_match' after H2 fix.
        Currently _has_brand_overlap('Lotto...', 'Otto') returns True
        because 'otto' is a substring of 'lotto', so the LLM's 'no_match'
        is incorrectly upgraded to 'uncertain'.
        Linked: H2"""
        with patch.object(matcher._client, "chat", return_value={
            "message": {"content": "no_match"}
        }):
            result = matcher._check_name_similarity(
                "Lotto Niedersachsen", "Otto"
            )
            assert result == "no_match"


# ═══════════════════════════════════════════════════════════════════════════
# _try_match_receipt  (U48–U56)
# ═══════════════════════════════════════════════════════════════════════════

class TestTryMatchReceipt:
    """Tests for _try_match_receipt. Mock db_client and _check_name_similarity."""

    def test_no_candidates(self):
        """U48: DB returns no candidates → (None, None)."""
        mock_db.get_receipt_candidates.return_value = []
        used = set()
        result, unc = matcher._try_match_receipt(
            make_tx("REWE", 43.20), used
        )
        assert result is None and unc is None

    def test_one_match_candidate(self):
        """U49: One match candidate → definitive MatchResult, id added to used_receipt_ids."""
        r = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=3)
        mock_db.get_receipt_candidates.return_value = [r]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            result, unc = matcher._try_match_receipt(
                make_tx("REWE SAGT DANKE", 43.20), used
            )
        assert result is not None
        assert result.matched_source == "receipt"
        assert result.matched_id == 1
        assert 1 in used
        assert unc is None

    def test_one_uncertain_candidate(self):
        """U50: One uncertain candidate → (None, candidate_dict), id NOT added."""
        r = make_receipt(id=2, issuer="Unbekannt", amount=22.50, days_before_bank=2)
        mock_db.get_receipt_candidates.return_value = [r]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="uncertain"):
            result, unc = matcher._try_match_receipt(
                make_tx("UNBEKANNTE BUCHUNG", 22.50), used
            )
        assert result is None
        assert unc is not None
        assert unc["id"] == 2
        assert 2 not in used

    def test_used_ids_filtered_out(self):
        """U51: Candidates already in used_receipt_ids are filtered out."""
        r1 = make_receipt(id=10, issuer="REWE", amount=43.20, days_before_bank=1)
        r2 = make_receipt(id=11, issuer="EDEKA", amount=43.20, days_before_bank=1)
        mock_db.get_receipt_candidates.return_value = [r1, r2]
        used = {10}
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            result, unc = matcher._try_match_receipt(
                make_tx("EDEKA MARKT", 43.20), used
            )
        assert result is not None
        assert result.matched_id == 11

    def test_mix_uncertain_then_match(self):
        """U52: [uncertain, match] in order → match wins, uncertain dropped."""
        r_unc = make_receipt(id=20, issuer="Unbekannt", amount=50.00, days_before_bank=2)
        r_match = make_receipt(id=21, issuer="EDEKA", amount=50.00, days_before_bank=1)
        mock_db.get_receipt_candidates.return_value = [r_unc, r_match]

        def fake_similarity(desc, name):
            if name == "Unbekannt":
                return "uncertain"
            return "match"

        used = set()
        with patch.object(matcher, "_check_name_similarity", side_effect=fake_similarity):
            result, unc = matcher._try_match_receipt(
                make_tx("EDEKA KAUF", 50.00), used
            )
        assert result is not None
        assert result.matched_id == 21
        assert unc is None
        assert 21 in used
        assert 20 not in used

    def test_two_match_candidates_first_wins(self):
        """U53: [match1, match2] → first match wins (lock in current behaviour)."""
        r1 = make_receipt(id=30, issuer="REWE", amount=30.00, days_before_bank=1)
        r2 = make_receipt(id=31, issuer="REWE Filiale", amount=30.00, days_before_bank=2)
        mock_db.get_receipt_candidates.return_value = [r1, r2]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            result, unc = matcher._try_match_receipt(
                make_tx("REWE", 30.00), used
            )
        assert result is not None
        assert result.matched_id == 30
        assert 30 in used
        assert 31 not in used

    def test_credit_direction_skips_receipt(self):
        """U54: Credit-direction tx → (None, None) immediately, no DB call.
        Currently _try_match_receipt does not check direction; after H1 fix
        it should return (None, None) for credit transactions without calling DB.
        Linked: H1"""
        mock_db.get_receipt_candidates.return_value = [
            make_receipt(id=1, issuer="REWE", amount=43.20, days_before_bank=1),
        ]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            result, unc = matcher._try_match_receipt(
                make_tx("REWE ERSTATTUNG", 43.20, direction="credit"), used
            )
        assert result is None and unc is None
        mock_db.get_receipt_candidates.assert_not_called()

    @pytest.mark.xfail(reason="H3: smallest-gap tiebreak not yet implemented")
    def test_two_matches_smallest_gap_wins(self):
        """U55: Two 'match' candidates with different gaps → smallest non-negative
        gap wins after H3 minimum fix.
        Currently the first candidate wins regardless of gap.
        Linked: H3"""
        r_close = make_receipt(id=40, issuer="Amazon", amount=29.99, days_before_bank=1)
        r_far = make_receipt(id=41, issuer="Amazon", amount=29.99, days_before_bank=10)
        mock_db.get_receipt_candidates.return_value = [r_far, r_close]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            result, unc = matcher._try_match_receipt(
                make_tx("AMAZON PAYMENTS", 29.99), used
            )
        assert result is not None
        assert result.matched_id == 40
        assert result.date_gap_days == 1

    @pytest.mark.xfail(reason="L12: empty issuer candidate should be skipped before LLM call")
    def test_empty_issuer_not_considered(self):
        """U56: Empty issuer candidate should be skipped entirely — no LLM call,
        no uncertain fallback. Currently _check_name_similarity returns
        'no_match' for empty strings, so the candidate doesn't match but the
        LLM IS still called. After L12 fix the candidate should be filtered
        before the LLM call is made.
        Linked: L12"""
        r = make_receipt(id=50, issuer="", amount=15.00, days_before_bank=1)
        mock_db.get_receipt_candidates.return_value = [r]
        used = set()
        with patch.object(matcher, "_check_name_similarity") as mock_sim:
            result, unc = matcher._try_match_receipt(
                make_tx("IRGENDWAS", 15.00), used
            )
        assert result is None and unc is None
        mock_sim.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# _try_match_regpayment  (U57–U62)
# ═══════════════════════════════════════════════════════════════════════════

class TestTryMatchRegpayment:
    """Tests for _try_match_regpayment. Mock db_client and _check_name_similarity."""

    def test_no_candidates(self):
        """U57: DB returns no candidates → (None, None)."""
        mock_db.get_regpayment_candidates.return_value = []
        used = set()
        result, unc = matcher._try_match_regpayment(
            make_tx("HAUSVERWALTUNG", 950.00, direction="debit"), -95000, used
        )
        assert result is None and unc is None

    def test_one_match(self):
        """U58: One match → definitive result, id committed."""
        rp = make_regpayment(id=10, reason="Miete", amount_cents=-95000)
        mock_db.get_regpayment_candidates.return_value = [rp]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            result, unc = matcher._try_match_regpayment(
                make_tx("HAUSVERWALTUNG MUSTER", 950.00, direction="debit"), -95000, used
            )
        assert result is not None
        assert result.matched_source == "regpayment"
        assert result.matched_id == 10
        assert 10 in used
        assert unc is None

    def test_one_uncertain(self):
        """U59: One uncertain → returned uncommitted."""
        rp = make_regpayment(id=11, reason="Miete", amount_cents=-95000)
        mock_db.get_regpayment_candidates.return_value = [rp]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="uncertain"):
            result, unc = matcher._try_match_regpayment(
                make_tx("HAUSVERWALTUNG MUSTER", 950.00, direction="debit"), -95000, used
            )
        assert result is None
        assert unc is not None
        assert unc["id"] == 11
        assert 11 not in used

    def test_used_ids_filtered(self):
        """U60: Already-used IDs filtered out."""
        rp1 = make_regpayment(id=20, reason="Miete", amount_cents=-95000)
        rp2 = make_regpayment(id=21, reason="Strom", amount_cents=-95000)
        mock_db.get_regpayment_candidates.return_value = [rp1, rp2]
        used = {20}
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            result, unc = matcher._try_match_regpayment(
                make_tx("STROMRECHNUNG", 950.00, direction="debit"), -95000, used
            )
        assert result is not None
        assert result.matched_id == 21

    def test_credit_income_finds_regpayment(self):
        """U61: Credit-direction income tx still attempts and finds regpayment match.
        This validates that H1 (receipt-only) does not affect regpayment matching.
        Linked: H1"""
        rp = make_regpayment(id=30, reason="Gehalt", amount_cents=250000)
        mock_db.get_regpayment_candidates.return_value = [rp]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            result, unc = matcher._try_match_regpayment(
                make_tx("ARBEITGEBER GMBH GEHALT", 2500.00, direction="credit"),
                250000, used
            )
        assert result is not None
        assert result.matched_source == "regpayment"
        assert result.matched_id == 30

    @pytest.mark.xfail(reason="L12: empty reason candidate should be skipped before LLM call")
    def test_empty_reason_not_considered(self):
        """U62: Empty reason candidate should be skipped entirely — no LLM call,
        no uncertain fallback. Currently _check_name_similarity returns
        'no_match' for empty strings, so the candidate doesn't match but the
        LLM IS still called. After L12 fix the candidate should be filtered
        before the LLM call is made.
        Linked: L12"""
        rp = make_regpayment(id=40, reason="", amount_cents=-1099)
        mock_db.get_regpayment_candidates.return_value = [rp]
        used = set()
        with patch.object(matcher, "_check_name_similarity") as mock_sim:
            result, unc = matcher._try_match_regpayment(
                make_tx("IRGENDWAS", 10.99, direction="debit"), -1099, used
            )
        assert result is None and unc is None
        mock_sim.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# _try_regpayment_amount_mismatch  (U63–U66)
# ═══════════════════════════════════════════════════════════════════════════

class TestTryRegpaymentAmountMismatch:
    """Tests for _try_regpayment_amount_mismatch."""

    def test_definitive_name_match(self):
        """U63: Definitive name match with different amount → AMOUNT_MISMATCH result."""
        rp = make_regpayment(id=12, reason="Handyvertrag", amount_cents=-3999)
        mock_db.get_regpayment_candidates_by_date.return_value = [rp]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            result = matcher._try_regpayment_amount_mismatch(
                make_tx("TELEKOM MOBILFUNK", 42.99, direction="debit"), used
            )
        assert result is not None
        assert result.status == matcher.AMOUNT_MISMATCH
        assert result.matched_id == 12
        assert any("Amount mismatch" in n for n in result.notes)

    def test_uncertain_name_match(self):
        """U64: Uncertain name match → AMOUNT_MISMATCH with 'uncertain' note."""
        rp = make_regpayment(id=13, reason="Netflix", amount_cents=-1299)
        mock_db.get_regpayment_candidates_by_date.return_value = [rp]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="uncertain"):
            result = matcher._try_regpayment_amount_mismatch(
                make_tx("NETFLIX ABO", 14.99, direction="debit"), used
            )
        assert result is not None
        assert result.status == matcher.AMOUNT_MISMATCH
        assert any("Uncertain" in n for n in result.notes)

    def test_no_name_match(self):
        """U65: No name match → None."""
        rp = make_regpayment(id=14, reason="Spotify", amount_cents=-1099)
        mock_db.get_regpayment_candidates_by_date.return_value = [rp]
        used = set()
        with patch.object(matcher, "_check_name_similarity", return_value="no_match"), \
             patch.object(matcher, "_has_brand_overlap", return_value=False):
            result = matcher._try_regpayment_amount_mismatch(
                make_tx("UNBEKANNTE BUCHUNG", 10.99, direction="debit"), used
            )
        assert result is None

    def test_used_id_still_considered_for_mismatch(self):
        """U66: Row already in used_regpayment_ids should still be considered for
        mismatch detection (Spotify price-hike scenario).
        The row should NOT be filtered out by `c["id"] not in used_regpayment_ids`
        because AMOUNT_MISMATCH is a diagnostic about the regpayment table, not
        a claim on the row.
        Linked: H5"""
        rp = make_regpayment(id=15, reason="Spotify", amount_cents=-1099)
        mock_db.get_regpayment_candidates_by_date.return_value = [rp]
        used = {15}
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            result = matcher._try_regpayment_amount_mismatch(
                make_tx("SPOTIFY PREMIUM", 11.99, direction="debit"), used
            )
        assert result is not None
        assert result.status == matcher.AMOUNT_MISMATCH


# ═══════════════════════════════════════════════════════════════════════════
# match_all end-to-end branching  (U67–U79)
# ═══════════════════════════════════════════════════════════════════════════

class TestMatchAll:
    """End-to-end tests for match_all branching logic."""

    def test_receipt_definitive_match(self):
        """U67: Receipt definitive match → uses receipt, regpayment never queried."""
        mock_db.get_receipt_candidates.return_value = [
            make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=3),
        ]
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])
        assert results[0].matched_source == "receipt"
        mock_db.get_regpayment_candidates.assert_not_called()

    def test_no_receipt_regpayment_definitive(self):
        """U68: No receipt, regpayment definitive → uses regpayment."""
        mock_db.get_receipt_candidates.return_value = []
        mock_db.get_regpayment_candidates.return_value = [
            make_regpayment(id=10, reason="Miete", amount_cents=-95000),
        ]
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([
                make_tx("HAUSVERWALTUNG MUSTER", 950.00, direction="debit"),
            ])
        assert results[0].matched_source == "regpayment"
        assert results[0].status == matcher.MATCHED

    def test_receipt_uncertain_regpayment_definitive(self):
        """U69: Receipt uncertain + regpayment definitive → regpayment wins
        (per TECHNICAL_SPEC §7.2: definitive beats uncertain from any source)."""
        mock_db.get_receipt_candidates.return_value = [
            make_receipt(id=1, issuer="Maybe REWE", amount=43.20, days_before_bank=3),
        ]
        mock_db.get_regpayment_candidates.return_value = [
            make_regpayment(id=10, reason="Miete", amount_cents=-4320),
        ]

        def fake_similarity(desc, name):
            if name == "Maybe REWE":
                return "uncertain"
            return "match"

        with patch.object(matcher, "_check_name_similarity", side_effect=fake_similarity):
            results = matcher.match_all([
                make_tx("HAUSVERWALTUNG", 43.20, direction="debit"),
            ])
        assert results[0].matched_source == "regpayment"
        assert results[0].matched_id == 10

    def test_both_uncertain_receipt_wins(self):
        """U70: Both uncertain → current code picks receipt_unc (lock in current behaviour)."""
        mock_db.get_receipt_candidates.return_value = [
            make_receipt(id=1, issuer="Maybe REWE", amount=43.20, days_before_bank=3),
        ]
        mock_db.get_regpayment_candidates.return_value = [
            make_regpayment(id=10, reason="Maybe Miete", amount_cents=-4320),
        ]
        with patch.object(matcher, "_check_name_similarity", return_value="uncertain"):
            results = matcher.match_all([
                make_tx("IRGENDWAS", 43.20, direction="debit"),
            ])
        assert results[0].matched_source == "receipt"

    def test_all_fail_amount_mismatch_hit(self):
        """U71: All paths fail + amount-mismatch hit → AMOUNT_MISMATCH."""
        mock_db.get_receipt_candidates.return_value = []
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = [
            make_regpayment(id=12, reason="Handyvertrag", amount_cents=-3999),
        ]
        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([
                make_tx("TELEKOM MOBILFUNK", 42.99, direction="debit"),
            ])
        assert results[0].status == matcher.AMOUNT_MISMATCH

    def test_all_fail_no_mismatch(self):
        """U72: All paths fail + no amount-mismatch → NO_MATCH."""
        mock_db.get_receipt_candidates.return_value = []
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []
        results = matcher.match_all([
            make_tx("UNBEKANNTE BUCHUNG", 7.50),
        ])
        assert results[0].status == matcher.NO_MATCH

    def test_one_to_one_constraint(self):
        """U73: Two txs vie for the same receipt → 1-to-1 constraint."""
        receipt = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=2)
        mock_db.get_receipt_candidates.return_value = [receipt]
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        tx1 = make_tx("REWE SAGT DANKE", 43.20, tx_date=date(2024, 4, 15))
        tx2 = make_tx("REWE MARKT 12345", 43.20, tx_date=date(2024, 4, 16))

        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([tx1, tx2])

        assert results[0].status == matcher.MATCHED
        assert results[1].status == matcher.NO_MATCH

    @pytest.mark.xfail(reason="H3: diagonal pairing not yet implemented — first-match wins currently")
    def test_order_dependence_diagonal_pairing(self):
        """U74: 2× €19.99 Amazon debits on D1/D2 + 2× receipts on D0/D1 —
        expect diagonal pairing after H3 (smallest-gap tiebreak).
        Currently the first receipt in the DB result always wins for tx1,
        which may leave a suboptimal pairing for tx2.
        Linked: H3"""
        r_d0 = make_receipt(id=1, issuer="Amazon", amount=19.99, days_before_bank=2,
                            file_name="receipt_d0.pdf")
        r_d1 = make_receipt(id=2, issuer="Amazon", amount=19.99, days_before_bank=1,
                            file_name="receipt_d1.pdf")
        mock_db.get_receipt_candidates.return_value = [r_d0, r_d1]
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        tx_d1 = make_tx("AMAZON PAYMENTS EU", 19.99, tx_date=date(2024, 4, 15))
        tx_d2 = make_tx("AMAZON PAYMENTS EU", 19.99, tx_date=date(2024, 4, 16))

        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([tx_d1, tx_d2])

        assert results[0].matched_id == 2
        assert results[0].date_gap_days == 1
        assert results[1].matched_id == 1
        assert results[1].date_gap_days == 1

    def test_stale_receipt_not_candidate(self):
        """U75: 2-year-old receipt of same amount must NOT be a candidate
        after H4 (date window enforcement). Stale candidates are filtered
        out of the candidate list before _check_name_similarity is called,
        so the transaction falls through to NO_MATCH.
        Linked: H4"""
        old_receipt = make_receipt(id=1, issuer="REWE", amount=43.20, days_before_bank=730)
        mock_db.get_receipt_candidates.return_value = [old_receipt]
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])

        assert results[0].status == matcher.NO_MATCH

    def test_stale_receipt_filtered_before_llm_call(self):
        """U75b: Stale receipt never reaches _check_name_similarity.
        The matcher-layer filter must drop candidates whose receipt_date
        is older than config.RECEIPT_DATE_WINDOW_DAYS before any LLM call.
        Linked: H4"""
        old_receipt = make_receipt(id=1, issuer="REWE", amount=43.20, days_before_bank=730)
        mock_db.get_receipt_candidates.return_value = [old_receipt]
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        with patch.object(matcher, "_check_name_similarity") as mock_sim:
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])

        assert results[0].status == matcher.NO_MATCH
        mock_sim.assert_not_called()

    def test_fresh_receipt_within_window_still_matches(self):
        """U75c: Receipt within the date window must still match — the
        H4 filter must not over-fire on legitimate candidates.
        Linked: H4"""
        fresh = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=3)
        mock_db.get_receipt_candidates.return_value = [fresh]
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])

        assert results[0].status == matcher.MATCHED
        assert results[0].matched_id == 1

    def test_receipt_at_window_boundary_matches(self):
        """U75d: A receipt exactly RECEIPT_DATE_WINDOW_DAYS old is still
        a valid candidate (boundary is inclusive). The delay status may
        be matched_unusual_delay because the window (28d) > DATE_TIER2_DAYS
        (14d) — the point of this test is that the candidate is NOT filtered.
        Linked: H4"""
        # mock_config.RECEIPT_DATE_WINDOW_DAYS == 28
        boundary = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=28)
        mock_db.get_receipt_candidates.return_value = [boundary]
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])

        assert results[0].matched_source == "receipt"
        assert results[0].matched_id == 1

    def test_receipt_one_day_beyond_window_filtered(self):
        """U75e: A receipt RECEIPT_DATE_WINDOW_DAYS+1 old is filtered out.
        Boundary is inclusive on the window side.
        Linked: H4"""
        stale = make_receipt(id=1, issuer="REWE GmbH", amount=43.20, days_before_bank=29)
        mock_db.get_receipt_candidates.return_value = [stale]
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])

        assert results[0].status == matcher.NO_MATCH

    def test_mixed_stale_and_fresh_only_fresh_considered(self):
        """U75f: When the DB returns a stale and a fresh receipt, only the
        fresh one reaches _check_name_similarity.
        Linked: H4"""
        stale = make_receipt(id=1, issuer="REWE", amount=43.20, days_before_bank=730)
        fresh = make_receipt(id=2, issuer="REWE GmbH", amount=43.20, days_before_bank=2)
        mock_db.get_receipt_candidates.return_value = [stale, fresh]
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        seen_ids: list[int] = []

        def fake_sim(_desc: str, _name: str) -> str:
            return "match"

        with patch.object(matcher, "_check_name_similarity", side_effect=fake_sim) as mock_sim:
            results = matcher.match_all([make_tx("REWE SAGT DANKE", 43.20)])

        assert results[0].status == matcher.MATCHED
        assert results[0].matched_id == 2
        # Only the fresh receipt should have been passed to the LLM.
        assert mock_sim.call_count == 1

    def test_stale_regpayment_spotify_scenario(self):
        """U76: tx A claims €10.99, tx B €11.99 → tx A MATCHED + tx B AMOUNT_MISMATCH.
        The regpayment row used for tx A must still be considered for
        amount-mismatch detection for tx B (diagnostic, not a claim on the row).
        Linked: H5"""
        rp = make_regpayment(id=1, reason="Spotify", amount_cents=-1099)
        mock_db.get_regpayment_candidates.return_value = [rp]
        mock_db.get_regpayment_candidates_by_date.return_value = [rp]

        tx_a = make_tx("SPOTIFY PREMIUM", 10.99, tx_date=date(2024, 4, 10))
        tx_b = make_tx("SPOTIFY PREMIUM", 11.99, tx_date=date(2024, 4, 15))

        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([tx_a, tx_b])

        assert results[0].status == matcher.MATCHED
        assert results[1].status == matcher.AMOUNT_MISMATCH

    def test_income_tx_matched_via_regpayment(self):
        """U77: Income tx (credit, salary) → MATCHED via regpayment.
        Validates H1 doesn't break income matching.
        Linked: H1"""
        mock_db.get_receipt_candidates.return_value = []
        mock_db.get_regpayment_candidates.return_value = [
            make_regpayment(id=1, reason="Gehalt", amount_cents=250000),
        ]
        mock_db.get_regpayment_candidates_by_date.return_value = []

        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([
                make_tx("ARBEITGEBER GMBH GEHALT", 2500.00, direction="credit"),
            ])

        assert results[0].status == matcher.MATCHED
        assert results[0].matched_source == "regpayment"

    def test_refund_tx_no_receipt_match(self):
        """U78: Refund tx (credit) where a same-amount purchase receipt exists →
        must NOT match the receipt; falls through. After H1 fix, credit
        transactions should skip receipt matching.
        Linked: H1"""
        mock_db.get_receipt_candidates.return_value = [
            make_receipt(id=1, issuer="Amazon", amount=29.99, days_before_bank=1),
        ]
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        with patch.object(matcher, "_check_name_similarity", return_value="match"):
            results = matcher.match_all([
                make_tx("AMAZON REFUND", 29.99, direction="credit"),
            ])

        assert results[0].matched_source != "receipt"

    def test_progress_log_includes_correct_count(self):
        """U79: match_all iterates with progress log including correct N/total.
        Light sanity check — verify all transactions are processed and
        results count matches input count.
        Linked: L14"""
        mock_db.get_receipt_candidates.return_value = []
        mock_db.get_regpayment_candidates.return_value = []
        mock_db.get_regpayment_candidates_by_date.return_value = []

        txs = [
            make_tx("TX1", 10.00, tx_date=date(2024, 4, 10)),
            make_tx("TX2", 20.00, tx_date=date(2024, 4, 11)),
            make_tx("TX3", 30.00, tx_date=date(2024, 4, 12)),
        ]

        results = matcher.match_all(txs)
        assert len(results) == 3
        assert all(r.status == matcher.NO_MATCH for r in results)
