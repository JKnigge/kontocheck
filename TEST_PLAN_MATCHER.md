# Test Plan: `pipeline/matcher.py`

## Context

Before implementing the fixes catalogued in `MATCHER_REVIEW.md` (project root),
we need a test suite that:

1. Locks in the current correct behaviour so the fixes don't regress it.
2. Encodes the discovered defects as failing tests today, which then turn green
   as each fix lands — the test suite becomes the acceptance criterion.
3. Stays fast and deterministic in the common case (mostly unit tests with
   no real Ollama or DB), with a small opt-in integration tier for cases
   where the LLM prompt itself is the thing under test.

Existing baseline: `tests/test_step4_matcher.py` covers the 6 status outcomes,
1-to-1 constraint, and chronological ordering using a dual-mode (script +
pytest) custom-runner pattern with shared helpers `make_receipt`,
`make_regpayment`, `make_tx`. The Ollama client is neutralized at import time
and `_check_name_similarity` is mocked as a whole. Coverage gaps: pure
helpers (`_has_brand_overlap`, `_strip_thinking`, `_to_signed_cents`
edge cases), the LLM error path, candidate ordering, the `_check_name_similarity`
prompt and verdict parser, and every defect listed in `MATCHER_REVIEW.md`.

This document defines test cases — not implementation. Each case lists:
- **Type:** UNIT (no external deps) or INTEGRATION (real Ollama)
- **Verifies:** what it asserts
- **Status today:** ✅ passes now / 🟥 expected to fail until the linked
  `MATCHER_REVIEW.md` item is fixed
- **Links:** which `MATCHER_REVIEW.md` ID the test covers, if any

---

## File organisation (proposed)

- **Keep `tests/test_step4_matcher.py` as-is** — it's the baseline regression
  net and dual-mode runnability is project convention.
- **Add `tests/test_matcher_helpers.py`** — pytest-style unit tests for the
  pure helpers (`_to_signed_cents`, `_strip_thinking`, `_has_brand_overlap`,
  `_compute_date_gap`, `_assign_delay_status`, new `_parse_verdict`).
  No mocks needed.
- **Add `tests/test_matcher_branches.py`** — pytest-style unit tests for
  `_try_match_receipt`, `_try_match_regpayment`,
  `_try_regpayment_amount_mismatch`, and `match_all` branching. Reuses the
  same `sys.modules` + `patch("ollama.Client")` trick as
  `test_step4_matcher.py`; lifts `make_receipt` / `make_regpayment` /
  `make_tx` into `tests/_helpers.py` so both files share them.
- **Add `tests/test_matcher_llm_integration.py`** — pytest-marked
  `@pytest.mark.integration`, skipped by default. Runs real Ollama against
  a small set of canonical German-bank cases to verify the prompt design.

Open question for the user before implementation: are we OK adding pytest
as a hard dependency (it's already used in CLAUDE.md commands, so this is
probably implicit yes), and OK introducing `@pytest.mark.integration`?

---

## Refactorings required to enable clean unit testing

These are SMALL, mechanical changes to `pipeline/matcher.py`. They preserve
behaviour and make the verdict parser + prompt independently testable —
which the existing all-or-nothing `_check_name_similarity` mock does not
allow.

### R1. Extract `_parse_verdict(text: str) -> str`
- **Why:** The verdict parser is the M7 defect site (`matcher.py:175`).
  Pulling the parsing into its own function lets us unit-test every
  malformed-LLM-output case in microseconds without touching Ollama.
- **Shape:**
  ```python
  def _parse_verdict(text: str) -> str:
      """Return 'match' | 'no_match' | 'uncertain' from a raw LLM reply.
      Defaults to 'no_match' when no recognisable verdict is found."""
      ...
  ```
- **Call site:** `_check_name_similarity` becomes
  `verdict = _parse_verdict(_strip_thinking(content))`.

### R2. Extract `_build_similarity_prompt(bank_description, candidate_name) -> str`
- **Why:** Lets us snapshot-test the M6 separator fix (no newline between
  contract and data) and the M9 few-shot rewrite without an LLM call.
- **Shape:** pure function returning the assembled prompt.

### R3. Lift fixtures into `tests/_helpers.py`
- **Why:** Reuse `make_receipt`, `make_regpayment`, `make_tx` across the
  new test files. Identical signatures to today's helpers.

### R4 (optional, recommended). Split `_build_receipt_result` / `_build_regpayment_result` into pure-build + explicit `used_*.add(...)` at the call site
- **Why:** Linked to L11. Removes silent mutation, makes a "preview" build
  testable without committing. Not blocking for tests, but if we touch
  these functions for L11 anyway, doing it as part of the test wave keeps
  the diff focused.

No other refactorings are needed. The existing import-time-mock pattern
covers everything else.

