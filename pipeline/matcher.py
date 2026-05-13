import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

import ollama

import config
from pipeline.extractor import Transaction
from storage import db_client

logger = logging.getLogger(__name__)

MATCHED = "matched"
MATCHED_LARGE_DELAY = "matched_large_delay"
MATCHED_UNUSUAL_DELAY = "matched_unusual_delay"
MATCHED_UNREVIEWED = "matched_unreviewed"
AMOUNT_MISMATCH = "amount_mismatch"
NO_MATCH = "no_match"

_STATUS_DISPLAY = {
    MATCHED: "✅ Matched",
    MATCHED_LARGE_DELAY: "⚠️ Matched — large delay",
    MATCHED_UNUSUAL_DELAY: "⚠️ Matched — unusual delay",
    MATCHED_UNREVIEWED: "⚠️ Matched — please verify",
    AMOUNT_MISMATCH: "⚠️ Amount mismatch",
    NO_MATCH: "❌ No match found",
}

_client = ollama.Client(host=config.OLLAMA_URL)


@dataclass
class MatchResult:
    transaction: Transaction
    status: str
    matched_source: str | None = None
    matched_id: int | None = None
    matched_name: str | None = None
    matched_file: str | None = None
    date_gap_days: int | None = None
    notes: list[str] = field(default_factory=list)


def _to_signed_cents(amount: Decimal, direction: str) -> int:
    cents = int(amount * 100)
    return -cents if direction == "debit" else cents


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _check_name_similarity(bank_description: str, candidate_name: str) -> str:
    prompt = (
        f'Could the bank statement description "{bank_description}" '
        f'refer to the same entity as "{candidate_name}"?\n\n'
        'Reply with exactly one word: "match", "no_match", or "uncertain".'
    )
    try:
        response = _client.chat(
            model=config.OLLAMA_MODEL,
            messages=[
                {"role": "user", "content": prompt},
            ],
            options={
                "temperature": 0.0,
                "num_predict": 50,
            },
            think=False,
        )
        content = _strip_thinking(response["message"]["content"] or "")
        content = content.strip().lower()
        if "match" in content and "no_match" not in content and "uncertain" not in content:
            return "match"
        if "uncertain" in content:
            return "uncertain"
        return "no_match"
    except Exception as e:
        logger.warning("Name similarity LLM call failed: %s — treating as uncertain", e)
        return "uncertain"


def _compute_date_gap(bank_date, receipt_date) -> int:
    return (bank_date - receipt_date).days


def _assign_status(date_gap_days: int) -> str:
    if date_gap_days <= config.DATE_TIER1_DAYS:
        return MATCHED
    if date_gap_days <= config.DATE_TIER2_DAYS:
        return MATCHED_LARGE_DELAY
    return MATCHED_UNUSUAL_DELAY


def _try_match_receipt(tx: Transaction, used_receipt_ids: set[int]) -> MatchResult | None:
    candidates = db_client.get_receipt_candidates(tx.amount, tx.date)
    candidates = [c for c in candidates if c["id"] not in used_receipt_ids]

    uncertain_fallback = None

    for c in candidates:
        issuer = c["issuer"] or ""
        similarity = _check_name_similarity(tx.description, issuer)

        if similarity == "match":
            gap = _compute_date_gap(tx.date, c["receipt_date"])
            status = _assign_status(gap)
            notes = []

            if c.get("manually_checked") is None and c.get("confidence") != "high":
                status = MATCHED_UNREVIEWED
                notes.append("Receipt not manually reviewed (belegbot flagged)")

            if gap > config.DATE_TIER1_DAYS:
                notes.append(f"Date gap: {gap} days between receipt and bank entry")

            used_receipt_ids.add(c["id"])
            return MatchResult(
                transaction=tx,
                status=status,
                matched_source="receipt",
                matched_id=c["id"],
                matched_name=issuer,
                matched_file=c.get("file_name"),
                date_gap_days=gap,
                notes=notes,
            )

        if similarity == "uncertain":
            if uncertain_fallback is None:
                gap = _compute_date_gap(tx.date, c["receipt_date"])
                uncertain_fallback = (c, gap)

    if uncertain_fallback is not None:
        c, gap = uncertain_fallback
        status = _assign_status(gap)
        notes = ["Uncertain name match — please verify"]

        if c.get("manually_checked") is None and c.get("confidence") != "high":
            status = MATCHED_UNREVIEWED
            notes.append("Receipt not manually reviewed (belegbot flagged)")

        if gap > config.DATE_TIER1_DAYS:
            notes.append(f"Date gap: {gap} days between receipt and bank entry")

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

    return None


