"""
pipeline/matcher.py — transaction matching engine for kontocheck

Responsibilities:
  - Match each bank statement transaction against receipts and regpayment DB rows
  - Enforce 1-to-1 postcondition: each DB row matched to at most one transaction
  - Use LLM for name similarity (handles "XYZ Systemgastronomie GmbH" → "McDonald's")
  - Assign a status verdict to each transaction
  - Return one MatchResult per transaction

Redesigned two-pass algorithm (see REDESIGN_PLAN.md):
  Pass A — per-transaction candidate gathering + single LLM choice
  Pass B — global reconciliation (1-to-1 postcondition + conflict flagging)

No DB row is claimed during iteration. The 1-to-1 constraint is enforced
as a postcondition in Pass B: when two transactions both claim the same
row with high certainty, neither wins — both are flagged UNCERTAIN with
conflict=True so a human can review.

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

MATCH     = "match"
UNCERTAIN = "uncertain"
NO_MATCH  = "no_match"

# Display strings for use in report.py
STATUS_DISPLAY = {
    MATCH:     "✅ Match",
    UNCERTAIN: "⚠️ Uncertain — please verify",
    NO_MATCH:  "❌ No match",
}

# Module-level Ollama client — created once, reused for all similarity calls
_client = ollama.Client(host=config.OLLAMA_URL)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class CandidateInfo:
    """One DB candidate considered for a transaction (display + audit)."""
    source:        str                          # "receipt" | "regpayment"
    id:            int
    name:          str                          # issuer or reason
    file_name:     Optional[str]      = None
    amount_match:  bool              = True     # False = name-only fallback candidate
    date_gap_days: Optional[int]     = None     # display only, no status impact
    note:          Optional[str]     = None     # e.g. "amount differs — update regpayment table"


@dataclass
class MatchResult:
    """
    The outcome of matching one bank statement transaction.
    One MatchResult is produced per transaction regardless of outcome.
    """
    transaction:    Transaction
    status:         str
    matched_source: Optional[str]      = None   # "receipt" | "regpayment" | None ; set only when status == MATCH
    matched_id:     Optional[int]     = None
    matched_name:   Optional[str]     = None
    matched_file:   Optional[str]     = None
    candidates:     list[CandidateInfo] = field(default_factory=list)
    conflict:       bool              = False
    conflict_with:  list[str]         = field(default_factory=list)  # other claiming tx descriptions
    notes:          list[str]         = field(default_factory=list)


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


# ── LLM helpers ───────────────────────────────────────────────────────────────

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


def _parse_choice_verdict(text: str) -> tuple[str, list[int]]:
    """
    Parse the new candidate-choice LLM reply format.

    Expected formats:
      match: <n>
      uncertain: <n,m,...>
      no_match

    Returns (verdict, indices) where verdict is "match" | "uncertain" |
    "no_match" and indices is the list of selected candidate numbers
    (1-based as emitted by the LLM). On no_match or unparseable output,
    indices is empty.

    The "match:" / "uncertain:" prefix is matched case-insensitively and
    tolerates surrounding whitespace or markdown. Numbers may be separated
    by commas or spaces. When no recognisable prefix is found, falls back
    to _parse_verdict so a bare "match"/"uncertain"/"no_match" token still
    classifies the verdict (with empty indices).
    """
    cleaned = _strip_thinking(text).strip().lower()

    # match: <nums>
    m = re.match(r"^[*>#\-\s]*match\b[:\s]+([\d,\s]+)", cleaned)
    if m:
        nums = _extract_numbers(m.group(1))
        if nums:
            return ("match", nums)

    # uncertain: <nums>
    m = re.match(r"^[*>#\-\s]*uncertain\b[:\s]+([\d,\s]+)", cleaned)
    if m:
        nums = _extract_numbers(m.group(1))
        if nums:
            return ("uncertain", nums)

    # bare verdict with no indices — fall back to _parse_verdict
    verdict = _parse_verdict(cleaned)
    return (verdict, [])


def _extract_numbers(s: str) -> list[int]:
    """Pull all positive integers out of a comma/space-separated list."""
    return [int(n) for n in re.findall(r"\d+", s)]


# Legal-entity suffixes and generic stopwords stripped before brand-token
# overlap is computed. Without this, "GmbH" or "Deutschland" alone would
# trigger spurious matches across totally unrelated companies.
_BRAND_NOISE_TOKENS = frozenset({
    "gmbh", "ag", "kg", "ohg", "ug", "kgaa", "se", "ek", "ev",
    "co", "company", "ltd", "llc", "inc", "corp",
    "deutschland", "germany", "international",
    "und", "and", "der", "die", "das", "the", "von",
    "kartenzahlung", "lastschrift", "basislastschrift", "sepa",
    "ueberweisung", "gutschrift", "dauerauftrag", "paypal",
    "danke", "sagt", "zahlung", "betrag", "auftrag",
    "bank", "konto", "card", "pos", "terminal",
    "dee", "deu", "mit", "fuer", "bei", "auf",
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


# ── Candidate-choice prompt + LLM call ────────────────────────────────────────

def _format_candidate_amount(c: dict) -> str:
    """Render a candidate's amount for the LLM prompt."""
    if "total_amount" in c:
        amt = c.get("total_amount")
        return f"€{amt:.2f}" if amt is not None else "?"
    if "amount" in c:
        # regpayment stores signed cents
        cents = c.get("amount")
        if cents is None:
            return "?"
        return f"€{abs(cents) / 100:.2f}"
    return "?"