---

## Unit tests — pure helpers (`tests/test_matcher_helpers.py`)

### `_to_signed_cents`
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U1 | Debit €43.20 → -4320 | UNIT | ✅ | — |
| U2 | Credit €2500.00 → +250000 | UNIT | ✅ | — |
| U3 | Debit €0.01 → -1 | UNIT | ✅ | — |
| U4 | Debit €0.005 → consistent result (document banker's rounding behaviour) | UNIT | ✅ | L11 background |
| U5 | Debit €19.995 → -1999 or -2000 (lock the current behaviour explicitly) | UNIT | ✅ | — |

### `_strip_thinking`
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U6 | Paired `<think>...</think>` removed | UNIT | ✅ | — |
| U7 | No tags → unchanged | UNIT | ✅ | — |
| U8 | Multiple paired blocks | UNIT | ✅ | — |
| U9 | Unclosed `<think>...` (truncated) → reasoning stripped | UNIT | 🟥 | L13 |
| U10 | Empty input → empty output | UNIT | ✅ | — |

### `_has_brand_overlap`
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U11 | Candidate `"OBI GmbH"` vs desc `"Kartenzahlung OBI.SAGT.DANKE/Hamburg/DE"` → True | UNIT | ✅ | — |
| U12 | Candidate `"REWE"` vs desc `"SEPA Lastschrift TELEKOM"` → False | UNIT | ✅ | — |
| U13 | Only noise tokens overlap (`"GmbH Deutschland"` vs `"Sparkasse GmbH Deutschland"`) → False | UNIT | ✅ | — |
| U14 | Short tokens skipped (`"DM"` is <3 chars → not matched) | UNIT | ✅ | — |
| U15 | Case-insensitive (`"obi"` vs `"OBI"`) → True | UNIT | ✅ | — |
| U16 | **Compound-word false positive: `"Otto"` vs `"Lotto Niedersachsen"` → False after fix** | UNIT | 🟥 | H2 |
| U17 | **Compound-word false positive: `"Baur"` vs `"Baumarkt Hamburg"` → False after fix** | UNIT | 🟥 | H2 |
| U18 | **Compound-word false positive: `"Apo"` vs `"Apolda Stadtkasse"` → False after fix** | UNIT | 🟥 | H2 |
| U19 | Expanded noise tokens (e.g. `"Kartenzahlung"`) don't count as overlap | UNIT | 🟥 | L15 |

### `_compute_date_gap`
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U20 | Same day → 0 | UNIT | ✅ | — |
| U21 | Receipt 5 days before bank → 5 | UNIT | ✅ | — |
| U22 | Receipt after bank → negative (documents current behaviour) | UNIT | ✅ | — |

### `_assign_delay_status` (TIER1=5, TIER2=14 from mock config)
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U23 | Gap 0 → MATCHED | UNIT | ✅ | — |
| U24 | Gap 5 (TIER1 boundary) → MATCHED | UNIT | ✅ | — |
| U25 | Gap 6 → MATCHED_LARGE_DELAY | UNIT | ✅ | — |
| U26 | Gap 14 (TIER2 boundary) → MATCHED_LARGE_DELAY | UNIT | ✅ | — |
| U27 | Gap 15 → MATCHED_UNUSUAL_DELAY | UNIT | ✅ | — |

### `_parse_verdict` (R1 — pure function after refactor)
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U28 | `"match"` → `"match"` | UNIT | ✅ | — |
| U29 | `"no_match"` → `"no_match"` | UNIT | ✅ | — |
| U30 | `"uncertain"` → `"uncertain"` | UNIT | ✅ | — |
| U31 | `"match."` → `"match"` | UNIT | 🟥 | M7 |
| U32 | `"**match**"` → `"match"` | UNIT | 🟥 | M7 |
| U33 | `'"match"'` → `"match"` | UNIT | 🟥 | M7 |
| U34 | `"The answer is match"` → `"match"` | UNIT | 🟥 | M7 |
| U35 | `"Sure! no_match"` → `"no_match"` | UNIT | 🟥 | M7 |
| U36 | `""` → `"no_match"` (sensible default) | UNIT | ✅ today by accident; lock in | M7 |
| U37 | Garbage `"asdf"` → `"no_match"` (no recognisable verdict) | UNIT | ✅ today by accident; lock in | M7 |

### `_build_similarity_prompt` (R2 — pure function after refactor)
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U38 | Contains both `bank_description` and `candidate_name` substrings | UNIT | ✅ | — |
| U39 | Output contract line is the LAST line of the prompt | UNIT | 🟥 | M6 |
| U40 | At least one `\n\n` separates the data from the contract | UNIT | 🟥 | M6 |
| U41 | Contains the three required verdict tokens (`match`, `no_match`, `uncertain`) | UNIT | ✅ | — |

---

## Unit tests — branch logic (`tests/test_matcher_branches.py`)

### `_check_name_similarity` (mock `_client.chat`, not the whole function)
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U42 | Empty candidate name → `"no_match"`, no LLM call | UNIT | ✅ | — |
| U43 | LLM returns `"match"` → returns `"match"` | UNIT | ✅ | — |
| U44 | LLM raises exception → returns `"uncertain"` and logs a warning | UNIT | 🟥 untested today | — |
| U45 | LLM returns `"no_match"` + no brand overlap → `"no_match"` | UNIT | ✅ | — |
| U46 | LLM returns `"no_match"` + clear brand overlap → upgraded to `"uncertain"` | UNIT | ✅ | — |
| U47 | LLM returns `"no_match"` + ONLY substring (compound-word) overlap → `"no_match"` after H2 fix | UNIT | 🟥 | H2 |

### `_try_match_receipt` (mock `db_client` + `_check_name_similarity`)
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U48 | DB returns no candidates → `(None, None)` | UNIT | ✅ | — |
| U49 | One match candidate → definitive `MatchResult`, id added to `used_receipt_ids` | UNIT | ✅ | — |
| U50 | One uncertain candidate → `(None, candidate_dict)`, id NOT added | UNIT | ✅ | — |
| U51 | Candidates already in `used_receipt_ids` filtered out | UNIT | ✅ | — |
| U52 | Mix [uncertain, match] in order → match wins, uncertain dropped | UNIT | ✅ | — |
| U53 | Mix [match1, match2] → first match wins (TODO: revisit after H3) | UNIT | ✅ | H3 |
| U54 | **Credit-direction tx → `(None, None)` immediately, no DB call** | UNIT | 🟥 | H1 |
| U55 | Two `match` candidates with different gaps → smallest non-negative gap wins (after H3 minimum fix) | UNIT | 🟥 | H3 |
| U56 | Empty `issuer` candidate is not considered (after L12) | UNIT | 🟥 | L12 |

### `_try_match_regpayment` (mock `db_client` + `_check_name_similarity`)
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U57 | DB returns no candidates → `(None, None)` | UNIT | ✅ | — |
| U58 | One match → definitive result, id committed | UNIT | ✅ | — |
| U59 | One uncertain → returned uncommitted | UNIT | ✅ | — |
| U60 | Already-used IDs filtered | UNIT | ✅ | — |
| U61 | **Credit-direction income tx still attempts and finds regpayment match** (validates H1 scope note) | UNIT | ✅ today; lock in | H1 |
| U62 | Empty `reason` candidate is not considered (after L12) | UNIT | 🟥 | L12 |

### `_try_regpayment_amount_mismatch`
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U63 | Definitive name match with different amount → AMOUNT_MISMATCH result | UNIT | ✅ | — |
| U64 | Uncertain name match → AMOUNT_MISMATCH with "uncertain" note | UNIT | ✅ | — |
| U65 | No name match → `None` | UNIT | ✅ | — |
| U66 | **Row already in `used_regpayment_ids` still considered for mismatch detection (after H5 fix)** — covers the Spotify price-hike scenario | UNIT | 🟥 | H5 |

### `match_all` end-to-end branching (mock DB + similarity)
| # | Case | Type | Status today | Links |
|---|---|---|---|---|
| U67 | Receipt definitive match → uses receipt, regpayment never queried | UNIT | ✅ | — |
| U68 | No receipt, regpayment definitive → uses regpayment | UNIT | ✅ | — |
| U69 | Receipt uncertain + regpayment definitive → regpayment wins | UNIT | ✅ | — |
| U70 | Both uncertain → current code picks receipt_unc (lock in current behaviour; revisit later) | UNIT | ✅ | — |
| U71 | All paths fail + amount-mismatch hit → AMOUNT_MISMATCH | UNIT | ✅ | — |
| U72 | All paths fail + no amount-mismatch → NO_MATCH | UNIT | ✅ | — |
| U73 | Two txs vie for the same receipt → 1-to-1 (already covered by baseline, retain) | UNIT | ✅ | — |
| U74 | **Order-dependence regression: 2× €19.99 Amazon debits on D1/D2 + 2× receipts on D0/D1 — expect diagonal pairing after H3** | UNIT | 🟥 | H3 |
| U75 | **Stale-receipt: 2-year-old receipt of same amount must NOT be a candidate after H4 (verify by checking no LLM call for the stale row when window is enforced)** | UNIT | 🟥 | H4 |
| U76 | **Stale regpayment Spotify scenario: tx A claims €10.99, tx B €11.99 → tx A MATCHED + tx B AMOUNT_MISMATCH (after H5)** | UNIT | 🟥 | H5 |
| U77 | Income tx (credit, salary) → MATCHED via regpayment (validates H1 doesn't break income) | UNIT | ✅ today; lock in | H1 |
| U78 | Refund tx (credit) where a same-amount purchase receipt exists → must NOT match the receipt; falls through (after H1) | UNIT | 🟥 | H1 |
| U79 | `match_all` iterates with `enumerate(..., 1)` and progress log includes correct N/total — light sanity check | UNIT | ✅ | L14 |

---

## Integration tests — real Ollama (`tests/test_matcher_llm_integration.py`)

Marked `@pytest.mark.integration`. Skipped by default; run with
`pytest -m integration` or in CI when `OLLAMA_URL` is reachable. Justification:
each case verifies *the prompt itself* on real model behaviour — by definition
not mockable, because the thing under test IS the model's reaction to the
prompt. Kept to a minimum: 5 cases covering the diagnostic axes the unit
tests can't reach.

| # | Bank description | Candidate | Expected | Verifies |
|---|---|---|---|---|
| I1 | `"Kartenzahlung OBI.SAGT.DANKE/Hamburg/DE"` | `"OBI GmbH & Co. Deutschland KG"` | `"match"` | Clean brand-token case — sanity smoke test |
| I2 | `"Basislastschrift EDEKA SAGT DANKE/BERLIN"` | `"EDEKA Müller oHG"` | `"match"` | German chain with regional operator |
| I3 | `"Kartenzahlung Stadtwerke Hamburg"` | `"Stadtwerke München AG"` | `"no_match"` or `"uncertain"` | M9 — generic-token collision must NOT count as match |
| I4 | `"POS 4711 //DE"` | `"OBI Bau- und Heimwerkermärkte"` | `"uncertain"` | M9 — truncated description must trigger `uncertain`, not `no_match` |
| I5 | `"SEPA Überweisung MUELLER J K"` | `"Jan-Karl Müller"` | `"match"` | Personal-name case with abbreviated initials |

Why these are integration tests:
- I1, I2, I5 verify the prompt accepts the right things.
- I3 verifies the prompt rejects the right things (the high-risk case — current
  prompt with "shared brand token is sufficient" guidance leans permissive).
- I4 verifies `uncertain` is reachable at all in real use — a 100% match/no_match
  binary in practice would mean M9's three-way contract failed.

Smoke test budget: `num_predict`, `num_ctx`, and `stop` (M8) are exercised
implicitly by every integration test. No separate latency assertions —
those belong in a perf harness, not the test suite.

---

## Critical files

- `pipeline/matcher.py` — code under test; R1, R2 add two extracted functions
- `tests/test_step4_matcher.py` — unchanged (baseline regression)
- `tests/_helpers.py` — NEW; lifts `make_receipt` / `make_regpayment` /
  `make_tx` from `test_step4_matcher.py:121-172`
- `tests/test_matcher_helpers.py` — NEW; pure-helper unit tests (U1–U41)
- `tests/test_matcher_branches.py` — NEW; branch & end-to-end unit tests
  (U42–U79)
- `tests/test_matcher_llm_integration.py` — NEW; opt-in real-LLM tests
  (I1–I5)
- `MATCHER_REVIEW.md` — cross-referenced from every 🟥 test

---

## Verification

After test cases are implemented (separate step, separate plan):

1. **All ✅ tests pass on the current `main`** —
   `python -m pytest tests/test_step4_matcher.py tests/test_matcher_helpers.py tests/test_matcher_branches.py`
2. **All 🟥 tests fail on the current `main`**, with each failure traceable to
   a `MATCHER_REVIEW.md` ID. We document the expected-fail set so CI can
   distinguish "regression" from "known unfixed defect".
3. **After each `MATCHER_REVIEW.md` wave lands**, the linked 🟥 tests flip to
   ✅; ✅ tests stay green. This is the acceptance criterion for the fix.
4. **Integration tests** run opt-in:
   `python -m pytest -m integration tests/test_matcher_llm_integration.py`
   — used to validate the prompt rewrite (M9) and any future LLM-side changes.

---

## Coverage gaps explicitly NOT addressed by this plan

- Performance / LLM call count (M10) — qualitative goal; not amenable to a
  unit test without a real LLM. Would need an integration counter with a
  fixed input set; out of scope here.
- The global-assignment refactor (H3 full version) — only the minimum fix
  (smallest-gap tiebreak) is testable from where the code is today.
  Full Hungarian/greedy global assignment would need its own test wave.
- The control-flow agent flagged "receipt_unc beats regpay_unc
  unconditionally" as MED — not in `MATCHER_REVIEW.md` yet. U70 locks in
  the current behaviour; revisit if that finding is promoted to a fix.