def _try_match_regpayment(tx: Transaction, signed_cents: int, used_regpayment_ids: set[int]) -> MatchResult | None:
    candidates = db_client.get_regpayment_candidates(signed_cents, tx.date)
    candidates = [c for c in candidates if c["id"] not in used_regpayment_ids]

    uncertain_fallback = None

    for c in candidates:
        reason = c["reason"] or ""
        similarity = _check_name_similarity(tx.description, reason)

        if similarity == "match":
            used_regpayment_ids.add(c["id"])
            return MatchResult(
                transaction=tx,
                status=MATCHED,
                matched_source="regpayment",
                matched_id=c["id"],
                matched_name=reason,
                matched_file=None,
                date_gap_days=None,
                notes=[],
            )

        if similarity == "uncertain":
            if uncertain_fallback is None:
                uncertain_fallback = c

    if uncertain_fallback is not None:
        c = uncertain_fallback
        used_regpayment_ids.add(c["id"])
        return MatchResult(
            transaction=tx,
            status=MATCHED,
            matched_source="regpayment",
            matched_id=c["id"],
            matched_name=c["reason"] or "",
            matched_file=None,
            date_gap_days=None,
            notes=["Uncertain name match — please verify"],
        )

    return None


def _try_regpayment_amount_mismatch(tx: Transaction, used_regpayment_ids: set[int]) -> MatchResult | None:
    candidates = db_client.get_regpayment_candidates(
        _to_signed_cents(tx.amount, tx.direction),
        tx.date,
    )
    candidates = [c for c in candidates if c["id"] not in used_regpayment_ids]
    if candidates:
        return None

    signed_cents = _to_signed_cents(tx.amount, tx.direction)
    opposite_cents = -signed_cents

    candidates = db_client.get_regpayment_candidates(opposite_cents, tx.date)
    candidates = [c for c in candidates if c["id"] not in used_regpayment_ids]

    uncertain_fallback = None

    for c in candidates:
        reason = c["reason"] or ""
        similarity = _check_name_similarity(tx.description, reason)

        if similarity == "match":
            used_regpayment_ids.add(c["id"])
            return MatchResult(
                transaction=tx,
                status=AMOUNT_MISMATCH,
                matched_source="regpayment",
                matched_id=c["id"],
                matched_name=reason,
                matched_file=None,
                date_gap_days=None,
                notes=[f"Expected amount: {c['amount'] / 100:.2f}€, actual: {tx.amount}€"],
            )

        if similarity == "uncertain":
            if uncertain_fallback is None:
                uncertain_fallback = c

    if uncertain_fallback is not None:
        c = uncertain_fallback
        used_regpayment_ids.add(c["id"])
        return MatchResult(
            transaction=tx,
            status=AMOUNT_MISMATCH,
            matched_source="regpayment",
            matched_id=c["id"],
            matched_name=c["reason"] or "",
            matched_file=None,
            date_gap_days=None,
            notes=[
                "Uncertain name match — please verify",
                f"Expected amount: {c['amount'] / 100:.2f}€, actual: {tx.amount}€",
            ],
        )

    return None


def match_all(transactions: list[Transaction]) -> list[MatchResult]:
    used_receipt_ids: set[int] = set()
    used_regpayment_ids: set[int] = set()

    sorted_tx = sorted(transactions, key=lambda t: t.date)
    results: list[MatchResult] = []

    for tx in sorted_tx:
        signed_cents = _to_signed_cents(tx.amount, tx.direction)

        receipt_result = _try_match_receipt(tx, used_receipt_ids)
        if receipt_result is not None:
            results.append(receipt_result)
            continue

        regpayment_result = _try_match_regpayment(tx, signed_cents, used_regpayment_ids)
        if regpayment_result is not None:
            results.append(regpayment_result)
            continue

        mismatch_result = _try_regpayment_amount_mismatch(tx, used_regpayment_ids)
        if mismatch_result is not None:
            results.append(mismatch_result)
            continue

        results.append(MatchResult(transaction=tx, status=NO_MATCH))

    logger.info(
        "Matching complete: %d transactions — %d matched, %d unmatched",
        len(results),
        sum(1 for r in results if r.status != NO_MATCH),
        sum(1 for r in results if r.status == NO_MATCH),
    )
    return results
