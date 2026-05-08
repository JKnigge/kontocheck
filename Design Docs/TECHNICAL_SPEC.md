# kontocheck — Technical Specification & Design Document

**Version:** 1.1  
**Date:** 2026-05-08  
**Status:** Approved — ready for implementation  

### Changelog
| Version | Change |
|---------|--------|
| 1.1 | Separate .env file per tool; amounts in cents with sign encoding direction; user is integer ID; removed isIncome (sign-based); corrected regpayment column names from actual schema |
| 1.0 | Initial version |

---

## 1. Overview

kontocheck is a Python command-line tool. It accepts a single PDF bank statement
as input, extracts all transactions using pdfplumber and a local LLM, matches
each transaction against the receipts and regpayment database tables, and writes
a Markdown reconciliation report.

kontocheck is a companion tool to belegbot. It accesses the same MariaDB instance
and Ollama instance but is completely independent in code and configuration.

---

## 2. Technology stack

| Component | Tool | Reason |
|-----------|------|--------|
| Language | Python 3.11+ | Consistent with belegbot |
| PDF text extraction | pdfplumber | Purpose-built for text-based PDFs, preserves table structure well |
| LLM | Ollama + DeepSeek-R1 14B | Same local instance as belegbot, no cloud dependency |
| Database | mysql-connector-python | Same MariaDB instance as belegbot |
| Configuration | python-dotenv | Own .env file, independent from belegbot |

**Why pdfplumber instead of pypdf:**
pdfplumber is significantly better at extracting text from table-structured
documents like bank statements. It preserves column alignment and handles
multi-line rows more reliably, giving the LLM cleaner input to work with.

**No OCR required:**
Bank statements are digitally produced text-based PDFs. Tesseract and pdf2image
are not needed for kontocheck.

---

## 3. System architecture

```
kontocheck.py  (entry point)
      │
      ├── pipeline/
      │     ├── extractor.py    PDF text extraction + LLM transaction parsing
      │     └── matcher.py      matching logic against DB tables
      │
      ├── storage/
      │     └── db_client.py    read-only DB queries (receipts + regpayment)
      │
      ├── reporting/
      │     └── report.py       Markdown report generation
      │
      └── config.py             own .env loading, independent from belegbot
```

### Data flow

```
bank_statement.pdf
        │
        ▼
pdfplumber → raw text (all pages combined)
        │
        ▼
LLM → list of Transaction objects (JSON)
        │
        ▼
Matcher → for each Transaction:
            1. convert amount to signed cents
            2. query receipts table (candidates by amount in euros)
            3. query regpayment table (candidates by signed cents)
            4. apply date constraints
            5. apply name similarity check (LLM)
            6. assign match + status verdict
            7. mark matched row as used (1-to-1 constraint)
        │
        ▼
Report generator → Markdown file
```

---

## 4. Configuration

kontocheck has its own `.env` file, independent from belegbot. This allows
separate database users and independent configuration of each tool.

### .env.example

```ini
# ── Database ──────────────────────────────────────────────
# kontocheck uses a dedicated read-only DB user
DB_HOST=192.168.1.x
DB_PORT=3306
DB_NAME=your_database_name
DB_USER=kontocheck
DB_PASSWORD=your_password

# ── Ollama ────────────────────────────────────────────────
OLLAMA_URL=http://192.168.1.x:11434
OLLAMA_MODEL=deepseek-r1:14b

# ── Output ────────────────────────────────────────────────
OUTPUT_FOLDER=kontocheck_reports

# ── Matching ──────────────────────────────────────────────
# Date tier thresholds (days between receipt date and bank booking date)
DATE_TIER1_DAYS=5     # within this: ✅ matched
DATE_TIER2_DAYS=14    # within this: ⚠️ matched, large delay
                      # beyond tier 2: ⚠️ matched, unusual delay

# ── regpayment ────────────────────────────────────────────
# Integer user ID from the user table — only this user's rows are queried
REGPAYMENT_USER_ID=1
```

