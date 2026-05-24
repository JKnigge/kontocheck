# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

```bash
# Activate the venv (Windows)
.venv\Scripts\activate

# Run the tool end-to-end against a statement PDF
python kontocheck.py path/to/statement.pdf
python kontocheck.py path/to/statement.pdf --log-level DEBUG   # prints LLM name-similarity verdicts

# Tests
python -m pytest tests/
python -m pytest tests/test_step4_matcher.py        # single module
python tests/test_step4_matcher.py                  # tests double as standalone scripts (mock config/ollama/db)
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

Tests in `tests/test_stepN_*.py` mirror the implementation steps. They mock `config`, `ollama`, and `storage.db_client` so the suite runs without a real DB or Ollama (see `test_step4_matcher.py:34-45` for the pattern: register a mock `config` module in `sys.modules` *before* importing the module under test, because matcher/extractor create the Ollama client at import time). Each test file is also runnable directly as a script.