def _candidate_name(c: dict) -> str:
    """Return the display name of a candidate (issuer for receipts, reason for regpayments)."""
    return (c.get("issuer") or c.get("reason") or "").strip()


def _build_candidate_choice_prompt(
    tx: Transaction,
    candidates: list[dict],
    source_label: str,
    conservative: bool = False,
) -> str:
    """
    Build a single LLM prompt presenting the bank description and a numbered
    list of candidates. The LLM picks one (match: <n>), several plausible
    ones (uncertain: <n,m,...>), or none (no_match).

    `source_label` is "receipt" or "regpayment" — used in the prompt so the
    LLM knows which table the candidates come from. When candidates from
    both tables are combined (name-only fallback), callers should pass a
    label like "receipt/regpayment" and rely on the per-candidate lines to
    disambiguate.
    """
    lines = [
        f'Bank statement description: "{tx.description}"',
        "",
        f"Candidates ({source_label}):",
    ]
    for i, c in enumerate(candidates, start=1):
        name = _candidate_name(c)
        amt = _format_candidate_amount(c)
        lines.append(f"  {i}. {name} — {amt}")
    lines.append("")
    lines.append(
        "Decide which candidate (if any) refers to the same merchant/payee "
        "as the bank statement description. German bank descriptions are "
        "mangled (e.g. \"Kartenzahlung OBI.SAGT.DANKE/Hamburg/DE\" for "
        "\"OBI GmbH & Co. Deutschland KG\"). Ignore legal suffixes (GmbH, "
        "AG, KG), cities, terminal IDs, and payment prefixes."
    )
    lines.append("")
    lines.append("Rules:")
    lines.append("- match: <n> — candidate n is the definitive match.")
    lines.append("- uncertain: <n,m,...> — these candidates are plausible, none is certain.")
    lines.append("- no_match — none of the candidates fit.")
    if conservative:
        lines.append("")
        lines.append(
            "Be conservative: only return match or uncertain if the "
            "probability is high; otherwise return no_match."
        )
    lines.append("")
    lines.append(
        "Answer with exactly one line in the format above and nothing else."
    )
    return "\n".join(lines)


def _choose_candidate(
    tx: Transaction,
    candidates: list[dict],
    source_label: str,
    conservative: bool = False,
) -> tuple[str, list[int]]:
    """
    Single LLM call per transaction. Returns (verdict, selected_indices)
    where verdict is "match" | "uncertain" | "no_match" and
    selected_indices are 1-based candidate numbers chosen by the LLM.

    Defensive fallbacks:
      - LLM exception → ("uncertain", [all indices]) — keep candidates.
      - Unparseable output → "no_match", then apply brand-overlap safety
        net: if _has_brand_overlap detects a shared brand token for any
        candidate, upgrade to ("uncertain", [those candidate indices]).
    """
    all_indices = list(range(1, len(candidates) + 1))
    prompt = _build_candidate_choice_prompt(tx, candidates, source_label, conservative)
    try:
        response = _client.chat(
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.0, "num_predict": 8000, "num_ctx": 32768},
            think=False,
        )
        content = _strip_thinking(response["message"]["content"] or "")
        verdict, indices = _parse_choice_verdict(content)

        if verdict in ("match", "uncertain") and indices:
            return (verdict, indices)
        if verdict == "uncertain":
            # uncertain with no explicit indices → keep all
            return ("uncertain", all_indices)

        # no_match (or unparseable → no_match). Apply brand-overlap safety
        # net so a clear shared token still surfaces as a suggestion.
        brand_hits = [
            i for i, c in enumerate(candidates, start=1)
            if _has_brand_overlap(tx.description, _candidate_name(c))
        ]
        if brand_hits:
            return ("uncertain", brand_hits)
        return ("no_match", [])
    except Exception as exc:
        logger.warning("Candidate-choice LLM call failed: %s — treating as uncertain", exc)
        return ("uncertain", all_indices)