**Recommended DB setup:**
Create a dedicated MariaDB user for kontocheck with SELECT-only permissions
on the `receipts` and `regpayment` tables. kontocheck never writes to the DB.

---

## 5. Data models

### 5.1 Transaction (extracted from PDF)

Amounts are stored in euros as extracted from the PDF. Conversion to signed
cents happens at match time in matcher.py.

```python
@dataclass
class Transaction:
    date:        date
    description: str        # raw text from bank statement
    amount:      Decimal    # always positive, in euros (e.g. Decimal("43.20"))
    direction:   str        # "debit" (money out) or "credit" (money in)
    raw_text:    str        # original line(s) from PDF for reference
```

### 5.2 MatchResult

```python
@dataclass
class MatchResult:
    transaction:    Transaction
    status:         str          # see status verdicts below
    matched_source: str | None   # "receipt" | "regpayment" | None
    matched_id:     int | None   # DB row id of the matched record
    matched_name:   str | None   # issuer or reason for display
    matched_file:   str | None   # file_name from receipts table if applicable
    date_gap_days:  int | None   # days between receipt date and bank date
    notes:          list[str]    # flags, warnings, LLM remarks
```

### 5.3 Status verdicts

| Constant | Display | Condition |
|----------|---------|-----------|
| MATCHED | ✅ Matched | Exact match, date within tier 1 |
| MATCHED_LARGE_DELAY | ⚠️ Matched — large delay | Match found, date gap in tier 2 |
| MATCHED_UNUSUAL_DELAY | ⚠️ Matched — unusual delay | Match found, date gap beyond tier 2 |
| MATCHED_UNREVIEWED | ⚠️ Matched — please verify | Match found, receipt not manually reviewed (bonus feature) |
| AMOUNT_MISMATCH | ⚠️ Amount mismatch | regpayment name match but amount differs |
| NO_MATCH | ❌ No match found | No candidate found after full search |

---

## 6. Amount handling

### Cents vs. euros
The `receipts` table stores `total_amount` as `DECIMAL(10,2)` in **euros**
(e.g. `43.20`).

The `regpayment` table stores `amount` as `BIGINT` in **cents**
(e.g. `4320` for €43.20).

The `Transaction` dataclass stores amounts in euros as extracted from the PDF.

**Conversion happens in matcher.py at match time:**

```python
def _to_signed_cents(amount: Decimal, direction: str) -> int:
    """
    Convert a transaction amount (euros, always positive) to signed cents
    for comparison against the regpayment table.

    direction="debit"  → negative cents (money leaving the account)
    direction="credit" → positive cents (money entering the account)
    """
    cents = int(amount * 100)
    return -cents if direction == "debit" else cents
```

### Sign convention in regpayment
The `amount` column uses sign to encode direction:
- Positive value → income (salary, reimbursements)
- Negative value → expense (rent, insurance, subscriptions)

A bank statement debit of €950.00 is matched against `amount = -95000`.
A bank statement credit of €2500.00 is matched against `amount = 95000`.

### receipts table matching
The `receipts.total_amount` column is in euros (DECIMAL). Comparison is
direct against `Transaction.amount` without conversion.

---

## 7. Pipeline detail

### 7.1 PDF extraction (extractor.py)

**Part A — pdfplumber text extraction:**
```python
import pdfplumber

with pdfplumber.open(path) as pdf:
    pages = [page.extract_text() for page in pdf.pages]
raw_text = "\n\n".join(filter(None, pages))
```

pdfplumber is run with default settings. For table-structured statements it
preserves column alignment well enough that the LLM can parse it reliably.
Raises `RuntimeError` if the file cannot be opened or produces empty text.

