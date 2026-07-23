# AGENTS.md

This file provides guidance to AI agents working with code in this repository.

## Common commands

```bash
# Activate the venv (Windows)
.venv\Scripts\activate

# Run the tool end-to-end against a statement PDF
python kontocheck.py path/to/statement.pdf
python kontocheck.py path/to/statement.pdf --log-level DEBUG   # prints LLM name-similarity verdicts

# ── pytest modules (proper pytest, mock DB + Ollama) ──────────────────
# Run ALL pytest modules together (safe — only collects pytest-style files):
python -m pytest tests/test_matcher_helpers.py tests/test_matcher_branches.py tests/test_matcher_llm_integration.py tests/test_db_client_queries.py

# Individual pytest modules:
python -m pytest tests/test_matcher_helpers.py          # pure helper unit tests (U1–U41)
python -m pytest tests/test_matcher_branches.py         # branch logic unit tests (U42–U79)
python -m pytest tests/test_db_client_queries.py        # db_client SQL construction (mocked connection)
python -m pytest tests/test_matcher_llm_integration.py  # real-LLM integration tests (I1–I5), skipped by default

# Run with xfail details to see which expected-fail tests map to which defect
python -m pytest tests/test_matcher_helpers.py tests/test_matcher_branches.py -v --tb=short

# Integration tests (require a running Ollama instance with model from .env)
python -m pytest -m integration tests/test_matcher_llm_integration.py -v

# ── Legacy script-mode tests (NOT pytest modules — run as standalone scripts) ──
# These call sys.exit() at module load and CRASH if collected by pytest.
# NEVER run `python -m pytest tests/` — it will hit sys.exit() during collection.
python tests/test_step4_matcher.py                       # baseline matcher regression (12 manual checks)
python tests/test_step5_report.py                        # report rendering regression (37 manual checks)
python tests/test_step1_config.py                        # config.py loading (requires real .env)
python tests/test_step2_db_client.py                     # db_client live queries (requires real DB)
python tests/test_step3_extractor.py path/to/statement.pdf  # extractor end-to-end (requires Ollama + PDF)
```

There is no lint/format config in the repo. Dependencies: `pdfplumber`, `ollama`, `mysql-connector-python`, `python-dotenv`.

## Architecture

kontocheck is a **read-only**, single-PDF reconciliation tool. The flow is strictly linear and lives in `kontocheck.py:main`:

```
PDF ──► extract_text ──► parse_transactions ──► match_all ──► generate
       (pdfplumber)     (LLM via Ollama)      (DB + LLM)     (Markdown)
```

Modules and their boundaries:

- `config.py` — loads `.env`, validates required vars, switches stdout/stderr to UTF-8 (Windows cp1252 would otherwise break the ✅/⚠️/❌ emoji). **Imported first** in `kontocheck.py` so this setup runs before any other module logs. Required vars exit fast on missing; never silently default DB/Ollama settings.
- `pipeline/extractor.py` — PDF → text → LLM JSON → `Transaction` list. `_normalize_table_rows` collapses multi-line pipe-delimited rows into one logical row per transaction *before* the LLM sees them; bank statements routinely spread one entry across 2-3 lines and the LLM cannot reliably group continuation rows on its own. The Ollama client is created **once** at module level (see `BUG FIX 2` comment) — do not re-create it per call. Output uses `_strip_thinking` to remove `<think>...</think>` blocks from reasoning models like DeepSeek-R1. On JSON parse failure there is a single retry with a more explicit prompt; a second failure raises `RuntimeError`.
- `pipeline/matcher.py` — the heart of the tool. Per transaction, attempts in order: (1) receipts (exact `total_amount` + `receipt_date ≤ bank_date` + LLM name similarity), (2) regpayment (exact **signed cents** + active date range + LLM name similarity), (3) regpayment **amount mismatch** (name match only, used to flag stale regpayment rows). A definitive `"match"` from either source beats any `"uncertain"` candidate from either source — see `match_all` comment referencing `TECHNICAL_SPEC §7.2`. The 1-to-1 constraint is enforced by the `used_receipt_ids` / `used_regpayment_ids` sets carried through the whole run; once a DB row is committed to a transaction, it cannot match another. Status constants and `STATUS_DISPLAY` are re-exported into `report.py`.
- `storage/db_client.py` — pooled MariaDB connection (single global `_connection`), only `SELECT`. The regpayment table stores amounts as **signed cents** (negative = debit), scoped by `REGPAYMENT_USER_ID`. Receipts are matched by `total_amount` (Decimal euros) and queried in date-descending order so the newest qualifying receipt wins ties.
- `reporting/report.py` — pure rendering, no LLM or DB. Filename derives from the **earliest transaction's year-month** (`kontocheck-YYYY-MM.md`); empty results fall back to a timestamped filename. Re-running overwrites the existing report for that period — this is intentional, the DB is the source of truth.

## Project context

kontocheck is the companion to **belegbot** (a separate receipt-scanner tool). They share only the database schema (`receipts`, `regpayment` tables) — separate DB users, separate Ollama configs, separate `.env`. kontocheck must remain **read-only** against both tables; do not introduce writes. See [[project_belegbot_independence]] in auto-memory.

