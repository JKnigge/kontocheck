"""
pipeline/matcher.py — transaction matching engine for kontocheck

Responsibilities:
  - Match each bank statement transaction against receipts and regpayment DB rows
  - Enforce 1-to-1 constraint: each DB row matched to at most one transaction
  - Use LLM for name similarity (handles "XYZ Systemgastronomie GmbH" → "McDonald's")
  - Assign a status verdict to each transaction
  - Return one MatchResult per transaction

Matching order per transaction:
  1. receipts table (exact amount + date constraint + name similarity)
  2. regpayment table (exact signed cents + date range + name similarity)
  3. regpayment amount mismatch (name similarity only, amount differs)
  4. NO_MATCH

Status constants are defined at module level and used by report.py.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

import ollama

import config
from pipeline.extractor import Transaction
from storage import db_client

logger = logging.getLogger(__name__)


# ── Status constants ──────────────────────────────────────────────────────────

MATCHED               = "matched"
MATCHED_LARGE_DELAY   = "matched_large_delay"
MATCHED_UNUSUAL_DELAY = "matched_unusual_delay"
MATCHED_UNREVIEWED    = "matched_unreviewed"
AMOUNT_MISMATCH       = "amount_mismatch"
NO_MATCH              = "no_match"

# Display strings for use in report.py
STATUS_DISPLAY = {
    MATCHED:               "✅ Matched",
    MATCHED_LARGE_DELAY:   "⚠️ Matched — large delay",
    MATCHED_UNUSUAL_DELAY: "⚠️ Matched — unusual delay",
    MATCHED_UNREVIEWED:    "⚠️ Matched — please verify",
    AMOUNT_MISMATCH:       "⚠️ Amount mismatch",
    NO_MATCH:              "❌ No match found",
}

# Module-level Ollama client — created once, reused for all similarity calls
_client = ollama.Client(host=config.OLLAMA_URL)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    """
    The outcome of matching one bank statement transaction.
    One MatchResult is produced per transaction regardless of outcome.
    """
    transaction:    Transaction
    status:         str
    matched_source: Optional[str]      = None   # "receipt" | "regpayment" | None
    matched_id:     Optional[int]      = None   # DB row id of matched record
    matched_name:   Optional[str]      = None   # issuer or reason for display
    matched_file:   Optional[str]      = None   # receipts.file_name if applicable
    date_gap_days:  Optional[int]      = None   # days between receipt and bank date
    notes:          list[str]          = field(default_factory=list)


# ── Amount conversion ─────────────────────────────────────────────────────────

def _to_signed_cents(amount: Decimal, direction: str) -> int:
    """
    Convert a transaction amount (euros, always positive) to signed cents
    for comparison against the regpayment table.

    direction="debit"  → negative cents (money leaving the account)
    direction="credit" → positive cents (money entering the account)

    Uses round() before int() to avoid silent truncation of fractional cents
    caused by Decimal arithmetic edge cases.
    """
    cents = int(round(amount * 100))
    return -cents if direction == "debit" else cents


# ── LLM name similarity ───────────────────────────────────────────────────────

def _strip_thinking(text: str) -> str:
    """Remove DeepSeek-R1 <think>...</think> blocks as a defensive fallback."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _check_name_similarity(bank_description: str, candidate_name: str) -> str:
    """
    Ask the LLM whether a bank statement description and a candidate name
    refer to the same entity.

    Returns: "match" | "no_match" | "uncertain"
    Falls back to "uncertain" on any LLM error so the candidate is kept
    as a fallback rather than silently discarded.
    """
    if not candidate_name.strip():
        return "no_match"

    prompt = (
        f'Could the bank statement description "{bank_description}" '
        f'refer to the same entity as "{candidate_name}"?\n\n'
        'Reply with exactly one word: "match", "no_match", or "uncertain".'
    )
    try:
        response = _client.chat(
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0, "num_predict": 50},
            think=False,
        )
        content = _strip_thinking(response["message"]["content"] or "")
        # Check the first word only — we asked for exactly one word
        first_word = content.strip().lower().split()[0] if content.strip() else ""
        if first_word == "match":
            return "match"
        if first_word == "uncertain":
            return "uncertain"
        return "no_match"
    except Exception as exc:
        logger.warning("Name similarity LLM call failed: %s — treating as uncertain", exc)
        return "uncertain"


