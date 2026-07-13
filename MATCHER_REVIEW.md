# Matcher Review — Findings & Fix Plan

Source: parallel review of `pipeline/matcher.py` and `storage/db_client.py` by three
focused agents (LLM prompt quality / control flow / candidate filtering & heuristics).
Citations use `file:line`.

This document is the reference to evaluate implementation changes against.
Each item below has an explicit status checkbox.

---

## HIGH — correctness bugs

### H1. Receipts can match credit/refund transactions
- [x] **Status:** done
- **Where:** `storage/db_client.py:42-57` (`get_receipt_candidates`), used from
  `pipeline/matcher.py:208` (`_try_match_receipt`).
- **Problem:** `get_receipt_candidates` filters only by `total_amount`. A €49.99
  refund (credit) collides with a €49.99 purchase receipt; the LLM happily
  confirms "Amazon ↔ Amazon".
- **Fix:** In `_try_match_receipt` ONLY, bail out early when
  `tx.direction != "debit"`:
  ```python
  def _try_match_receipt(tx, used_receipt_ids):
      if tx.direction != "debit":
          return (None, None)
      ...
  ```
- **Scope note — do NOT skip regpayment for credits.** Income (salary,
  refunds with a regpayment row) MUST still flow through
  `_try_match_regpayment` and `_try_regpayment_amount_mismatch`.
  `_to_signed_cents` (matcher.py:79-91) already converts credits into
  POSITIVE signed cents, and the employer's regpayment row is stored as
  positive cents, so the existing regpayment query handles income correctly.
  This fix only suppresses the receipts lookup, where there is no notion
  of "a receipt for a credit".
- **Eval criterion:** A credit-direction transaction never reaches
  `get_receipt_candidates`; debit receipt behaviour unchanged; an income
  transaction (credit, positive amount) still matches its regpayment row
  via the existing definitive / uncertain / amount-mismatch paths.

### H2. Substring brand-overlap fires on compound words
- [x] **Status:** done
- **Where:** `pipeline/matcher.py:129` (`_has_brand_overlap`).
- **Problem:** `token in desc_lower` is substring, not whole-word. Candidate
  `"Otto"` matches `"Lotto"`; `"Apo"` matches `"Apolda"` (city); `"Baur"` matches
  `"Baumarkt"`. German compounding amplifies this.
- **Fix:** Tokenize both sides and intersect token sets:
  ```python
  desc_tokens = {t.lower() for t in re.findall(r"[A-Za-zÄÖÜäöüß0-9]+", bank_description)}
  for raw_token in re.findall(r"[A-Za-zÄÖÜäöüß0-9]+", candidate_name):
      token = raw_token.lower()
      if len(token) < 3 or token in _BRAND_NOISE_TOKENS:
          continue
      if token in desc_tokens:
          return True
  return False
  ```
- **Eval criterion:** `_has_brand_overlap("Baumarkt Hamburg", "Baur") == False`;
  `_has_brand_overlap("OBI SAGT DANKE", "OBI GmbH") == True`.

### H3. Greedy chronological matching + `receipt_date DESC` tiebreak can mispair
- [ ] **Status:** open (architectural — defer to dedicated PR)
- **Where:** `pipeline/matcher.py:414` (`match_all` sort), `storage/db_client.py:51`
  (`ORDER BY receipt_date DESC`), `pipeline/matcher.py:236` (first-match-wins).
- **Problem:** Two €19.99 Amazon debits on D1/D2, two receipts on D0/D1. The
  earlier tx grabs the newer receipt, leaving the later tx with a stale receipt
  and an undeserved date-gap penalty.
- **Fix (minimum):** Within a single transaction, prefer the candidate with the
  smallest non-negative date gap that yields `match`, not the first one returned.
- **Fix (full):** Two-pass global assignment. Pass 1 collects all (tx, candidate,
  similarity, gap) triples without committing. Pass 2 resolves conflicts via
  greedy "smallest-gap first" over all `match` triples globally. (Scipy
  Hungarian is overkill at this candidate-set size.)
- **Eval criterion:** The D1/D2 × D0/D1 scenario in a unit test produces the
  diagonal pairing, not the off-diagonal one.

### H4. No lower bound on receipt date
- [x] **Status:** done
- **Where:** `storage/db_client.py:50` (`receipt_date <= bank_date`).
- **Problem:** A 2024 €4.50 receipt is a candidate for a 2026 €4.50 transaction.
  Small recurring amounts collide constantly; the LLM has no temporal awareness.