# ── Candidate gathering helpers ──────────────────────────────────────────────

def _gather_amount_match_candidates(tx: Transaction, signed_cents: int) -> list[dict]:
    """
    Gather candidates from both tables whose amount matches exactly and
    whose date fits the existing constraints. Receipts are queried only
    for debit transactions (H1: refunds/credits do not have purchase
    receipts). Regpayments are queried for both directions.

    Applies the L12 (drop empty issuer/reason) and H4 (date window)
    defense-in-depth filters. Each candidate dict is tagged with a
    "__source" key ("receipt" or "regpayment") so downstream code can
    build CandidateInfo without re-querying.
    """
    candidates: list[dict] = []

    if tx.direction == "debit":
        receipts = db_client.get_receipt_candidates_by_date(
            tx.date, config.RECEIPT_DATE_WINDOW_DAYS, amount=tx.amount
        )
        # L12: drop empty/whitespace-only issuers
        receipts = [c for c in receipts if (c.get("issuer") or "").strip()]
        # H4: drop receipts older than the configured window
        window_days = getattr(config, "RECEIPT_DATE_WINDOW_DAYS", None)
        if window_days:
            lower_bound = tx.date - timedelta(days=window_days)
            receipts = [
                c for c in receipts
                if c.get("receipt_date") is None or c["receipt_date"] >= lower_bound
            ]
        for r in receipts:
            r["__source"] = "receipt"
            candidates.append(r)

    regpays = db_client.get_regpayment_candidates_by_date(tx.date, signed_cents)
    # L12: drop empty/whitespace-only reasons
    regpays = [c for c in regpays if (c.get("reason") or "").strip()]
    for rp in regpays:
        rp["__source"] = "regpayment"
        candidates.append(rp)

    return candidates


def _gather_name_only_candidates(tx: Transaction) -> list[dict]:
    """
    Name-only fallback: gather broader candidates by date window only
    (no amount filter). Receipts via get_receipt_candidates_by_date (within
    the configured window), regpayments via get_regpayment_candidates_by_date.

    The candidate pool is already bounded by RECEIPT_DATE_WINDOW_DAYS.
    Each candidate dict is tagged with "__source" and "__amount_match"
    (False when the candidate's amount differs from the tx amount, True
    when it happens to match).
    """
    candidates: list[dict] = []
    window_days = getattr(config, "RECEIPT_DATE_WINDOW_DAYS", 28)

    if tx.direction == "debit":
        receipts = db_client.get_receipt_candidates_by_date(tx.date, window_days)
        receipts = [c for c in receipts if (c.get("issuer") or "").strip()]
        for r in receipts:
            r["__source"] = "receipt"
            r["__amount_match"] = (
                r.get("total_amount") is not None
                and Decimal(r["total_amount"]) == tx.amount
            )
            candidates.append(r)

    regpays = db_client.get_regpayment_candidates_by_date(tx.date)
    regpays = [c for c in regpays if (c.get("reason") or "").strip()]
    signed_cents = _to_signed_cents(tx.amount, tx.direction)
    for rp in regpays:
        rp["__source"] = "regpayment"
        rp["__amount_match"] = (rp.get("amount") == signed_cents)
        candidates.append(rp)

    return candidates


# ── Result builders ───────────────────────────────────────────────────────────

def _build_candidate_info(c: dict, tx: Transaction, amount_match: bool) -> CandidateInfo:
    """Build a CandidateInfo from a candidate dict."""
    source = c["__source"]
    gap: Optional[int] = None
    note: Optional[str] = None

    if source == "receipt" and c.get("receipt_date") is not None:
        gap = (tx.date - c["receipt_date"]).days
    elif source == "regpayment":
        # regpayment has no single receipt date; use startDate if present
        if c.get("startDate") is not None:
            try:
                gap = (tx.date - c["startDate"]).days
            except Exception:
                gap = None

    if not amount_match and source == "regpayment":
        note = "regpayment amount differs — update table if correct"

    return CandidateInfo(
        source=source,
        id=c["id"],
        name=_candidate_name(c),
        file_name=c.get("file_name"),
        amount_match=amount_match,
        date_gap_days=gap,
        note=note,
    )