The `Design Docs/` folder (PRD.md, TECHNICAL_SPEC.md, IMPLEMENTATION_PLAN.md) is the authoritative source for matching rules and status semantics — when behavior is ambiguous, `TECHNICAL_SPEC §7.2` (matching order) and `§7.3` (filename/output rules) are referenced by name in code comments.

## Testing notes

### Test files and their roles

| File | Purpose | Runs without DB/Ollama? |
|---|---|---|
| `tests/test_matcher_helpers.py` | Pure helper unit tests (U1–U41): `_to_signed_cents`, `_strip_thinking`, `_has_brand_overlap`, `_compute_date_gap`, `_assign_delay_status`, `_parse_verdict`, `_build_similarity_prompt` | Yes — mocks config, ollama, db_client |
| `tests/test_matcher_branches.py` | Branch logic unit tests (U42–U79): `_check_name_similarity`, `_try_match_receipt`, `_try_match_regpayment`, `_try_regpayment_amount_mismatch`, `match_all` | Yes — mocks config, ollama, db_client |
| `tests/test_db_client_queries.py` | `db_client` SQL construction (mocked connection). Asserts H4 lower bound on `receipt_date` and existing ORDER BY / upper bound. | Yes — mocks mysql.connector.connect |
| `tests/test_matcher_llm_integration.py` | Real-LLM integration tests (I1–I5). Reads `OLLAMA_URL` and `OLLAMA_MODEL` from `.env`. Skipped unless run with `-m integration`. | **No** — requires running Ollama with the configured model |
| `tests/_helpers.py` | Shared fixtures: `Transaction`, `make_receipt`, `make_regpayment`, `make_tx` | N/A |
| `tests/test_step4_matcher.py` | **Legacy script-mode** (NOT a pytest module). Baseline matcher regression (41 manual checks). Calls `sys.exit()` at module load. | Yes — mocks config, ollama, db_client |
| `tests/test_step5_report.py` | **Legacy script-mode** (NOT a pytest module). Report rendering regression (37 manual checks). Calls `sys.exit()` at module load. | Yes — synthetic MatchResults |
| `tests/test_step1_config.py` | **Legacy script-mode**. `config.py` loading via real `.env`. Calls `sys.exit(1)` on missing `.env`. | **No** — requires real `.env` |
| `tests/test_step2_db_client.py` | **Legacy script-mode**. `db_client` live queries. Calls `sys.exit(1)` on missing DB. | **No** — requires real DB |
| `tests/test_step3_extractor.py` | **Legacy script-mode**. Extractor end-to-end against a PDF. Calls `sys.exit(1)` on missing PDF / Ollama. | **No** — requires Ollama + PDF |

### Mock pattern

All unit tests mock `config`, `ollama.Client`, and `storage.db_client` at import time (see `test_step4_matcher.py:34-45` for the canonical pattern: register a mock `config` module in `sys.modules` *before* importing the module under test, because matcher/extractor create the Ollama client at import time). Each unit test file is self-contained and sets up its own mocks.

### Xfail tests and defect tracking

Tests marked `@pytest.mark.xfail` encode known defects from `MATCHER_REVIEW.md`. Each xfail test has a `reason` string referencing the defect ID (e.g. `M7`, `H2`, `L13`). When a fix lands, the corresponding xfail test should flip to passing. **Do not remove xfail markers** unless the underlying defect has been fixed in `pipeline/matcher.py`.

Current xfail map (19 tests):

| Defect | Tests | Issue |
|---|---|---|
| M7 | 5 (U31–U35) | `_parse_verdict` doesn't strip punctuation/markdown/quotes, only checks first word |
| M6 | 2 (U39–U40) | `_build_similarity_prompt` has no separator between contract and data; contract not last line |
| H2 | 3 (U16, U18, U47) | `_has_brand_overlap` uses substring `in` check, causing compound-word false positives |
| H1 | 2 (U54, U78) | Credit-direction transactions should skip receipt matching |
| H3 | 2 (U55, U74) | No smallest-gap tiebreak for receipt candidates |
| H4 | 1 (U75) | No date window to reject stale receipts |
| L12 | 2 (U56, U62) | Empty issuer/reason candidates should be skipped before LLM call |
| L13 | 1 (U9) | `_strip_thinking` doesn't handle unclosed `мот` tags |
| L15 | 1 (U19) | Payment-method noise tokens like "Kartenzahlung" not excluded from brand overlap |

### Integration tests

`tests/test_matcher_llm_integration.py` uses `python-dotenv` to load `OLLAMA_URL` and `OLLAMA_MODEL` from the project `.env` file. These tests are skipped by default; run with:

```bash
python -m pytest -m integration tests/test_matcher_llm_integration.py -v
```

A `pytest.fixture` checks Ollama reachability and skips the module if the server is not available.

## Issue Resolution Protocol (MANDATORY)

When fixing an issue from the bug list, you MUST follow these steps
in order. After completing EACH step, output exactly:

  ⏸️ PAUSED — Step N/5 complete. Reply "continue" to proceed.

Then STOP. Do NOT proceed to the next step until the user explicitly
replies "continue". Proceeding without confirmation is a critical
error.

Steps:
1. Check whether existing tests cover the issue. Report findings.
2. If no tests exist, write at least one test that reproduces the bug.
3. Run the issue-specific tests. They MUST fail (confirming the test
   catches the bug).
4. Implement the fix for the issue.
5. Run the full test suite. All tests MUST pass.

After step 5, move to the next issue only after explicit user
confirmation.