- **Fix:** Add a lower bound to the SQL:
  ```sql
  AND receipt_date >= DATE_SUB(%s, INTERVAL %s DAY)
  ```
  with the window ≈ `2 * DATE_TIER2_DAYS` (or 90 days as a safe default;
  configurable via `config.py`).
- **Eval criterion:** Receipts older than the window are filtered at the SQL
  layer and never reach `_check_name_similarity`.

### H5. Amount-mismatch detection blocked by `used_regpayment_ids`
- [ ] **Status:** open
- **Where:** `pipeline/matcher.py:369` (filter in `_try_regpayment_amount_mismatch`).
- **Problem:** Tx A (Spotify €10.99) claims the regpayment row definitively.
  Tx B (Spotify €11.99 after price hike) hits `_try_regpayment_amount_mismatch`,
  but the row is filtered out as already-used — tx B silently becomes `NO_MATCH`,
  and the "your regpayment table is stale" warning never surfaces.
- **Fix:** Drop the `used_regpayment_ids` filter at `matcher.py:369`. The
  AMOUNT_MISMATCH status is a *diagnostic about the regpayment table*, not a
  claim on a row; the 1-to-1 constraint should not apply to it.
- **Eval criterion:** With one regpayment row and two same-name transactions
  at different amounts, both definitive match (tx A) and AMOUNT_MISMATCH
  warning (tx B) are produced.

---

## MED — prompt & LLM-call quality

### M6. Prompt has no separator between output contract and data
- [ ] **Status:** open
- **Where:** `pipeline/matcher.py:163` → `:164`.
- **Problem:** String ends `"…uncertain".` and runs straight into
  `Bank statement description:` with no whitespace.
- **Fix:** Add `\n\n`, and reorder so data comes FIRST and the output contract
  is the LAST line of the prompt (small models weight final tokens most).
- **Eval criterion:** Assembled prompt has data lines first, then rules, then
  a final "Answer with exactly one lowercase word…" line.

### M7. Verdict parser is fragile
- [ ] **Status:** open
- **Where:** `pipeline/matcher.py:175`.
- **Problem:** `content.strip().lower().split()[0]` misclassifies `"match."`,
  `"**match**"`, `'"match"'`, `"The answer is match"` as `no_match`.
- **Fix:**
  ```python
  tokens = re.findall(r"[a-z_]+", content.lower())
  first_word = next((t for t in tokens if t in {"match", "no_match", "uncertain"}), "")
  ```
- **Eval criterion:** Unit test covering these five malformed outputs all
  produce the correct verdict.

### M8. Sampling budget wildly oversized for a one-word answer
- [ ] **Status:** open
- **Where:** `pipeline/matcher.py:171`.
- **Problem:** `num_predict=8000`, `num_ctx=32768` for a one-word verdict.
  Reasoning models can run away into a long `<think>` block on the latency tail.
- **Fix:**
  ```python
  options={
      "temperature": 0.0,
      "top_k": 5,
      "num_predict": 32,
      "num_ctx": 2048,
      "stop": ["\n", ".", ",", '"'],
  }
  ```
- **Eval criterion:** End-to-end matching latency drops measurably; correctness
  on existing test fixtures unchanged.

### M9. `uncertain` is undefined for the model
- [ ] **Status:** open
- **Where:** `pipeline/matcher.py:151-166` (prompt body).
- **Problem:** Three-state contract but no criteria for picking `uncertain` vs
  `no_match`. Model collapses to binary in practice.
