"""
tests/test_matcher_helpers.py — pytest-style unit tests for pure helpers in matcher.py

Covers: _to_signed_cents, _strip_thinking, _has_brand_overlap,
_compute_date_gap, _assign_delay_status, _parse_verdict,
_build_similarity_prompt.

These functions have no external dependencies (no DB, no Ollama), so
no mocking is required beyond the standard import-time config/ollama mock.

Run:  python -m pytest tests/test_matcher_helpers.py -v
"""

import importlib.util
import os
import sys
import types
from datetime import date
from decimal import Decimal
from unittest.mock import patch

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
mock_config.REGPAYMENT_USER_ID = 1
sys.modules["config"] = mock_config

from tests._helpers import Transaction

mock_extractor = types.ModuleType("pipeline.extractor")
mock_extractor.Transaction = Transaction
sys.modules["pipeline.extractor"] = mock_extractor

mock_db = types.ModuleType("storage.db_client")
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


# ═══════════════════════════════════════════════════════════════════════════
# _to_signed_cents
# ═══════════════════════════════════════════════════════════════════════════

class TestToSignedCents:
    def test_debit_43_20(self):
        assert matcher._to_signed_cents(Decimal("43.20"), "debit") == -4320

    def test_credit_2500(self):
        assert matcher._to_signed_cents(Decimal("2500.00"), "credit") == 250000

    def test_debit_0_01(self):
        assert matcher._to_signed_cents(Decimal("0.01"), "debit") == -1

    def test_debit_0_005_rounding(self):
        result = matcher._to_signed_cents(Decimal("0.005"), "debit")
        assert result in (0, -1), f"Expected 0 or -1 for half-cent, got {result}"

    def test_debit_19_995_rounding(self):
        result = matcher._to_signed_cents(Decimal("19.995"), "debit")
        assert result in (-1999, -2000), f"Expected -1999 or -2000, got {result}"


# ═══════════════════════════════════════════════════════════════════════════
# _strip_thinking
# ═══════════════════════════════════════════════════════════════════════════

# Build the tag strings without literal angle brackets in source so the
# source itself stays readable even in editors that interpret tags.
_THINK_OPEN = "\u003cthink\u003e"
_THINK_CLOSE = "\u003c/think\u003e"


class TestStripThinking:
    def test_paired_tags_removed(self):
        text = f"{_THINK_OPEN}reasoning here{_THINK_CLOSE}answer"
        assert matcher._strip_thinking(text) == "answer"

    def test_no_tags_unchanged(self):
        assert matcher._strip_thinking("plain answer") == "plain answer"

    def test_multiple_paired_blocks(self):
        text = f"{_THINK_OPEN}a{_THINK_CLOSE}mid{_THINK_OPEN}b{_THINK_CLOSE}end"
        assert matcher._strip_thinking(text) == "midend"

    @pytest.mark.xfail(reason="L13: unclosed think tag not stripped by current regex")
    def test_unclosed_tag_strips_reasoning(self):
        """Unclosed think tag: current regex does NOT match (no closing tag),
        so the reasoning remains. After L13 fix it should be stripped.
        Linked: L13"""
        text = f"{_THINK_OPEN}reasoning without close"
        assert matcher._strip_thinking(text) == ""

    def test_empty_input(self):
        assert matcher._strip_thinking("") == ""


# ═══════════════════════════════════════════════════════════════════════════
# _has_brand_overlap
# ═══════════════════════════════════════════════════════════════════════════

