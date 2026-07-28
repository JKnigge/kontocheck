"""
tests/test_matcher_helpers.py — pytest-style unit tests for pure helpers in matcher.py

Covers: _to_signed_cents, _strip_thinking, _has_brand_overlap,
_parse_verdict, _parse_choice_verdict, _build_candidate_choice_prompt.

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
mock_config.RECEIPT_DATE_WINDOW_DAYS = 28
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
        assert result in (-1999, -2000), f"Expected -1999 or -2000 for half-cent, got {result}"


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

    def test_expanded_noise_kartenzahlung(self):
        """Payment-method tokens like 'Kartenzahlung' should not count as
        brand overlap. After L15 fix they should be excluded.
        Linked: L15"""
        assert matcher._has_brand_overlap(
            "Kartenzahlung AMZ*1234",
            "Kartenzahlung Service GmbH",
        ) is False


# ═══════════════════════════════════════════════════════════════════════════
# _parse_verdict  (retained — used as fallback by _parse_choice_verdict)
# ═══════════════════════════════════════════════════════════════════════════

class TestParseVerdict:
    def test_plain_match(self):
        assert matcher._parse_verdict("match") == "match"

    def test_plain_no_match(self):
        assert matcher._parse_verdict("no_match") == "no_match"

    def test_plain_uncertain(self):
        assert matcher._parse_verdict("uncertain") == "uncertain"

    def test_match_with_period(self):
        """'match.' — first word is 'match.' which != 'match'.
        After M7 fix, punctuation should be stripped.
        Linked: M7"""
        assert matcher._parse_verdict("match.") == "match"

    def test_match_with_markdown_bold(self):
        """'**match**' — first word is '**match**' which != 'match'.
        After M7 fix, markdown formatting should be stripped.
        Linked: M7"""
        assert matcher._parse_verdict("**match**") == "match"

    def test_match_with_quotes(self):
        """'"match"' — first word is '"match"' which != 'match'.
        After M7 fix, quotes should be stripped.
        Linked: M7"""
        assert matcher._parse_verdict('"match"') == "match"

    def test_match_in_sentence(self):
        """'The answer is match' — first word is 'the' which != 'match'.
        After M7 fix, the verdict should be found anywhere in the text.
        Linked: M7"""
        assert matcher._parse_verdict("The answer is match") == "match"

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
# _parse_choice_verdict  (new — candidate-choice format)
# ═══════════════════════════════════════════════════════════════════════════

class TestParseChoiceVerdict:
    def test_match_single_index(self):
        assert matcher._parse_choice_verdict("match: 1") == ("match", [1])

    def test_match_two_digit_index(self):
        assert matcher._parse_choice_verdict("match: 12") == ("match", [12])

    def test_uncertain_multiple_indices(self):
        assert matcher._parse_choice_verdict("uncertain: 1,2,3") == ("uncertain", [1, 2, 3])

    def test_uncertain_space_separated(self):
        assert matcher._parse_choice_verdict("uncertain: 1 2 3") == ("uncertain", [1, 2, 3])

    def test_no_match(self):
        assert matcher._parse_choice_verdict("no_match") == ("no_match", [])

    def test_match_with_spaces_around_colon(self):
        assert matcher._parse_choice_verdict("match:   2") == ("match", [2])

    def test_match_uppercase(self):
        assert matcher._parse_choice_verdict("MATCH: 1") == ("match", [1])

    def test_match_with_markdown_bold_prefix(self):
        assert matcher._parse_choice_verdict("**match: 1**") == ("match", [1])

    def test_match_with_leading_dash(self):
        assert matcher._parse_choice_verdict("- match: 1") == ("match", [1])

    def test_uncertain_no_indices_keeps_verdict(self):
        """'uncertain' with no indices → verdict classified, empty indices."""
        assert matcher._parse_choice_verdict("uncertain") == ("uncertain", [])

    def test_match_no_indices_keeps_verdict(self):
        """'match' with no indices → verdict classified, empty indices."""
        assert matcher._parse_choice_verdict("match") == ("match", [])

    def test_garbage_defaults_to_no_match(self):
        assert matcher._parse_choice_verdict("asdf") == ("no_match", [])

    def test_empty_defaults_to_no_match(self):
        assert matcher._parse_choice_verdict("") == ("no_match", [])

    def test_match_with_thinking_block(self):
        """LLM wraps the verdict in a thinking block — _strip_thinking runs first."""
        text = f"{_THINK_OPEN}reasoning{_THINK_CLOSE}match: 1"
        assert matcher._parse_choice_verdict(text) == ("match", [1])


# ═══════════════════════════════════════════════════════════════════════════
# _build_candidate_choice_prompt
# ═══════════════════════════════════════════════════════════════════════════

class TestBuildCandidateChoicePrompt:
    @pytest.fixture()
    def tx(self):
        return Transaction(
            date=date(2024, 4, 15),
            description="Kartenzahlung OBI SAGT DANKE",
            amount=Decimal("43.20"),
            direction="debit",
        )

    @pytest.fixture()
    def candidates(self):
        return [
            {"id": 1, "issuer": "OBI GmbH", "total_amount": Decimal("43.20")},
            {"id": 2, "issuer": "REWE Filiale", "total_amount": Decimal("43.20")},
        ]

    def test_contains_tx_description(self, tx, candidates):
        prompt = matcher._build_candidate_choice_prompt(tx, candidates, "receipt")
        assert "Kartenzahlung OBI SAGT DANKE" in prompt

    def test_contains_all_candidate_names(self, tx, candidates):
        prompt = matcher._build_candidate_choice_prompt(tx, candidates, "receipt")
        assert "OBI GmbH" in prompt
        assert "REWE Filiale" in prompt

    def test_contains_numbered_list(self, tx, candidates):
        prompt = matcher._build_candidate_choice_prompt(tx, candidates, "receipt")
        assert "1. OBI GmbH" in prompt
        assert "2. REWE Filiale" in prompt

    def test_contains_source_label(self, tx, candidates):
        prompt = matcher._build_candidate_choice_prompt(tx, candidates, "receipt")
        assert "receipt" in prompt

    def test_contains_verdict_format_line(self, tx, candidates):
        prompt = matcher._build_candidate_choice_prompt(tx, candidates, "receipt")
        assert "match: <n>" in prompt
        assert "uncertain: <n,m,...>" in prompt
        assert "no_match" in prompt

    def test_conservative_variant_has_instruction(self, tx, candidates):
        prompt = matcher._build_candidate_choice_prompt(
            tx, candidates, "receipt", conservative=True
        )
        assert "conservative" in prompt.lower()
        assert "high" in prompt.lower()

    def test_non_conservative_has_no_conservative_instruction(self, tx, candidates):
        prompt = matcher._build_candidate_choice_prompt(
            tx, candidates, "receipt", conservative=False
        )
        assert "conservative" not in prompt.lower()

    def test_output_contract_last_line(self, tx, candidates):
        """The output contract line should be the LAST non-empty line."""
        prompt = matcher._build_candidate_choice_prompt(tx, candidates, "receipt")
        non_empty = [line for line in prompt.split("\n") if line.strip()]
        last_line = non_empty[-1]
        assert "Answer with exactly one line" in last_line, (
            f"Expected contract line as last non-empty line, got: {last_line!r}"
        )

    def test_regpayment_candidate_amount_rendered(self, tx):
        """Regpayment candidates store amount as signed cents — rendered as euros."""
        rp_candidates = [{"id": 10, "reason": "Miete", "amount": -95000}]
        prompt = matcher._build_candidate_choice_prompt(tx, rp_candidates, "regpayment")
        assert "€950.00" in prompt