# ── Date helpers ──────────────────────────────────────────────────────────────

def _compute_date_gap(bank_date: date, receipt_date: date) -> int:
    """Return the number of days between receipt date and bank booking date."""
    return (bank_date - receipt_date).days


def _assign_delay_status(date_gap_days: int) -> str:
    """Map a date gap in days to the appropriate status constant."""
    if date_gap_days <= config.DATE_TIER1_DAYS:
        return MATCHED
    if date_gap_days <= config.DATE_TIER2_DAYS:
        return MATCHED_LARGE_DELAY
    return MATCHED_UNUSUAL_DELAY


# ── Receipt matching ──────────────────────────────────────────────────────────

def _try_match_receipt(
    tx: Transaction,
    used_receipt_ids: set[int],
) -> Optional[MatchResult]:
    """
    Attempt to match a transaction against the receipts table.
    Queries by exact euro amount with date constraint, then checks name
    similarity for each candidate. Returns the first definitive match,
    or the first uncertain candidate as a fallback, or None.
    """
    candidates = db_client.get_receipt_candidates(tx.amount, tx.date)
    candidates = [c for c in candidates if c["id"] not in used_receipt_ids]

    if not candidates:
        return None

    uncertain_fallback: Optional[tuple] = None

    for c in candidates:
        issuer = c["issuer"] or ""
        similarity = _check_name_similarity(tx.description, issuer)

        if similarity == "match":
            return _build_receipt_result(tx, c, used_receipt_ids, uncertain=False)

        if similarity == "uncertain" and uncertain_fallback is None:
            uncertain_fallback = c

    if uncertain_fallback is not None:
        return _build_receipt_result(tx, uncertain_fallback, used_receipt_ids, uncertain=True)

    return None


def _build_receipt_result(
    tx: Transaction,
    c: dict,
    used_receipt_ids: set[int],
    uncertain: bool,
) -> MatchResult:
    """Build a MatchResult for a receipt candidate and mark it as used."""
    gap = _compute_date_gap(tx.date, c["receipt_date"])
    status = _assign_delay_status(gap)
    notes: list[str] = []

    if uncertain:
        notes.append("Uncertain name match — please verify")

    # Bonus feature: flag receipts that belegbot marked for review but
    # that have not been manually checked yet
    if c.get("manually_checked") is None and c.get("confidence") != "high":
        status = MATCHED_UNREVIEWED
        notes.append("Receipt was flagged by belegbot and may not have been manually reviewed")

    if gap > config.DATE_TIER1_DAYS:
        notes.append(f"Date gap: {gap} days between receipt date and bank booking")

    used_receipt_ids.add(c["id"])
    return MatchResult(
        transaction=tx,
        status=status,
        matched_source="receipt",
        matched_id=c["id"],
        matched_name=c["issuer"] or "",
        matched_file=c.get("file_name"),
        date_gap_days=gap,
        notes=notes,
    )


# ── Regpayment matching ───────────────────────────────────────────────────────

def _try_match_regpayment(
    tx: Transaction,
    signed_cents: int,
    used_regpayment_ids: set[int],
) -> Optional[MatchResult]:
    """
    Attempt to match a transaction against the regpayment table by exact
    signed-cent amount within the valid date range.
    Returns the first definitive match, uncertain fallback, or None.
    """
    candidates = db_client.get_regpayment_candidates(signed_cents, tx.date)
    candidates = [c for c in candidates if c["id"] not in used_regpayment_ids]

    if not candidates:
        return None

    uncertain_fallback: Optional[dict] = None

    for c in candidates:
        reason = c["reason"] or ""
        similarity = _check_name_similarity(tx.description, reason)

        if similarity == "match":
            return _build_regpayment_result(tx, c, used_regpayment_ids, uncertain=False)

        if similarity == "uncertain" and uncertain_fallback is None:
            uncertain_fallback = c

    if uncertain_fallback is not None:
        return _build_regpayment_result(tx, uncertain_fallback, used_regpayment_ids, uncertain=True)

    return None