def _build_match_result(
    tx: Transaction,
    chosen: dict,
    ruled_out: list[dict],
    amount_match: bool,
) -> MatchResult:
    """
    Build a provisional MATCH result. `chosen` is the candidate the LLM
    picked; `ruled_out` are the other amount-matching candidates the LLM
    explicitly did not pick (kept in candidates[] for transparency).
    """
    source = chosen["__source"]
    notes: list[str] = []

    # belegbot-unreviewed note for receipts
    if source == "receipt":
        if chosen.get("manually_checked") is None and chosen.get("confidence") != "high":
            notes.append("Receipt flagged by belegbot — please verify")

    if not amount_match and source == "regpayment":
        expected_euros = abs(chosen["amount"]) / 100
        notes.append(
            f"regpayment amount differs — update table if correct "
            f"(expected €{expected_euros:.2f}, actual €{tx.amount:.2f})"
        )

    candidates = [_build_candidate_info(c, tx, amount_match=True) for c in ruled_out]

    return MatchResult(
        transaction=tx,
        status=MATCH,
        matched_source=source,
        matched_id=chosen["id"],
        matched_name=_candidate_name(chosen),
        matched_file=chosen.get("file_name"),
        candidates=candidates,
        notes=notes,
    )


def _build_uncertain_result(
    tx: Transaction,
    candidates: list[dict],
    amount_match: bool,
    selected_indices: list[int],
) -> MatchResult:
    """Build an UNCERTAIN result listing the selected candidates."""
    cand_infos: list[CandidateInfo] = []
    for i, c in enumerate(candidates, start=1):
        if i in selected_indices:
            amt_match = amount_match if amount_match else c.get("__amount_match", True)
            cand_infos.append(_build_candidate_info(c, tx, amt_match))
    return MatchResult(
        transaction=tx,
        status=UNCERTAIN,
        candidates=cand_infos,
    )


# ── Public interface ──────────────────────────────────────────────────────────

def match_all(transactions: list[Transaction]) -> list[MatchResult]:
    """
    Match all transactions and return one MatchResult per transaction.

    Two-pass algorithm (see REDESIGN_PLAN.md §3):
      Pass A — per-transaction candidate gathering + single LLM choice.
               Build provisional MatchResult objects. No DB row is claimed
               yet.
      Pass B — global reconciliation. Build a claims map from provisional
               MATCH results. Single-claimant rows keep MATCH. Multi-claimant
               conflicts downgrade all claimants to UNCERTAIN with
               conflict=True. Return results sorted by transaction date.
    """
    results: list[MatchResult] = []
    i = 1

    # ── Pass A ────────────────────────────────────────────────────────────
    for tx in sorted(transactions, key=lambda t: t.date):
        logger.info("Matching transaction %d/%d...", i, len(transactions))
        signed_cents = _to_signed_cents(tx.amount, tx.direction)

        # Step 1 — amount-matching candidates
        amount_candidates = _gather_amount_match_candidates(tx, signed_cents)

        if amount_candidates:
            # Step 2 — single LLM call over the candidate set
            source_label = (
                "receipt/regpayment" if {c["__source"] for c in amount_candidates} == {"receipt", "regpayment"}
                else amount_candidates[0]["__source"]
            )
            verdict, indices = _choose_candidate(tx, amount_candidates, source_label)

            if verdict == "match" and indices:
                chosen_idx = indices[0] - 1  # 1-based → 0-based
                chosen = amount_candidates[chosen_idx]
                ruled_out = [c for j, c in enumerate(amount_candidates) if j != chosen_idx]
                result = _build_match_result(tx, chosen, ruled_out, amount_match=True)
            elif verdict == "uncertain" and indices:
                result = _build_uncertain_result(
                    tx, amount_candidates, amount_match=True,
                    selected_indices=indices,
                )
            else:
                # no_match on amount-matching candidates → fall through to
                # the name-only fallback (Step 3).
                result = _match_name_only_fallback(tx)
        else:
            # Step 3 — no amount-matching candidates → name-only fallback
            result = _match_name_only_fallback(tx)

        logger.info(
            "Matching Result for transaction %d/%d: %s", i, len(transactions), result.status
        )
        i += 1
        results.append(result)
        logger.debug(
            "%-40s  %s  →  %s",
            tx.description[:40],
            f"€{tx.amount:.2f} ({tx.direction})",
            STATUS_DISPLAY.get(result.status, result.status),
        )

    # ── Pass B — global reconciliation ────────────────────────────────────
    _reconcile_conflicts(results)

    # ── Final logging + return ────────────────────────────────────────────
    n_match     = sum(1 for r in results if r.status == MATCH)
    n_uncertain = sum(1 for r in results if r.status == UNCERTAIN)
    n_unmatched = sum(1 for r in results if r.status == NO_MATCH)
    logger.info(
        "Matching complete: %d transactions — %d matched, %d uncertain, %d unmatched",
        len(results), n_match, n_uncertain, n_unmatched,
    )
    return sorted(results, key=lambda r: r.transaction.date)


