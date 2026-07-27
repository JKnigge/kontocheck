# Matcher Redesign Plan

> **Status:** Draft — awaiting explicit go-ahead before implementation.
> **Scope:** `pipeline/matcher.py`, `storage/db_client.py`, `reporting/report.py`,
> and the test suite. No changes to `config.py` semantics, `pipeline/extractor.py`,
> `kontocheck.py`, or the DB schema (kontocheck stays read-only).

## 1. Problem statement

The current matcher (`pipeline/matcher.py:match_all`) processes transactions
in chronological order and **commits** a DB row id to `used_receipt_ids` /
`used_regpayment_ids` **immediately** when a definitive `"match"` is found.
This greedy, first-come-first-served assignment causes a structural defect:

> Transaction #1 (`Mueller Systemgastronomie GmbH`, €13.37) sees receipt
> `Gastro Meier` (€13.37, date fits), the LLM says `"match"`, and the id is
> committed. Transaction #2 (`McDonalds Meier`, €13.37, same date) — a
> *better* name fit — never sees that receipt again because it is already in
> `used_receipt_ids`.

The 1-to-1 constraint (PRD §6.3: "each receipt or regpayment row can be
matched to at most one bank entry per run") is intended as a **postcondition**
on the final result set, not a mandate to assign greedily in iteration order.
The greediness is an implementation choice that the current `match_all`
happens to lock in — and that two known H3 xfail defects
(`test_two_matches_smallest_gap_wins`, `test_order_dependence_diagonal_pairing`)
explicitly flag as wrong.

## 2. Design goals

1. **Defer commitment.** No DB row is claimed until *all* transactions have
   been evaluated. This lets a later, better-fitting transaction win a row
   that an earlier, weaker match would otherwise have consumed.

2. **One LLM call per transaction.** Instead of N pairwise
   `_check_name_similarity` calls per transaction (one per candidate), send
   the full candidate set to the LLM in a single call and let it pick. This
   is both cheaper and more accurate: the LLM can directly compare
   `Gastro Meier` vs `McDonalds Meier` against `Mueller Systemgastronomie`
   rather than judging each in isolation.

3. **Three statuses only.** Collapse the existing six statuses
   (`MATCHED`, `MATCHED_LARGE_DELAY`, `MATCHED_UNUSUAL_DELAY`,
   `MATCHED_UNREVIEWED`, `AMOUNT_MISMATCH`, `NO_MATCH`) to three:
   `MATCH`, `UNCERTAIN`, `NO_MATCH`. The date-gap tiers are dropped because
   the SQL `RECEIPT_DATE_WINDOW_DAYS` bound already filters stale receipts —
   anything outside the window never becomes a candidate.

4. **Surface conflicts, never silently resolve.** When two transactions both
   claim the same DB row with high certainty (the McDonalds/Mueller
   scenario), the system does **not** pick a winner. It flags **both** as
   `UNCERTAIN` and lists the contested candidate plus the other claiming
   transaction, so a human can review.

5. **Preserve the 1-to-1 postcondition.** Each DB row ends up claimed by at
   most one transaction in the final result set — but the enforcement moves
   from "commit during iteration" to a final reconciliation pass.

## 3. Matching logic (new algorithm)

The new `match_all` is a two-pass algorithm:

```
Pass A — per-transaction candidate gathering + LLM choice
Pass B — global reconciliation (1-to-1 postcondition + conflict flagging)
```

### Pass A — per transaction (sorted by date for deterministic logging)

For each transaction `tx`:

**Step 1 — Gather amount-matching candidates.**

Query both tables for candidates whose amount matches exactly and whose date
fits the existing constraints (receipts: `receipt_date <= bank_date` and
within `RECEIPT_DATE_WINDOW_DAYS`; regpayments: `startDate <= bank_date` and
`endDate IS NULL OR >= bank_date`, scoped by `REGPAYMENT_USER_ID`). Apply
the existing L12 (drop empty issuer/reason) and H4 (date window)
defense-in-depth filters.

**Step 2 — If ≥1 amount-matching candidate: send them to the LLM.**

A single LLM call presents the bank description and a numbered list of
candidates (name, source, amount, whether the amount matches). The LLM
returns exactly one of:

- `match: <n>` — candidate `n` is the definitive match.
- `uncertain: <n,m,...>` — these candidates are plausible, none is certain.
- `no_match` — none of the candidates fit.

Build the provisional result:

- `match: <n>` → provisional `MATCH`. The matched candidate goes into
  `matched_*` fields. The **other** amount-matching candidates (the ones the
  LLM ruled out) go into `candidates[]` so the report can show "also
  considered: ...". **The id is NOT committed yet** — Pass B may downgrade it.
- `uncertain: <n,m,...>` → `UNCERTAIN` with those candidates in
  `candidates[]`. No commit.
- `no_match` → fall through to step 3.

**Step 3 — If 0 amount-matching candidates: name-only fallback search.**

Gather broader candidates by date window only (no amount filter):

- receipts via a new `get_receipt_candidates_by_date(bank_date, window_days)`
  query (within the configured window).
- regpayments via the existing `get_regpayment_candidates_by_date(bank_date)`.

Cap the combined candidate pool at N (e.g. 20) by date proximity. Send to
the LLM with a **conservative** prompt variant: "only return `match` or
`uncertain` if probability is high; otherwise `no_match`."

- `match: <n>` → provisional `MATCH` with `amount_match=False` on the
  candidate (and a note if it's a regpayment with a different amount — this
  subsumes the old `AMOUNT_MISMATCH` diagnostic).
- `uncertain: <n,m,...>` → `UNCERTAIN` with those candidates
  (`amount_match=False` where the amount differs).
- `no_match` → `NO_MATCH`.

**Defensive fallbacks (unchanged in spirit from today):**

- LLM exception → treat as `uncertain` (keep candidates rather than silently
  dropping them).
- LLM `no_match` but `_has_brand_overlap` detects a clear shared brand token
  → upgrade to `uncertain` with that candidate (the existing safety net).
- Unparseable LLM output → `no_match` (same as today's `_parse_verdict`
  default), subject to the brand-overlap upgrade above.

### Pass B — global reconciliation

After all transactions have a provisional `MatchResult`:

**Step 1.** Build a claims map: `claims[(source, db_id)] -> [result_index, ...]`
for every provisional `MATCH` result.

**Step 2.** For each `(source, db_id)` claimed by exactly one transaction:
keep the result as `MATCH`. The 1-to-1 postcondition holds.

**Step 3.** For each `(source, db_id)` claimed by ≥2 transactions (a
**conflict**): for **every** claimant of that row —

- change `status` to `UNCERTAIN`,
- clear `matched_source/matched_id/matched_name/matched_file`,
- move the contested candidate into `candidates[]` (de-duplicated),
- set `conflict = True`,
- append each *other* claimant's transaction description + date to
  `conflict_with`.

Per the agreed decision, **neither** transaction claims the row. The
conflict is surfaced for human review.

**Step 4.** Return results sorted by transaction date.

## 4. Status definitions (final)

| Status | Display | Assigned when |
|---|---|---|
| `MATCH` | ✅ Match | LLM is certain about one candidate (from amount-matching candidates or the name-only fallback). Pass B confirms no other tx contests the same row. Ruled-out amount-matching candidates listed in `candidates[]` for transparency. |
| `UNCERTAIN` | ⚠️ Uncertain — please verify | One of: (a) a candidate was claimed by ≥2 transactions in Pass B — list all claiming transactions via `conflict_with`; (b) one or more amount-matching candidates exist but the LLM gives no clear verdict — list the candidates; (c) no amount-match, but the name-only fallback found plausible candidates — list them (regpayment amount-difference cases appear here with `amount_match=False` + a note). |
| `NO_MATCH` | ❌ No match | No candidates at all from any source, or the LLM returned `no_match` on every source including the name-only fallback. |

### Dropped statuses and where their info goes

| Old status | Disposition |
|---|---|
| `MATCHED_LARGE_DELAY` | Dropped. The `RECEIPT_DATE_WINDOW_DAYS` SQL bound already filters stale receipts; anything outside the window never becomes a candidate. |
| `MATCHED_UNUSUAL_DELAY` | Dropped (same reason). |
| `MATCHED_UNREVIEWED` | Dropped as a status. The belegbot `manually_checked=NULL` + `confidence != "high"` info is preserved as a **note** on a `MATCH` result: `"Receipt flagged by belegbot — please verify"`. |
| `AMOUNT_MISMATCH` | Dropped as a status. A regpayment candidate that matches by name but differs in amount now surfaces as an `UNCERTAIN` candidate with `amount_match=False` and a note `"regpayment amount differs — update table if correct"`. |

### Statistics bucketing

- `MATCH` → matched bucket; its amount counts toward "total matched amount".
- `UNCERTAIN` → its own bucket (neither matched nor unmatched); reported in
  the header as `⚠️ N`.
- `NO_MATCH` → unmatched bucket; its amount counts toward "total unmatched
  amount".

Header line: `**Transactions:** N total  ✅ N  ⚠️ N  ❌ N`.

## 5. Implementation steps

The steps below are ordered for a single-pass implementation. Each step is
self-contained and leaves the codebase in a consistent state.

### Phase 1 — Data model (`pipeline/matcher.py`)

1. Replace the six status constants with three: `MATCH="match"`,
   `UNCERTAIN="uncertain"`, `NO_MATCH="no_match"`.
2. Replace `STATUS_DISPLAY` with the three-entry mapping above.
3. Add the `CandidateInfo` dataclass:
   ```python
   @dataclass
   class CandidateInfo:
       source: str                          # "receipt" | "regpayment"
       id: int
       name: str                            # issuer or reason
       file_name: Optional[str] = None
       amount_match: bool = True            # False = name-only fallback candidate
       date_gap_days: Optional[int] = None  # display only, no status impact
       note: Optional[str] = None           # e.g. "amount differs — update regpayment table"
   ```
4. Replace the `MatchResult` dataclass:
   ```python
   @dataclass
   class MatchResult:
       transaction: Transaction
       status: str
       matched_source: Optional[str] = None       # set only when status == MATCH
       matched_id: Optional[int] = None
       matched_name: Optional[str] = None
       matched_file: Optional[str] = None
       candidates: list[CandidateInfo] = field(default_factory=list)
       conflict: bool = False
       conflict_with: list[str] = field(default_factory=list)  # other claiming tx descriptions
       notes: list[str] = field(default_factory=list)
   ```
   `date_gap_days` is removed from `MatchResult` (it lives on `CandidateInfo`
   for display only).

### Phase 2 — DB layer (`storage/db_client.py`)

1. Add `get_receipt_candidates_by_date(bank_date, window_days) -> list[dict]`:
   same `SELECT` columns as `get_receipt_candidates`, but without the
   `total_amount = %s` predicate; bounded by
   `receipt_date <= bank_date` and
   `receipt_date >= DATE_SUB(bank_date, INTERVAL window_days DAY)`.
   Ordered by `ABS(DATEDIFF(receipt_date, bank_date))` then `receipt_date DESC`.
2. Reuse the existing `get_regpayment_candidates_by_date` for the name-only
   fallback and the regpayment amount-difference case.
3. Leave `get_receipt_candidates` and `get_regpayment_candidates` unchanged
   in signature (still used for the amount-matching pass). The `used_*_ids`
   sets are simply no longer passed to their callers — the sets no longer
   exist in `match_all`.

### Phase 3 — LLM prompt + choice (`pipeline/matcher.py`)

1. Add `_build_candidate_choice_prompt(tx, candidates, conservative=False) -> str`:
   presents the bank description + a numbered list of candidates with name,
   source, amount, and `amount_match` flag. The non-conservative variant
   asks the LLM to return `match: <n>`, `uncertain: <n,m,...>`, or `no_match`.
   The conservative variant (used in step 3 of Pass A) instructs the LLM to
   only return `match`/`uncertain` if probability is high.
2. Add `_choose_candidate(tx, candidates, conservative=False) -> tuple[str, list[int]]`:
   single LLM call per transaction. Returns `(verdict, selected_indices)`
   where `verdict` is `"match" | "uncertain" | "no_match"`. Defensive
   fallbacks:
   - LLM exception → `("uncertain", [all indices])` (keep candidates).
   - Unparseable output → `"no_match"`, then apply the brand-overlap safety
     net: if `_has_brand_overlap` detects a shared brand token for any
     candidate, upgrade to `("uncertain", [those candidate indices])`.
3. Extend `_parse_verdict` (or add a sibling `_parse_choice_verdict`) to
   parse `match: <n>` and `uncertain: <n,m,...>` patterns.
4. Retain `_strip_thinking` and `_has_brand_overlap` unchanged.

### Phase 4 — New `match_all` (`pipeline/matcher.py`)

Replace the body of `match_all` with the two-pass algorithm from §3:

- **Pass A:** iterate transactions (sorted by date). For each tx, gather
  amount-matching candidates; if any, call `_choose_candidate`; otherwise
  run the name-only fallback. Build provisional `MatchResult` objects.
- **Pass B:** build the claims map from provisional `MATCH` results.
  Resolve single-claimant rows (keep MATCH) and multi-claimant conflicts
  (downgrade all to UNCERTAIN with `conflict=True`). Return results sorted
  by transaction date.

Delete `_try_match_receipt`, `_try_match_regpayment`,
`_try_regpayment_amount_mismatch`, `_build_receipt_result`,
`_build_regpayment_result` (their logic is folded into Pass A + Pass B).
Delete `_compute_date_gap` and `_assign_delay_status` (no longer drive
status). Keep `_to_signed_cents` (still needed for the regpayment amount
query) and the `_BRAND_NOISE_TOKENS` / `_has_brand_overlap` safety net.

Add a belegbot-unreviewed-note helper: when building a `MATCH` result from a
receipt candidate whose `manually_checked is None and confidence != "high"`,
append `"Receipt flagged by belegbot — please verify"` to `notes`.

### Phase 5 — Report (`reporting/report.py`)

1. Import the three new statuses + `CandidateInfo`; drop imports of the six
   old statuses. Set `_WARNING_STATUSES = {UNCERTAIN}`.
2. `_row_details`: for `MATCH`, render `matched_name` + `matched_file`; for
   `UNCERTAIN`, render `candidates[]` as
   `"Possible candidates: Gastro Meier (receipt, €13.37, 3d), McDonalds Meier (receipt, €13.37, 1d)"`.
3. `_build_attention` (the uncertain section): one subsection per
   `UNCERTAIN` result. Render `conflict_with` as a bulleted list when
   `conflict=True` ("Also claimed by: 2026-04-12 McDonalds Meier"). Render
   any `notes` (including the belegbot-unreviewed note).
4. `_build_statistics`: three buckets (matched / uncertain / unmatched) +
   total matched amount + total unmatched amount.
5. Header: `**Transactions:** N total  ✅ N  ⚠️ N  ❌ N`.
6. No other structural changes to the report sections.

### Phase 6 — Tests

**Rewrite (contract preserved, mechanism changed):**
- `test_one_to_one_constraint` (U73) — the contested row's id must appear in
  at most one tx's `matched_id`; under the new design a conflict surfaces as
  two `UNCERTAIN` results with `conflict=True` rather than tx1 MATCH + tx2
  NO_MATCH.
- `test_match_all_commits_receipt_id_at_call_site` (U82) and
  `test_match_all_commits_regpayment_id_at_call_site` (U83) — rename; the
  "commit at call site" concept is gone. Re-assert the 1-to-1 postcondition
  holds after Pass B (no two `MATCH` results share `matched_id`).
- `test_step4_matcher.py` Tests 1–10 — update status expectations to the new
  three-status vocabulary.
- `test_step5_report.py` — update all 37 checks against the new status set
  and candidate rendering.

**Remove xfail (now passing):**
- `test_two_matches_smallest_gap_wins` (U55, H3) — deferred assignment +
  conflict flagging naturally resolves the tie.
- `test_order_dependence_diagonal_pairing` (U74, H3) — global reconciliation
  produces the diagonal pairing.

**Drop (status no longer exists):**
- `TestAssignDelayStatus` (U24–U28) — date tiers eliminated.
- Any test asserting `MATCHED_LARGE_DELAY` / `MATCHED_UNUSUAL_DELAY` /
  `MATCHED_UNREVIEWED` / `AMOUNT_MISMATCH` status values (e.g. step4 Tests
  2, 3, 4, 7).

**New tests:**
- `_build_candidate_choice_prompt` — contains all candidate names, the
  verdict format line, the transaction description; conservative variant
  includes the "only if high probability" instruction.
- `_choose_candidate` — parses `match: N`, `uncertain: N,M`, `no_match`;
  falls back to `uncertain` on LLM exception; upgrades `no_match`→`uncertain`
  on brand overlap.
- `match_all` end-to-end — conflict detection: two txs claim the same
  receipt → both `UNCERTAIN`, `conflict=True`, contested candidate in both
  `candidates[]`, each `conflict_with` lists the other.
- `match_all` end-to-end — name-only fallback: no amount match → candidate
  found via date-window search → `UNCERTAIN` with `amount_match=False`.
- `match_all` end-to-end — single claimant: stays `MATCH` (no false
  conflict).
- `match_all` end-to-end — belegbot-unreviewed note appears on `MATCH` when
  `manually_checked is None and confidence != "high"`.
- Report rendering — `candidates[]` appears in details; `conflict_with`
  appears in the uncertain section.

**Update (integration tests):**
- `tests/test_matcher_llm_integration.py` (I1–I5) — these currently call
  `_check_name_similarity` directly, which no longer exists. Rewrite each to
  call `_choose_candidate` with a single-candidate list and assert the
  verdict (`match`/`uncertain`/`no_match`) matches the expected set.

## 6. Verification

Run after implementation (single pass, end):

```bash
.venv\Scripts\python.exe -m pytest tests/test_matcher_helpers.py tests/test_matcher_branches.py tests/test_db_client_queries.py -v
.venv\Scripts\python.exe tests\test_step4_matcher.py
.venv\Scripts\python.exe tests\test_step5_report.py
```

Optionally (requires running Ollama + configured model):

```bash
.venv\Scripts\python.exe -m pytest -m integration tests/test_matcher_llm_integration.py -v
```

All tests must pass. The two H3 xfail tests must flip to passing (xfail
markers removed).

## 7. Out of scope

- No changes to `config.py` semantics. `DATE_TIER1_DAYS` /
  `DATE_TIER2_DAYS` become unused but can stay in `config.py` / `.env`
  for now (removing them is a separate cleanup). `RECEIPT_DATE_WINDOW_DAYS`
  remains in active use.
- No changes to `pipeline/extractor.py`, `kontocheck.py`, or the DB schema.
- No DB writes (kontocheck stays read-only).
- No changes to the belegbot integration or the shared DB schema.

## 8. Decisions log

| Decision | Choice | Rationale |
|---|---|---|
| Uncertain status | New `UNCERTAIN` status (not folded into `MATCHED` or `NO_MATCH`) | Cleanly separates "we have a likely match" from "we have no idea". |
| Conflict resolution | Flag both, claim neither | Most conservative; surfaces the conflict for human review without the system picking a winner. |
| Name-only fallback scope | Both receipts and regpayments | More thorough; regpayment amount-difference cases surface here. |
| LLM batch prompt scope | Per-tx decision, resolve in Phase B | Simpler prompts; no cross-tx context for the LLM. |
| Status set | 3 statuses only (`MATCH`/`UNCERTAIN`/`NO_MATCH`) | Simplicity per user request; date tiers and amount-mismatch folded into notes/candidates. |
| belegbot unreviewed flag | Keep as a note on `MATCH` | Preserves the PRD §6.4 feature without a separate status. |
| `date_gap_days` display | Keep on `CandidateInfo` for display only | Informational value in the candidate list; no status impact. |
| Execution cadence | Single pass, verify at end | Fewest interruptions; this plan saved as a checkpoint. |