def _build_regpayment_result(
    tx: Transaction,
    c: dict,
    used_regpayment_ids: set[int],
    uncertain: bool,
    amount_mismatch: bool = False,
) -> MatchResult:
    """Build a MatchResult for a regpayment candidate and mark it as used."""
    notes: list[str] = []

    if uncertain:
        notes.append("Uncertain name match — please verify")

    if amount_mismatch:
        expected_euros = abs(c["amount"]) / 100
        notes.append(
            f"Amount mismatch: expected €{expected_euros:.2f}, "
            f"actual €{tx.amount:.2f} — update regpayment table if correct"
        )

    status = AMOUNT_MISMATCH if amount_mismatch else MATCHED

    used_regpayment_ids.add(c["id"])
    return MatchResult(
        transaction=tx,
        status=status,
        matched_source="regpayment",
        matched_id=c["id"],
        matched_name=c["reason"] or "",
        matched_file=None,
        date_gap_days=None,
        notes=notes,
    )


# ── Regpayment amount mismatch detection ──────────────────────────────────────

def _try_regpayment_amount_mismatch(
    tx: Transaction,
    used_regpayment_ids: set[int],
) -> Optional[MatchResult]:
    """
    Last-resort check: query ALL regpayment rows active on the transaction
    date (regardless of amount) and look for a name match. If the name
    matches but the amount differs, return an AMOUNT_MISMATCH result.

    This handles the case where a regular payment has changed its amount
    and the regpayment table hasn't been updated yet.
    """
    all_candidates = db_client.get_regpayment_candidates_by_date(tx.date)
    candidates = [c for c in all_candidates if c["id"] not in used_regpayment_ids]

    if not candidates:
        return None

    uncertain_fallback: Optional[dict] = None

    for c in candidates:
        reason = c["reason"] or ""
        similarity = _check_name_similarity(tx.description, reason)

        if similarity == "match":
            return _build_regpayment_result(
                tx, c, used_regpayment_ids,
                uncertain=False, amount_mismatch=True,
            )

        if similarity == "uncertain" and uncertain_fallback is None:
            uncertain_fallback = c

    if uncertain_fallback is not None:
        return _build_regpayment_result(
            tx, uncertain_fallback, used_regpayment_ids,
            uncertain=True, amount_mismatch=True,
        )

    return None


# ── Public interface ──────────────────────────────────────────────────────────

def match_all(transactions: list[Transaction]) -> list[MatchResult]:
    """
    Match all transactions and return one MatchResult per transaction.

    Processes transactions in chronological order. Maintains used_receipt_ids
    and used_regpayment_ids sets to enforce the 1-to-1 matching constraint
    across the full run.
    """
    used_receipt_ids:    set[int] = set()
    used_regpayment_ids: set[int] = set()

    results: list[MatchResult] = []

    for tx in sorted(transactions, key=lambda t: t.date):
        signed_cents = _to_signed_cents(tx.amount, tx.direction)

        result = (
            _try_match_receipt(tx, used_receipt_ids)
            or _try_match_regpayment(tx, signed_cents, used_regpayment_ids)
            or _try_regpayment_amount_mismatch(tx, used_regpayment_ids)
            or MatchResult(transaction=tx, status=NO_MATCH)
        )

        results.append(result)
        logger.debug(
            "%-40s  %s  →  %s",
            tx.description[:40],
            f"€{tx.amount:.2f} ({tx.direction})",
            STATUS_DISPLAY.get(result.status, result.status),
        )

    n_matched   = sum(1 for r in results if r.status != NO_MATCH)
    n_unmatched = sum(1 for r in results if r.status == NO_MATCH)
    logger.info(
        "Matching complete: %d transactions — %d matched, %d unmatched",
        len(results), n_matched, n_unmatched,
    )
    return results