def _match_name_only_fallback(tx: Transaction) -> MatchResult:
    """
    Pass A Step 3: no amount-matching candidates (or the LLM ruled them all
    out). Gather broader candidates by date window only and send them to
    the LLM with the conservative prompt variant.

    - match: <n> → provisional MATCH with amount_match=False on the
      candidate (subsumes the old AMOUNT_MISMATCH diagnostic for
      regpayments whose amount differs).
    - uncertain: <n,m,...> → UNCERTAIN with those candidates.
    - no_match → NO_MATCH.
    """
    candidates = _gather_name_only_candidates(tx)
    if not candidates:
        return MatchResult(transaction=tx, status=NO_MATCH)

    source_label = (
        "receipt/regpayment" if {c["__source"] for c in candidates} == {"receipt", "regpayment"}
        else candidates[0]["__source"]
    )
    verdict, indices = _choose_candidate(
        tx, candidates, source_label, conservative=True
    )

    if verdict == "match" and indices:
        chosen_idx = indices[0] - 1
        chosen = candidates[chosen_idx]
        amt_match = chosen.get("__amount_match", False)
        ruled_out = [c for j, c in enumerate(candidates) if j != chosen_idx]
        return _build_match_result(tx, chosen, ruled_out, amount_match=amt_match)
    if verdict == "uncertain" and indices:
        return _build_uncertain_result(
            tx, candidates, amount_match=False, selected_indices=indices,
        )
    return MatchResult(transaction=tx, status=NO_MATCH)


def _reconcile_conflicts(results: list[MatchResult]) -> None:
    """
    Pass B: enforce the 1-to-1 postcondition and flag conflicts.

    Mutates `results` in place. For every provisional MATCH result, record
    a claim on (matched_source, matched_id). When ≥2 transactions claim the
    same row, downgrade ALL claimants to UNCERTAIN with conflict=True,
    clear their matched_* fields, move the contested candidate into
    candidates[], and record each other claimant's description + date in
    conflict_with.
    """
    # Step 1 — build claims map
    claims: dict[tuple[str, int], list[int]] = {}
    for idx, r in enumerate(results):
        if r.status == MATCH and r.matched_source is not None and r.matched_id is not None:
            key = (r.matched_source, r.matched_id)
            claims.setdefault(key, []).append(idx)

    # Step 2 — single-claimant rows keep MATCH (nothing to do).
    # Step 3 — multi-claimant conflicts: downgrade all claimants.
    for (source, db_id), claimant_indices in claims.items():
        if len(claimant_indices) < 2:
            continue

        # Find the contested candidate info from one of the claimants
        # (they all reference the same row). We need to reconstruct a
        # CandidateInfo from the matched_* fields.
        first = results[claimant_indices[0]]
        contested = CandidateInfo(
            source=source,
            id=db_id,
            name=first.matched_name or "",
            file_name=first.matched_file,
            amount_match=True,
            date_gap_days=None,
            note="Contested — also claimed by another transaction",
        )

        for idx in claimant_indices:
            r = results[idx]
            other_descs = [
                f"{results[other].transaction.date.isoformat()} "
                f"{results[other].transaction.description}"
                for other in claimant_indices if other != idx
            ]
            # Clear matched_* fields
            r.matched_source = None
            r.matched_id = None
            r.matched_name = None
            r.matched_file = None
            # Move contested candidate into candidates[] (de-duplicated by id)
            if not any(c.id == db_id and c.source == source for c in r.candidates):
                r.candidates.append(contested)
            r.conflict = True
            r.conflict_with.extend(other_descs)
            r.status = UNCERTAIN
