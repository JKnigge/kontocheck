# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

```bash
# Activate the venv (Windows)
.venv\Scripts\activate

# Run the tool end-to-end against a statement PDF
python kontocheck.py path/to/statement.pdf
python kontocheck.py path/to/statement.pdf --log-level DEBUG   # prints LLM candidate-choice verdicts

# pytest modules (mock DB + Ollama — safe to run together)
python -m pytest tests/test_matcher_helpers.py tests/test_matcher_branches.py tests/test_db_client_queries.py tests/test_matcher_llm_integration.py

# Legacy script-mode tests (NOT pytest modules — they call sys.exit() at module
# load and CRASH if collected by pytest. NEVER run `python -m pytest tests/`):
python tests/test_step4_matcher.py   # matcher regression (44 manual checks)
python tests/test_step5_report.py    # report rendering regression (39 manual checks)
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
- `pipeline/matcher.py` — the heart of the tool. Uses a **two-pass algorithm** (see `Design Docs/REDESIGN_PLAN.md`): **Pass A** iterates transactions (sorted by date) and for each one gathers amount-matching candidates from both tables (receipts: exact `total_amount` + `receipt_date ≤ bank_date` + within `RECEIPT_DATE_WINDOW_DAYS`; regpayments: exact **signed cents** + active date range, scoped by `REGPAYMENT_USER_ID`), then makes a **single LLM call** (`_choose_candidate`) over the whole candidate set. If no amount match exists, a name-only fallback searches by date window only (conservative prompt variant). **Pass B** (`_reconcile_conflicts`) builds a claims map from provisional `MATCH` results; when ≥2 transactions claim the same DB row, **all** claimants are downgraded to `UNCERTAIN` with `conflict=True` — neither wins. The 1-to-1 postcondition (each DB row matched to at most one transaction) is enforced here, **not** during iteration. The old `used_receipt_ids` / `used_regpayment_ids` sets and the greedy-commit-during-iteration approach are gone. Three statuses only: `MATCH`, `UNCERTAIN`, `NO_MATCH` (the six old statuses incl. `AMOUNT_MISMATCH` and the date-tier variants were collapsed — see `Design Docs/REDESIGN_PLAN.md §4`). Status constants and `STATUS_DISPLAY` are re-exported into `report.py`.
- `storage/db_client.py` — pooled MariaDB connection (single global `_connection`), only `SELECT`. The regpayment table stores amounts as **signed cents** (negative = debit), scoped by `REGPAYMENT_USER_ID`. Receipts are matched by `total_amount` (Decimal euros) and queried in date-descending order so the newest qualifying receipt wins ties.
- `reporting/report.py` — pure rendering, no LLM or DB. Filename derives from the **earliest transaction's year-month** (`kontocheck-YYYY-MM.md`); empty results fall back to a timestamped filename. Re-running overwrites the existing report for that period — this is intentional, the DB is the source of truth.

## Project context

kontocheck is the companion to **belegbot** (a separate receipt-scanner tool). They share only the database schema (`receipts`, `regpayment` tables) — separate DB users, separate Ollama configs, separate `.env`. kontocheck must remain **read-only** against both tables; do not introduce writes. See [[project_belegbot_independence]] in auto-memory.

The `Design Docs/` folder (PRD.md, TECHNICAL_SPEC.md, IMPLEMENTATION_PLAN.md,
REDESIGN_PLAN.md, MATCHER_REVIEW.md, TEST_PLAN_MATCHER.md) documents the design
history. PRD, TECHNICAL_SPEC, IMPLEMENTATION_PLAN, MATCHER_REVIEW and
TEST_PLAN_MATCHER each carry an "OUTDATED" banner at the top — they predate the
two-pass matcher redesign and are retained as a historical record only. The
current authoritative source for matching rules and status semantics is
`Design Docs/REDESIGN_PLAN.md` (status: Implemented). Code comments still
reference `TECHNICAL_SPEC §7.2` / `§7.3` by name for historical traceability;
treat those references as describing the pre-redesign design, not the current
behaviour.

## Testing notes

The test suite has two tiers:

- **pytest modules** (`tests/test_matcher_helpers.py`, `tests/test_matcher_branches.py`, `tests/test_db_client_queries.py`, `tests/test_matcher_llm_integration.py`) — mock `config`, `ollama.Client`, and `storage.db_client` at import time (see `test_matcher_helpers.py` for the canonical pattern: register a mock `config` module in `sys.modules` *before* importing the module under test, because matcher/extractor create the Ollama client at import time). The LLM integration tests are marked `@pytest.mark.integration` and skipped by default; run with `-m integration` against a live Ollama server.

- **Legacy script-mode tests** (`tests/test_stepN_*.py`) — call `sys.exit()` at module load and CRASH if collected by pytest. **NEVER run `python -m pytest tests/`** — run each as a standalone script instead. `test_step4_matcher.py` (44 manual checks) and `test_step5_report.py` (39 manual checks) are the regression baselines for matcher and report respectively.

See `AGENTS.md` for the full test inventory, xfail/defect map, and run commands.