**Part B — LLM transaction parsing:**
The raw text is sent to DeepSeek via Ollama with a prompt asking it to extract
a JSON array of transactions. Each transaction must include date, description,
amount (positive decimal in euros), and direction ("debit" or "credit").

The same `think=False` and `_strip_thinking()` approach from belegbot is applied.
Retries once on parse failure with a clarifying prompt before giving up.

Validates each parsed transaction: date parseable, amount numeric and positive,
direction is "debit" or "credit". Invalid transactions are logged and skipped.

### 7.2 Matching logic (matcher.py)

The matcher processes transactions one at a time in chronological order.
Two sets track used rows: `used_receipt_ids` and `used_regpayment_ids`,
enforcing the 1-to-1 constraint across the full run.

**For each transaction:**

```
1. Convert amount to signed cents for regpayment matching

2. Query receipts WHERE total_amount = transaction.amount (euros)
   AND receipt_date <= transaction.date
   → candidate list, ordered by receipt_date DESC (most recent first)
   Filter out ids already in used_receipt_ids

3. Query regpayment WHERE amount = signed_cents
   AND startDate <= transaction.date
   AND (endDate IS NULL OR endDate >= transaction.date)
   AND user = REGPAYMENT_USER_ID
   Filter out ids already in used_regpayment_ids

4. If no candidates from either source → NO_MATCH

5. For each candidate (receipts first, then regpayment):
   a. LLM name similarity check:
      "Could bank description X refer to the same entity as Y?"
      → returns: "match" | "no_match" | "uncertain"
   b. If "match":
      - calculate date_gap_days
      - assign status tier based on gap (tier 1 / tier 2 / beyond)
      - check manually_checked flag if source is receipts (bonus feature)
      - add id to used_receipt_ids or used_regpayment_ids
      - return MatchResult
   c. If "no_match": continue to next candidate
   d. If "uncertain": note it, continue but keep as fallback

6. If no definitive match but one uncertain candidate exists:
   → return that candidate as MATCHED with uncertainty note in notes

7. If still no match → NO_MATCH
```

**Receipts checked before regpayment:**
Card and cash payments are more likely to have a receipt than to be a regular
payment. Checking receipts first reduces false matches against regpayment rows.

**Name similarity via LLM:**
A focused binary prompt: "Could the bank description X refer to the same
entity as Y?" This handles semantic gaps like "XYZ Systemgastronomie GmbH"
matching "McDonald's" naturally. Short prompt, `temperature=0.0`, `think=False`,
50 token budget.

### 7.3 Report generation (report.py)

Rendered directly from MatchResult objects — no LLM involvement.

```markdown
# kontocheck — Statement [Period]

**Analysed:** [timestamp]
**Transactions:** N total  ✅ N  ⚠️ N  ❌ N

---

## Transactions

| Date | Description | Amount | Direction | Status | Details |
|------|-------------|--------|-----------|--------|---------|
| ...  | ...         | ...    | ...       | ...    | ...     |

---

## ⚠️ Items requiring attention
[one subsection per non-green item with full details and flags]

## ❌ Unmatched transactions
[list with amounts — need manual receipts or regpayment entries]

## Statistics
[totals per status, total matched amount, total unmatched amount]
```

Report filename: `kontocheck-2026-04.md` (from statement period) or
`kontocheck-20260508-143200.md` (timestamp fallback).

---

## 8. Database access

kontocheck is **read-only**. It never inserts, updates or deletes any rows.

### Actual table schemas used

**receipts table (written by belegbot):**
```sql
id, file_name, issuer, receipt_date (DATE), total_amount (DECIMAL 10,2 in euros),
confidence, manually_checked
```

**regpayment table (manually maintained):**
```sql
id INT AUTO_INCREMENT PRIMARY KEY,
amount BIGINT          -- in cents; positive=income, negative=expense
reason VARCHAR(255)    -- human-readable description
user INT               -- foreign key to user table (integer ID)
shared TINYINT(1)
startDate TIMESTAMP    -- inclusive start of validity period
endDate TIMESTAMP      -- inclusive end; NULL means open-ended
frequency VARCHAR(32)  -- 'MONTHLY' or 'YEARLY'
monthlyAmount INT      -- computed/stored column, not used by kontocheck
createdAt TIMESTAMP
updatedAt TIMESTAMP
```

