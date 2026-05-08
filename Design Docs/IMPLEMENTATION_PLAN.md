# kontocheck — Implementation Plan

**Version:** 1.1  
**Date:** 2026-05-08  
**Status:** Ready to begin  

### Changelog
| Version | Change |
|---------|--------|
| 1.1 | Separate .env file; REGPAYMENT_USER_ID is integer; signed cents conversion documented; corrected regpayment column names; removed .env additions section (replaced by own .env) |
| 1.0 | Initial version |

---

## Project structure

```
kontocheck/
│
├── .env                        ← kontocheck-specific config (never commit)
├── .env.example                ← template with all settings documented
├── requirements.txt            ← Python dependencies
├── config.py                   ← loads and validates .env
├── kontocheck.py               ← entry point, argument parsing, orchestration
│
├── pipeline/
│   ├── __init__.py
│   ├── extractor.py            ← pdfplumber extraction + LLM transaction parsing
│   └── matcher.py              ← matching logic against receipts + regpayment
│
├── storage/
│   ├── __init__.py
│   └── db_client.py            ← read-only DB queries
│
├── reporting/
│   ├── __init__.py
│   └── report.py               ← Markdown report renderer
│
└── kontocheck_reports/         ← output folder (auto-created, configurable)
```

---

## Dependencies (requirements.txt)

```
pdfplumber>=0.11.0          # text extraction from text-based PDFs
ollama>=0.2.0               # Ollama Python client
mysql-connector-python>=8.3.0
python-dotenv>=1.0.0
```

No system-level binaries required. No Tesseract, no Poppler.

---

## Implementation steps

### Phase 1 — Foundation

---

#### Step 1 — `.env.example` + `requirements.txt` + `config.py`

**`.env.example`**: template with all settings and inline comments.
See Technical Specification section 4 for the full template.

**`requirements.txt`**: four dependencies as listed above.

**`config.py`**: loads and validates the kontocheck `.env` file.
All settings listed below, using the same `_require()` and `_int()` helper
pattern established in belegbot:

```python
# Database
DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

# Ollama
OLLAMA_URL, OLLAMA_MODEL

# Output
OUTPUT_FOLDER = Path(...)       # where reports are saved, auto-created

# Matching
DATE_TIER1_DAYS = int(...)      # default 5
DATE_TIER2_DAYS = int(...)      # default 14

# regpayment
REGPAYMENT_USER_ID = int(...)   # integer ID from the user table
```

Includes `print_config()` and `ensure_folders()` functions.

**Test:** `python -c "import config; config.print_config()"` — verify all
settings print correctly without errors.

---

#### Step 2 — `storage/db_client.py`

Read-only database access. Three functions:

```python
def test_connection() -> bool:
    """Verify DB is reachable at startup."""

def get_receipt_candidates(amount: Decimal, bank_date: date) -> list[dict]:
    """
    Return receipts where total_amount matches (euros) and
    receipt_date is on or before bank_date.
    Ordered by receipt_date DESC.
    """

def get_regpayment_candidates(signed_cents: int, bank_date: date) -> list[dict]:
    """
    Return regpayment rows where amount matches (signed cents),
    startDate <= bank_date, and endDate IS NULL or >= bank_date,
    for the configured REGPAYMENT_USER_ID.
    """
```

Returns plain dicts — keeps the DB layer decoupled from pipeline models.
The used_ids filtering is done in Python (in matcher.py), not in SQL, to
avoid dynamic query generation.

**Key detail:** `get_regpayment_candidates` receives already-converted signed
cents from the matcher. The DB layer does not perform any amount conversion.

**Test:** connect to DB, run both queries with known amounts, verify correct
rows are returned. Test the cents conversion: a debit of €950.00 should
query with `-95000` and return the rent row.

---

### Phase 2 — Extraction pipeline

---

#### Step 3 — `pipeline/extractor.py`

Two responsibilities:

**Part A — PDF text extraction (`extract_text`):**
```python
def extract_text(pdf_path: Path) -> str:
    """Extract and combine text from all pages using pdfplumber."""
```
Combines pages with double newlines. Raises `RuntimeError` on failure or
empty output.

**Part B — LLM transaction parsing (`parse_transactions`):**
```python
def parse_transactions(raw_text: str) -> list[Transaction]:
    """Send raw text to LLM, parse JSON array into Transaction objects."""
```

**Transaction dataclass** (defined in this file):
```python
@dataclass
class Transaction:
    date:        date
    description: str
    amount:      Decimal    # always positive, euros
    direction:   str        # "debit" | "credit"
    raw_text:    str
```

LLM prompt strategy:
- System prompt: bank statement parser, JSON only, no markdown
- User prompt: full raw text + JSON array format specification
- `think=False`, `_strip_thinking()` fallback (same as belegbot)
- `temperature=0.0`, `num_predict=2000` (statement may have many transactions)
- Retry once on parse failure with clarifying prompt
- Validate each transaction after parsing; skip and log invalid entries

**Test:** run against a real bank statement PDF. Print the transaction list
and verify count, dates, amounts, and directions are correct. This test also
validates pdfplumber output quality and informs prompt refinement if needed.

> **Important:** examine the raw pdfplumber output before finalising the
> prompt. Check for header rows, balance lines, multi-line descriptions, and
> column alignment quality. Adjust the prompt to instruct the LLM to skip
> non-transaction lines if needed.