class TestHasBrandOverlap:
    def test_obi_brand_match(self):
        assert matcher._has_brand_overlap(
            "Kartenzahlung OBI.SAGT.DANKE/Hamburg/DE",
            "OBI GmbH",
        ) is True

    def test_rewe_vs_telekom_no_overlap(self):
        assert matcher._has_brand_overlap(
            "SEPA Lastschrift TELEKOM",
            "REWE",
        ) is False

    def test_only_noise_tokens_overlap(self):
        assert matcher._has_brand_overlap(
            "Sparkasse GmbH Deutschland",
            "GmbH Deutschland",
        ) is False

    def test_short_tokens_skipped(self):
        assert matcher._has_brand_overlap(
            "DM DROGERIE MARKT",
            "DM",
        ) is False

    def test_case_insensitive(self):
        assert matcher._has_brand_overlap(
            "Kartenzahlung obi.SAGT.DANKE",
            "OBI GmbH",
        ) is True

    def test_compound_word_otto_lotto(self):
        """Compound-word false positive: 'otto' is a substring of 'lotto'.
        Current code uses `in` (substring), so this returns True.
        After H2 fix it should return False.
        Linked: H2"""
        assert matcher._has_brand_overlap(
            "Lotto Niedersachsen",
            "Otto",
        ) is False

    def test_compound_word_baur_baumarkt(self):
        """Compound-word false positive: 'baur' is a substring of 'baumarkt'.
        After H2 fix should return False.
        Linked: H2"""
        assert matcher._has_brand_overlap(
            "Baumarkt Hamburg",
            "Baur",
        ) is False

    def test_compound_word_apo_apolda(self):
        """Compound-word false positive: 'apo' is a substring of 'apolda'.
        After H2 fix should return False.
        Linked: H2"""
        assert matcher._has_brand_overlap(
            "Apolda Stadtkasse",
            "Apo",
        ) is False

    @pytest.mark.xfail(reason="L15: payment-method noise tokens like 'Kartenzahlung' not excluded")
    def test_expanded_noise_kartenzahlung(self):
        """Payment-method tokens like 'Kartenzahlung' should not count as
        brand overlap. After L15 fix they should be excluded.
        Linked: L15"""
        assert matcher._has_brand_overlap(
            "Kartenzahlung AMZ*1234",
            "Kartenzahlung Service GmbH",
        ) is False


# ═══════════════════════════════════════════════════════════════════════════
# _compute_date_gap
# ═══════════════════════════════════════════════════════════════════════════

class TestComputeDateGap:
    def test_same_day(self):
        assert matcher._compute_date_gap(date(2024, 4, 15), date(2024, 4, 15)) == 0

    def test_receipt_5_days_before(self):
        assert matcher._compute_date_gap(date(2024, 4, 15), date(2024, 4, 10)) == 5

    def test_receipt_after_bank_negative(self):
        assert matcher._compute_date_gap(date(2024, 4, 15), date(2024, 4, 20)) == -5


# ═══════════════════════════════════════════════════════════════════════════
# _assign_delay_status  — uses mock config: TIER1=5, TIER2=14
# ═══════════════════════════════════════════════════════════════════════════

class TestAssignDelayStatus:
    def test_gap_0_matched(self):
        assert matcher._assign_delay_status(0) == matcher.MATCHED

    def test_gap_5_tier1_boundary(self):
        assert matcher._assign_delay_status(5) == matcher.MATCHED

    def test_gap_6_large_delay(self):
        assert matcher._assign_delay_status(6) == matcher.MATCHED_LARGE_DELAY

    def test_gap_14_tier2_boundary(self):
        assert matcher._assign_delay_status(14) == matcher.MATCHED_LARGE_DELAY

    def test_gap_15_unusual_delay(self):
        assert matcher._assign_delay_status(15) == matcher.MATCHED_UNUSUAL_DELAY


# ═══════════════════════════════════════════════════════════════════════════
# _parse_verdict  (U28–U37)
# ═══════════════════════════════════════════════════════════════════════════