- **Fix:** Define `uncertain` explicitly ("description too truncated to decide,
  OR only generic tokens overlap") and add 2-3 German few-shots
  (OBI / Stadtwerke / "POS 4711"). Suggested rewrite:
  ```
  Bank statement description: "{bank_description}"
  Candidate name: "{candidate_name}"

  These two strings were already matched by amount and date. Decide whether
  they refer to the same merchant/payee.

  German bank descriptions are mangled (e.g. "Kartenzahlung OBI.SAGT.DANKE/
  Hamburg/DE" for "OBI GmbH & Co. Deutschland KG"). Ignore legal suffixes
  (GmbH, AG, KG), cities, terminal IDs, and payment prefixes.

  Rules:
  - match: a distinctive brand/name token clearly identifies the same entity.
  - no_match: names clearly refer to different entities.
  - uncertain: description is too truncated/abbreviated to decide, OR only a
    generic token overlaps (e.g. "Stadtwerke", "Apotheke", "Tankstelle").

  Examples:
    "EDEKA SAGT DANKE//BERLIN" vs "EDEKA Müller oHG" -> match
    "Kartenzahlung Stadtwerke Hamburg" vs "Stadtwerke München AG" -> no_match
    "POS 4711 //DE" vs "OBI Bau- und Heimwerkermärkte" -> uncertain

  Answer with exactly one lowercase word and nothing else: match, no_match, or uncertain.
  ```
- **Eval criterion:** On a small labelled set of German-bank cases (Stadtwerke
  collision, truncated POS, clean EDEKA), the verdicts match the labels.

### M10. Pre-LLM cheap signals
- [ ] **Status:** open
- **Where:** `pipeline/matcher.py:134` (`_check_name_similarity`).
- **Problem:** Each candidate is one LLM call; 5 receipts + 3 regpayments per
  transaction = 8 calls. Most are easy decisions.
- **Fix:** Layered short-circuit before the LLM call:
  ```python
  # Token-set intersection over noise-filtered ≥5-letter tokens → "match"
  # Jaro-Winkler < 0.55 over normalized strings → "no_match"
  # Ambiguous → fall through to LLM
  ```
  Jaro-Winkler is ~5 lines of Python or `from rapidfuzz import fuzz`. Decision
  needed: take the rapidfuzz dependency or roll it inline.
- **Eval criterion:** LLM call count on a representative statement drops by
  ≥50% without changing the final per-transaction status distribution.

---

## LOW — hygiene

### L11. Silent set mutation in `_build_*` helpers
- [ ] **Status:** open
- **Where:** `pipeline/matcher.py:268, 341` (`used_*.add(c["id"])` inside
  functions named `_build_…`).
- **Fix:** Rename to `_commit_receipt_result` / `_commit_regpayment_result`,
  OR split into a pure `_build_…` + explicit `used_*.add(...)` at the call site.

### L12. Empty issuer/reason candidates are pointless
- [ ] **Status:** open
- **Where:** `storage/db_client.py:48` (receipts query), `:65`, `:82`
  (regpayment queries).
- **Fix:** Add `AND issuer IS NOT NULL AND issuer <> ''` (resp. `reason`) to
  the SELECT WHERE clauses.

### L13. `_strip_thinking` only handles paired tags
- [ ] **Status:** open
- **Where:** `pipeline/matcher.py:98`.
- **Fix:** Add a second pass for unclosed `<think>` blocks:
  ```python
  text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
  text = re.sub(r"<think>.*", "", text, flags=re.DOTALL)
  return text.strip()
  ```

### L14. Manual loop counter in `match_all`
- [ ] **Status:** open
- **Where:** `pipeline/matcher.py:412, 446` (`i = 1`, `i = i + 1`).
- **Fix:** `for i, tx in enumerate(sorted(transactions, key=lambda t: t.date), 1):`

### L15. Expand `_BRAND_NOISE_TOKENS` with bank-statement vocabulary
- [ ] **Status:** open
- **Where:** `pipeline/matcher.py:104-109`.
- **Fix:** Add: `kartenzahlung`, `lastschrift`, `basislastschrift`, `sepa`,
  `überweisung`, `gutschrift`, `dauerauftrag`, `paypal`, `danke`, `sagt`,
  `zahlung`, `betrag`, `auftrag`, `bank`, `konto`, `card`, `pos`, `terminal`,
  `dee`, `deu`, `mit`, `für`, `bei`, `auf`. Defends against extractor drift
  leaking bank text into stored `issuer` fields.

---

## Recommended implementation order

**Wave 1 — correctness (small, independently testable):**
H1, H2, H4, H5

**Wave 2 — prompt & LLM economics (do as a unit, they interact):**
M6, M7, M8, M9, M10

**Wave 3 — architectural:**
H3 (global assignment) as its own PR after Wave 1 lands.

**Wave 4 — hygiene (can be folded into any of the above):**
L11, L12, L13, L14, L15

---

## Evaluation checklist (after implementation)

- [ ] All existing tests in `tests/` still pass.
- [ ] New unit tests cover: credit-direction skip, whole-word brand overlap,
  date-window SQL filter, amount-mismatch double-claim scenario, parser
  robustness on malformed verdicts.
- [ ] No new writes to receipts or regpayment tables (read-only invariant
  per `CLAUDE.md`).
- [ ] LLM call count on a representative statement decreased.
- [ ] Status distribution on a known-good statement unchanged for cases the
  fixes weren't aimed at.