---

#### Step 4 — `pipeline/matcher.py`

The core matching engine.

**Public interface:**
```python
def match_all(transactions: list[Transaction]) -> list[MatchResult]:
    """Match all transactions and return one MatchResult per transaction."""
```

**MatchResult dataclass** (defined in this file):
```python
@dataclass
class MatchResult:
    transaction:    Transaction
    status:         str
    matched_source: str | None   # "receipt" | "regpayment" | None
    matched_id:     int | None
    matched_name:   str | None
    matched_file:   str | None   # receipts.file_name if applicable
    date_gap_days:  int | None
    notes:          list[str]
```

**Status constants** (module-level strings):
```python
MATCHED               = "matched"
MATCHED_LARGE_DELAY   = "matched_large_delay"
MATCHED_UNUSUAL_DELAY = "matched_unusual_delay"
MATCHED_UNREVIEWED    = "matched_unreviewed"
AMOUNT_MISMATCH       = "amount_mismatch"
NO_MATCH              = "no_match"
```

**Amount conversion helper:**
```python
def _to_signed_cents(amount: Decimal, direction: str) -> int:
    """Convert euros + direction to signed cents for regpayment matching."""
    cents = int(amount * 100)
    return -cents if direction == "debit" else cents
```

**Name similarity helper:**
```python
def _check_name_similarity(bank_description: str, candidate_name: str) -> str:
    """Ask LLM: could X and Y refer to the same entity?
    Returns: 'match' | 'no_match' | 'uncertain'
    """
```
Short focused prompt, `temperature=0.0`, `think=False`, `num_predict=50`.

**Internal state:**
```python
used_receipt_ids:    set[int]  # populated during match_all
used_regpayment_ids: set[int]  # populated during match_all
```

**Test:** run with the transaction list from Step 3 and real DB data.
Verify:
- Correct status assigned per transaction
- 1-to-1 constraint works (two receipts with same amount match separately)
- Signed cents conversion correctly routes debits vs. credits
- Date tier thresholds trigger correct status labels

---

### Phase 3 — Report generation

---

#### Step 5 — `reporting/report.py`

Pure Markdown rendering from MatchResult objects. No LLM.

```python
def generate(
    results:     list[MatchResult],
    source_file: Path,
) -> Path:
    """Render Markdown report, save to OUTPUT_FOLDER, return path."""
```

**Report sections:**
1. Header — source filename, analysis timestamp, counts per status
2. Full transaction table — chronological, one row per transaction,
   columns: Date | Description | Amount | Direction | Status | Details
3. Attention section — one subsection per non-green result with full notes
4. Unmatched section — list of all NO_MATCH items
5. Statistics — totals per status, total matched €, total unmatched €

**Filename logic:**
Attempt to derive period from transaction dates (earliest month in statement).
Format: `kontocheck-2026-04.md`. Fall back to timestamp if undetermined:
`kontocheck-20260508-143200.md`.

**Test:** run with a synthetic set of MatchResult objects covering all six
status types. Open the Markdown file and verify formatting and completeness.

---

### Phase 4 — Orchestration

---

#### Step 6 — `kontocheck.py`

Entry point, argument parsing, and orchestration.

```python
def main() -> None:
    # 1. Parse arguments: pdf_path (positional), --log-level
    # 2. Setup logging (same pattern as belegbot)
    # 3. print_config(), ensure_folders()
    # 4. test_connection() — exit on failure
    # 5. extract_text(pdf_path)
    # 6. parse_transactions(raw_text)
    # 7. match_all(transactions)
    # 8. generate(results, pdf_path)
    # 9. Print report path to stdout
```

**Usage:**
```bash
python kontocheck.py path/to/statement.pdf
python kontocheck.py path/to/statement.pdf --log-level DEBUG
```

**Test:** full end-to-end run with a real bank statement. Read the resulting
Markdown report and verify all transactions are present with correct statuses.

---

## Build and test order

| Step | File(s) | Depends on | Testable independently |
|------|---------|------------|----------------------|
| 1 | `.env.example`, `requirements.txt`, `config.py` | nothing | ✅ yes |
| 2 | `storage/db_client.py` | config | ✅ yes |
| 3 | `pipeline/extractor.py` | config, ollama | ✅ yes |
| 4 | `pipeline/matcher.py` | db_client, extractor, ollama | ✅ yes |
| 5 | `reporting/report.py` | nothing (pure rendering) | ✅ yes |
| 6 | `kontocheck.py` | everything | ✅ full end-to-end |

---

## Pre-implementation checklist

Before writing any code, verify the following:

- [ ] MariaDB user `kontocheck` created with SELECT-only on `receipts`
      and `regpayment`
- [ ] `.env` filled in with correct DB credentials, Ollama URL, and
      `REGPAYMENT_USER_ID`
- [ ] `REGPAYMENT_USER_ID` confirmed by running
      `SELECT id FROM user WHERE ...` on the DB
- [ ] pdfplumber test run on a real statement PDF to assess text quality
      before writing the LLM extraction prompt (see note in Step 3)
- [ ] Confirmed that `amount` sign convention in regpayment matches
      expectations (negative = expense, positive = income)