class TestParseVerdict:
    def test_plain_match(self):
        assert matcher._parse_verdict("match") == "match"

    def test_plain_no_match(self):
        assert matcher._parse_verdict("no_match") == "no_match"

    def test_plain_uncertain(self):
        assert matcher._parse_verdict("uncertain") == "uncertain"

    @pytest.mark.xfail(reason="M7: _parse_verdict doesn't strip punctuation from first word")
    def test_match_with_period(self):
        """'match.' — first word is 'match.' which != 'match'.
        After M7 fix, punctuation should be stripped.
        Linked: M7"""
        assert matcher._parse_verdict("match.") == "match"

    @pytest.mark.xfail(reason="M7: _parse_verdict doesn't strip markdown formatting")
    def test_match_with_markdown_bold(self):
        """'**match**' — first word is '**match**' which != 'match'.
        After M7 fix, markdown formatting should be stripped.
        Linked: M7"""
        assert matcher._parse_verdict("**match**") == "match"

    @pytest.mark.xfail(reason="M7: _parse_verdict doesn't strip quotes from first word")
    def test_match_with_quotes(self):
        """'"match"' — first word is '"match"' which != 'match'.
        After M7 fix, quotes should be stripped.
        Linked: M7"""
        assert matcher._parse_verdict('"match"') == "match"

    @pytest.mark.xfail(reason="M7: _parse_verdict only checks first word, not full text")
    def test_match_in_sentence(self):
        """'The answer is match' — first word is 'the' which != 'match'.
        After M7 fix, the verdict should be found anywhere in the text.
        Linked: M7"""
        assert matcher._parse_verdict("The answer is match") == "match"

    @pytest.mark.xfail(reason="M7: _parse_verdict only checks first word; verdict in sentence not detected")
    def test_verdict_in_sentence(self):
        """'Sure! uncertain' — first word is 'sure!' which != 'uncertain'.
        Currently returns 'no_match' by default (wrong). After M7 fix,
        the verdict should be found anywhere in the text.
        Using 'uncertain' instead of 'no_match' so the current wrong
        behaviour produces a different value (no_match vs uncertain),
        making the defect visible as an xfail rather than an accidental pass.
        Linked: M7"""
        assert matcher._parse_verdict("Sure! uncertain") == "uncertain"

    def test_empty_defaults_to_no_match(self):
        """Empty string → sensible default. Lock in current behaviour.
        Linked: M7"""
        assert matcher._parse_verdict("") == "no_match"

    def test_garbage_defaults_to_no_match(self):
        """Unrecognisable input → no_match. Lock in current behaviour.
        Linked: M7"""
        assert matcher._parse_verdict("asdf") == "no_match"


# ═══════════════════════════════════════════════════════════════════════════
# _build_similarity_prompt  (U38–U41)
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildSimilarityPrompt:
    @pytest.fixture()
    def sample_prompt(self):
        return matcher._build_similarity_prompt(
            "Kartenzahlung OBI SAGT DANKE",
            "OBI GmbH & Co. Deutschland KG",
        )

    def test_contains_both_strings(self, sample_prompt):
        assert "Kartenzahlung OBI SAGT DANKE" in sample_prompt
        assert "OBI GmbH & Co. Deutschland KG" in sample_prompt

    @pytest.mark.xfail(reason="M6: output contract line should be the last line (after data), currently before data")
    def test_output_contract_last_line(self, sample_prompt):
        """The output contract line (You MUST reply...) should be the
        LAST non-empty line of the prompt, so the LLM's last seen context
        is the instruction on how to respond.
        Currently the data (Bank statement / Candidate name) comes after
        the contract, so the last line is data — the M6 fix should move
        the contract to the end.
        Linked: M6"""
        non_empty = [line for line in sample_prompt.split("\n") if line.strip()]
        last_line = non_empty[-1]
        assert 'You MUST reply' in last_line, (
            f"Expected contract line as last non-empty line, got: {last_line!r}"
        )

    @pytest.mark.xfail(reason="M6: no blank line between output contract and data fields")
    def test_newline_separates_contract_and_data(self, sample_prompt):
        """At least one blank line (\\n\\n) must separate the output
        contract instruction from the bank/candidate data.
        Current prompt concatenates them without a blank separator.
        Linked: M6"""
        contract_ending = 'or "uncertain".'
        data_beginning = "Bank statement description:"
        idx_contract = sample_prompt.find(contract_ending)
        idx_data = sample_prompt.find(data_beginning)
        assert idx_contract != -1 and idx_data != -1
        between = sample_prompt[idx_contract + len(contract_ending):idx_data]
        assert "\n\n" in between, (
            f"Expected at least one blank line between contract and data, "
            f"got between: {between!r}"
        )

    def test_contains_verdict_tokens(self, sample_prompt):
        assert '"match"' in sample_prompt
        assert '"no_match"' in sample_prompt
        assert '"uncertain"' in sample_prompt
