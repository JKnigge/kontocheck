"""
tests/test_matcher_llm_integration.py — opt-in integration tests with real Ollama

These tests verify the LLM candidate-choice prompt design by calling a real
Ollama instance. They are skipped by default; run with:
    python -m pytest -m integration tests/test_matcher_llm_integration.py -v

Marked @pytest.mark.integration so they only run when explicitly requested.
Each case verifies *the prompt itself* on real model behaviour — by definition
not mockable, because the thing under test IS the model's reaction to the prompt.

The tests call _choose_candidate with a single-candidate list and assert the
verdict (match/uncertain/no_match) matches the expected set. This exercises
the _build_candidate_choice_prompt → LLM → _parse_choice_verdict pipeline.
"""

import importlib.util
import os
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env so OLLAMA_URL and OLLAMA_MODEL come from the real project config
# when available.  Must happen before setting mock_config values.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

mock_config = types.ModuleType("config")
mock_config.OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
mock_config.OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "deepseek-r1")
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
# Do NOT mock ollama.Client here — we want the real LLM connection.
_spec.loader.exec_module(_mod)

matcher = _mod


pytestmark = pytest.mark.integration


# Each case sends a single-candidate list to _choose_candidate and checks
# that the verdict falls within the expected set. The candidate's amount
# matches the tx amount so the candidate would be an amount-matching one.
INTEGRATION_CASES = [
    # (id, bank_description, candidate_name, amount, expected_verdicts, verifies)
    (
        "I1",
        "Kartenzahlung OBI.SAGT.DANKE/Hamburg/DE",
        "OBI GmbH & Co. Deutschland KG",
        43.20,
        {"match"},
        "Clean brand-token case — sanity smoke test",
    ),
    (
        "I2",
        "Basislastschrift EDEKA SAGT DANKE/BERLIN",
        "EDEKA Müller oHG",
        43.20,
        {"match"},
        "German chain with regional operator",
    ),
    (
        "I3",
        "Kartenzahlung Stadtwerke Hamburg",
        "Stadtwerke München AG",
        43.20,
        {"no_match", "uncertain"},
        "M9 — generic-token collision must NOT count as match",
    ),
    (
        "I4",
        "POS 4711 //DE",
        "OBI Bau- und Heimwerkermärkte",
        43.20,
        {"uncertain"},
        "M9 — truncated description must trigger uncertain, not no_match",
    ),
    (
        "I5",
        "SEPA Überweisung MUELLER J M",
        "Jan Müller",
        43.20,
        {"match"},
        "Personal-name case with abbreviated initials",
    ),
]


@pytest.fixture(scope="module")
def ollama_available():
    """Check if Ollama is reachable; skip module if not."""
    try:
        import ollama
        client = ollama.Client(host=mock_config.OLLAMA_URL)
        client.list()
        return True
    except Exception:
        pytest.skip("Ollama not reachable — skipping integration tests")
        return False


def _make_tx_and_candidate(bank_desc, candidate_name, amount):
    """Build a Transaction and a single-candidate list (receipt format)."""
    tx = Transaction(
        date=date(2024, 4, 15),
        description=bank_desc,
        amount=Decimal(str(amount)),
        direction="debit",
    )
    candidate = {
        "id": 1,
        "issuer": candidate_name,
        "total_amount": Decimal(str(amount)),
        "__source": "receipt",
    }
    return tx, [candidate]


@pytest.mark.parametrize(
    "case_id,bank_desc,candidate_name,amount,expected_verdicts,verifies",
    INTEGRATION_CASES,
    ids=[c[0] for c in INTEGRATION_CASES],
)
def test_choose_candidate(case_id, bank_desc, candidate_name, amount,
                          expected_verdicts, verifies, ollama_available):
    """Run _choose_candidate against real Ollama with a single candidate and
    verify the verdict matches the expected set."""
    tx, candidates = _make_tx_and_candidate(bank_desc, candidate_name, amount)
    verdict, indices = matcher._choose_candidate(tx, candidates, "receipt")
    assert verdict in expected_verdicts, (
        f"{case_id}: expected one of {expected_verdicts}, got '{verdict}' "
        f"(indices={indices}) for '{bank_desc}' vs '{candidate_name}' ({verifies})"
    )
