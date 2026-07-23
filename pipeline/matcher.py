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
from datetime import date, timedelta
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
    """Remove DeepSeek-R1 thinking blocks as a defensive fallback."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _parse_verdict(text: str) -> str:
    """Return 'match' | 'no_match' | 'uncertain' from a raw LLM reply.

    Defaults to 'no_match' when no recognisable verdict is found.
    Scans the whole reply for the first occurrence of any known verdict
    keyword, so malformed outputs (punctuation, markdown, quotes, or a
    verdict embedded in a sentence) are still classified correctly.
    """
    tokens = re.findall(r"[a-z_]+", text.lower())
    first_word = next((t for t in tokens if t in {"match", "no_match", "uncertain"}), "")
    if first_word == "match":
        return "match"
    if first_word == "uncertain":
        return "uncertain"
    return "no_match"


# Legal-entity suffixes and generic stopwords stripped before brand-token
# overlap is computed. Without this, "GmbH" or "Deutschland" alone would
# trigger spurious matches across totally unrelated companies.
_BRAND_NOISE_TOKENS = frozenset({
    "gmbh", "ag", "kg", "ohg", "ug", "kgaa", "se", "ek", "ev",
    "co", "company", "ltd", "llc", "inc", "corp",
    "deutschland", "germany", "international",
    "und", "and", "der", "die", "das", "the", "von",
})


def _has_brand_overlap(bank_description: str, candidate_name: str) -> bool:
    """
    Return True if any meaningful brand token (>=3 letters, not a legal-
    entity suffix or stopword) from the candidate name appears as a whole
    word in the bank description, case-insensitive.

    Used as a safety net after the LLM verdict: receipts and regpayment
    candidates already have matching amount and date, so a single shared
    brand token (e.g. "OBI" in "OBI GmbH & Co. Deutschland KG" vs.
    "Kartenzahlung OBI.SAGT.DANKE/Hamburg/DE") is a strong signal that the
    candidate should not be silently discarded.

    Both sides are tokenized so that compound-word false positives (e.g.
    "Otto" matching "Lotto") are avoided.
    """
    desc_tokens = {t.lower() for t in re.findall(r"[A-Za-zÄÖÜäöüß0-9]+", bank_description)}
    for raw_token in re.findall(r"[A-Za-zÄÖÜäöüß0-9]+", candidate_name):
        token = raw_token.lower()
        if len(token) < 3 or token in _BRAND_NOISE_TOKENS:
            continue
        if token in desc_tokens:
            return True
    return False


def _build_similarity_prompt(bank_description: str, candidate_name: str) -> str:
    """Build the LLM prompt for name-similarity verification.

    The prompt explicitly defines the 'uncertain' verdict (M9) so the model
    does not collapse to a binary match/no_match decision, and includes
    German few-shot examples that small models weight heavily.
    """
    return (
        f'Bank statement description: "{bank_description}"\n'
        f'Candidate name: "{candidate_name}"\n\n'
        f'These two strings were already matched by amount and date. Decide '
        f'whether they refer to the same merchant/payee.\n\n'
        f'German bank descriptions are mangled (e.g. '
        f'"Kartenzahlung OBI.SAGT.DANKE/Hamburg/DE" for "OBI GmbH & Co. '
        f'Deutschland KG"). Ignore legal suffixes (GmbH, AG, KG), cities, '
        f'terminal IDs, and payment prefixes.\n\n'
        f'Rules:\n'
        f'- match: a distinctive brand/name token clearly identifies the '
        f'same entity.\n'
        f'- no_match: names clearly refer to different entities.\n'
        f'- uncertain: description is too truncated or abbreviated to '
        f'decide, OR only a generic token overlaps (e.g. "Stadtwerke", '
        f'"Apotheke", "Tankstelle").\n\n'
        f'Examples:\n'
        f'  "EDEKA SAGT DANKE//BERLIN" vs "EDEKA Müller oHG" -> match\n'
        f'  "Kartenzahlung Stadtwerke Hamburg" vs "Stadtwerke München AG" '
        f'-> no_match\n'
        f'  "POS 4711 //DE" vs "OBI Bau- und Heimwerkermärkte" -> uncertain\n\n'
        f'Answer with exactly one lowercase word and nothing else: match, '
        f'no_match, or uncertain.'
    )


def _check_name_similarity(bank_description: str, candidate_name: str) -> str:
    """
    Ask the LLM whether a bank statement description and a candidate name
    refer to the same entity.

    Returns: "match" | "no_match" | "uncertain"
    Falls back to "uncertain" on any LLM error so the candidate is kept
    as a fallback rather than silently discarded.

    Candidates reaching this function already have matching amount and date,
    so we bias toward keeping them: a clear shared brand token overrides an
    LLM "no_match" verdict to "uncertain" so it surfaces as a suggestion in
    the final report instead of being dropped.
    """
    if not candidate_name.strip():
        return "no_match"

    prompt = _build_similarity_prompt(bank_description, candidate_name)
    try:
        response = _client.chat(
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0, "num_predict": 8000, "num_ctx": 32768},
            think=False,
        )
        content = _strip_thinking(response["message"]["content"] or "")
        verdict = _parse_verdict(content)
        if verdict == "match":
            return "match"
        if verdict == "uncertain":
            return "uncertain"
        # LLM said "no_match" (or unparseable). Apply brand-overlap safety
        # net so a clear shared token still surfaces as a suggestion.
        if _has_brand_overlap(bank_description, candidate_name):
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
    ) -> tuple[Optional[MatchResult], Optional[dict]]:
    """
    Attempt to match a transaction against the receipts table.

    Returns a tuple (definitive_result, uncertain_candidate):
      - If a candidate's name similarity is "match": the first such candidate
        is built into a MatchResult (which also commits the id to
        used_receipt_ids) and returned as definitive_result.
      - Otherwise the first "uncertain" candidate (if any) is returned as
        uncertain_candidate WITHOUT being committed. The caller decides
        whether to fall back to it after checking regpayment for a
        definitive match (per TECHNICAL_SPEC §7.2 step 6).
    """
    if tx.direction != "debit":
        return (None, None)

    candidates = db_client.get_receipt_candidates(tx.amount, tx.date)
    candidates = [c for c in candidates if c["id"] not in used_receipt_ids]

    # H4: defense-in-depth — even when the SQL layer already filters stale
    # receipts via DATE_SUB, drop any candidate whose receipt_date is older
    # than the configured window. This keeps stale receipts from reaching
    # _check_name_similarity (and thus the LLM) when the DB query is mocked
    # or the window is widened at runtime.
    window_days = getattr(config, "RECEIPT_DATE_WINDOW_DAYS", None)
    if window_days:
        lower_bound = tx.date - timedelta(days=window_days)
        candidates = [
            c for c in candidates
            if c.get("receipt_date") is None or c["receipt_date"] >= lower_bound
        ]

    if not candidates:
        return (None, None)

    uncertain_fallback: Optional[dict] = None

    for c in candidates:
        issuer = c["issuer"] or ""
        similarity = _check_name_similarity(tx.description, issuer)

        if similarity == "match":
            used_receipt_ids.add(c["id"])
            return (_build_receipt_result(tx, c, uncertain=False), None)

        if similarity == "uncertain" and uncertain_fallback is None:
            uncertain_fallback = c

    return (None, uncertain_fallback)


def _build_receipt_result(
    tx: Transaction,
    c: dict,
    uncertain: bool,
) -> MatchResult:
    """Build a MatchResult for a receipt candidate (pure, no side effects)."""
    gap = _compute_date_gap(tx.date, c["receipt_date"])
    status = _assign_delay_status(gap)
    notes: list[str] = []

    if uncertain:
        notes.append("Uncertain name match — please verify")

    if c.get("manually_checked") is None and c.get("confidence") != "high":
        status = MATCHED_UNREVIEWED
        notes.append("Receipt was flagged by belegbot and may not have been manually reviewed")

    if gap > config.DATE_TIER1_DAYS:
        notes.append(f"Date gap: {gap} days between receipt date and bank booking")

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
) -> tuple[Optional[MatchResult], Optional[dict]]:
    """
    Attempt to match a transaction against the regpayment table by exact
    signed-cent amount within the valid date range.

    Returns a tuple (definitive_result, uncertain_candidate) following the
    same contract as _try_match_receipt: a definitive "match" is built and
    committed immediately; an "uncertain" candidate is returned uncommitted
    so the caller can prefer it only after both sources have failed to
    produce a definitive match.
    """
    candidates = db_client.get_regpayment_candidates(signed_cents, tx.date)
    candidates = [c for c in candidates if c["id"] not in used_regpayment_ids]

    if not candidates:
        return (None, None)

    uncertain_fallback: Optional[dict] = None

    for c in candidates:
        reason = c["reason"] or ""
        similarity = _check_name_similarity(tx.description, reason)

        if similarity == "match":
            used_regpayment_ids.add(c["id"])
            return (_build_regpayment_result(tx, c, uncertain=False), None)

        if similarity == "uncertain" and uncertain_fallback is None:
            uncertain_fallback = c

    return (None, uncertain_fallback)


def _build_regpayment_result(
    tx: Transaction,
    c: dict,
    uncertain: bool,
    amount_mismatch: bool = False,
) -> MatchResult:
    """Build a MatchResult for a regpayment candidate (pure, no side effects)."""
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

    H5: AMOUNT_MISMATCH is a *diagnostic about the regpayment table*, not a
    claim on a row. The 1-to-1 constraint enforced by `used_regpayment_ids`
    does NOT apply here — a row already claimed definitively by an earlier
    transaction must still surface a stale-amount warning for a later one.
    `used_regpayment_ids` is accepted for signature stability but
    intentionally ignored; no rows are added to it either.
    """
    candidates = db_client.get_regpayment_candidates_by_date(tx.date)

    if not candidates:
        return None

    uncertain_fallback: Optional[dict] = None

    for c in candidates:
        reason = c["reason"] or ""
        similarity = _check_name_similarity(tx.description, reason)

        if similarity == "match":
            return _build_regpayment_result(
                tx, c,
                uncertain=False, amount_mismatch=True,
            )

        if similarity == "uncertain" and uncertain_fallback is None:
            uncertain_fallback = c

    if uncertain_fallback is not None:
        return _build_regpayment_result(
            tx, uncertain_fallback,
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
    i = 1

    for tx in sorted(transactions, key=lambda t: t.date):
        logger.info("Matching transaction %d/%d...", i, len(transactions))
        signed_cents = _to_signed_cents(tx.amount, tx.direction)

        # Per TECHNICAL_SPEC §7.2: definitive ("match") candidates from either
        # source beat any "uncertain" candidate. Receipts have priority within
        # each tier (definitive and uncertain) because card/cash purchases are
        # more likely to have a receipt than to be a regular payment.
        receipt_def, receipt_unc = _try_match_receipt(tx, used_receipt_ids)
        if receipt_def is not None:
            result = receipt_def
        else:
            regpay_def, regpay_unc = _try_match_regpayment(
                tx, signed_cents, used_regpayment_ids,
            )
            if regpay_def is not None:
                result = regpay_def
            elif receipt_unc is not None:
                used_receipt_ids.add(receipt_unc["id"])
                result = _build_receipt_result(
                    tx, receipt_unc, uncertain=True,
                )
            elif regpay_unc is not None:
                used_regpayment_ids.add(regpay_unc["id"])
                result = _build_regpayment_result(
                    tx, regpay_unc, uncertain=True,
                )
            else:
                result = (
                    _try_regpayment_amount_mismatch(tx, used_regpayment_ids)
                    or MatchResult(transaction=tx, status=NO_MATCH)
                )

        logger.info("Matching Result for transaction %d/%d: %s", i, len(transactions), result.status)
        i=i+1
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