### Queries

**receipts candidates:**
```sql
SELECT id, file_name, issuer, receipt_date, total_amount,
       confidence, manually_checked
FROM receipts
WHERE total_amount = %s       -- euros as Decimal
  AND receipt_date <= %s      -- bank entry date
ORDER BY receipt_date DESC
```

**regpayment candidates:**
```sql
SELECT id, amount, reason, frequency, startDate, endDate
FROM regpayment
WHERE amount = %s             -- signed cents as int
  AND startDate <= %s         -- bank entry date
  AND (endDate IS NULL OR endDate >= %s)
  AND user = %s               -- integer user ID
```

Both queries return all candidates; Python-side filtering excludes already-
matched IDs using used_receipt_ids / used_regpayment_ids sets to avoid
dynamic SQL generation.

---

## 9. LLM usage summary

| Call | Purpose | Temperature | think=False | Max tokens |
|------|---------|-------------|-------------|------------|
| Transaction extraction | Parse raw PDF text into structured JSON array | 0.0 | Yes | 2000 |
| Name similarity | Binary: does description X match entity Y? | 0.0 | Yes | 50 |

The name similarity call uses a very low token budget (50) since the answer
is always one of three short strings: "match", "no_match", or "uncertain".

---

## 10. Error handling

| Situation | Handling |
|-----------|---------|
| PDF cannot be opened | Exit with clear error message |
| pdfplumber extracts empty text | Exit — likely wrong file type |
| LLM extraction fails or unparseable | Retry once, then exit |
| DB connection fails | Exit at startup |
| Individual name similarity call fails | Log warning, treat as "uncertain" |
| Transaction with foreign currency | Appears as NO_MATCH — handled manually |
| regpayment amount sign mismatch | Correctly handled by signed cents conversion |

kontocheck exits early on fatal errors. Individual matching failures are
logged but do not stop the run.

---

## 11. Key design decisions and rationale

**Separate .env file and dedicated DB user:**
Each tool has its own configuration and its own DB user with appropriate
permissions. belegbot needs INSERT + SELECT on receipts; kontocheck needs
SELECT-only on receipts and regpayment. Separate configs enforce least-privilege
at the DB level and prevent one tool's configuration from affecting the other.

**Amounts in cents with sign encoding direction:**
The regpayment table uses signed BIGINT cents. The sign encodes direction
(positive = income, negative = expense), eliminating the need for a separate
isIncome column. Conversion from euros to signed cents happens once in
matcher.py at match time, keeping Transaction amounts in human-readable euros
throughout the rest of the code.

**pdfplumber over pypdf:**
pypdf extracts text but loses table structure. pdfplumber preserves it, giving
the LLM significantly better input for a tabular document like a bank statement.

**LLM for name matching instead of fuzzy string matching:**
Fuzzy matching (e.g. Levenshtein distance) cannot handle the semantic gap
between "XYZ Systemgastronomie GmbH" and "McDonald's". The LLM resolves this
naturally. The call is cheap (short prompt, 50 token budget) and deterministic
at temperature 0.0.

**Receipts checked before regpayment:**
A debit card payment is almost always a one-off purchase with a receipt.
Checking receipts first avoids incorrectly matching a grocery shop against
a regular payment with the same amount.

**No LLM for report generation:**
The kontocheck report is fully deterministic. Status verdicts are
self-explanatory and do not require narrative interpretation.

**No state persistence:**
Each run is fully self-contained. The receipts and regpayment tables provide
all necessary reference data. This keeps the tool simple and avoids migration
or cleanup concerns.
