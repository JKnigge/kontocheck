# kontocheck

A command-line tool that reconciles a monthly bank account statement (PDF) against
known receipts and regular payments stored in a MariaDB database, and writes a
human-readable Markdown report.

kontocheck reads the `receipts` table populated by the **belegbot** receipt-scanner. Thus, kontocheck requires read access to the relevant tables.

---

## What it does

1. Reads a text-based bank statement PDF.
2. Uses a local LLM (via [Ollama](https://ollama.com/)) to extract every
   transaction — date, description, amount, direction — regardless of the
   bank's layout.
3. Matches each transaction against:
   - the **`receipts`** table (exact amount + receipt date ≤ booking date +
     LLM name similarity), then
   - the **`regpayment`** table (exact signed-cent amount + active date range +
     LLM name similarity), then
   - a last-resort **amount mismatch** check against `regpayment` (name matches
     but amount differs — flags stale regpayment rows).
4. Enforces a 1-to-1 mapping: each DB row is matched to at most one transaction.
5. Writes a Markdown report to `OUTPUT_FOLDER` with a transactions table, an
   "items requiring attention" section, an unmatched list, and statistics.

Status verdicts used in the report:

| Status | Meaning |
|---|---|
| ✅ Matched | Receipt or regpayment found, date gap ≤ `DATE_TIER1_DAYS` |
| ⚠️ Matched — large delay | Date gap ≤ `DATE_TIER2_DAYS` |
| ⚠️ Matched — unusual delay | Date gap > `DATE_TIER2_DAYS` |
| ⚠️ Matched — please verify | Receipt flagged by belegbot, not manually reviewed |
| ⚠️ Amount mismatch | Name matches a regpayment row but the amount differs |
| ❌ No match found | No candidate in either source |

---

## Requirements

- **Python 3.10+** (uses PEP 604 type syntax, e.g. `list[str] | None`)
- A reachable **MariaDB / MySQL** server with `receipts` and `regpayment` tables.
  kontocheck only needs `SELECT` privileges on those two tables; use a dedicated
  read-only user — there is no need to reuse belegbot's database user.
- A reachable **Ollama** server with the model named in `OLLAMA_MODEL` pulled.
  The project is tuned for `deepseek-r1:14b`, but any chat model that follows
  the system prompts will work.

---

## Setup

### 1. Clone the repo and install the dependencies

```bash
git clone <repo-url> kontocheck
cd kontocheck

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

Dependencies: `pdfplumber`, `ollama`, `mysql-connector-python`, `python-dotenv`.

### 2. Prepare the database user

Create a dedicated MariaDB user with read access to the two tables kontocheck
queries. For example:

```sql
CREATE USER 'kontocheck'@'%' IDENTIFIED BY '<strong-password>';
GRANT SELECT ON your_database.receipts   TO 'kontocheck'@'%';
GRANT SELECT ON your_database.regpayment TO 'kontocheck'@'%';
FLUSH PRIVILEGES;
```

### 3. Prepare the Ollama server

On the host running Ollama, pull the model once:

```bash
ollama pull deepseek-r1:14b
```

Verify the server is reachable from the machine that will run kontocheck:

```bash
curl http://<ollama-host>:11434/api/tags
```

### 4. Create your `.env`

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable | Purpose |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Read-only DB credentials for kontocheck |
| `OLLAMA_URL` | URL of the Ollama server, e.g. `http://192.168.1.x:11434` |
| `OLLAMA_MODEL` | Model name, e.g. `deepseek-r1:14b` |
| `OUTPUT_FOLDER` | Where reports are written (default `kontocheck_reports`) |
| `DATE_TIER1_DAYS` | Receipt→booking gap that still counts as ✅ (default `5`) |
| `DATE_TIER2_DAYS` | Gap up to which it's flagged as "large delay" (default `14`) |
| `REGPAYMENT_USER_ID` | Integer `user` id whose regpayment rows are queried |

All non-optional variables are required at startup — the process exits with a
clear error if any are missing.

---

## Running the check

A typical end-to-end run looks like this:

```bash
# 1. Activate the virtualenv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 2. Download this month's statement from your bank as a PDF and save it
#    anywhere on disk, e.g. C:\Users\me\Downloads\statement-2026-04.pdf

# 3. Run kontocheck against that PDF
python kontocheck.py "C:\Users\me\Downloads\statement-2026-04.pdf"
```

What happens in order:

1. The effective configuration is printed (with the DB password masked) so you
   can confirm the tool is pointing at the right DB and Ollama.
2. The DB connection is tested. A misconfigured database fails fast here,
   before any LLM work is done.
3. The PDF is opened with `pdfplumber` and converted to text.
4. The text is sent to the LLM, which returns a JSON array of transactions.
5. Each transaction is matched against `receipts`, then `regpayment`, then the
   amount-mismatch check. Status verdicts are assigned.
6. A Markdown report is written to `OUTPUT_FOLDER`, named after the statement
   period (e.g. `kontocheck-2026-04.md`).
7. The final line printed to stdout is the absolute path of the report:

   ```
   Report written to: kontocheck_reports/kontocheck-2026-04.md
   ```

### Useful flags

```bash
python kontocheck.py statement.pdf --log-level DEBUG
```

`DEBUG` prints the LLM's name-similarity verdict for every receipt/regpayment
candidate it considers. Helpful when a transaction was unexpectedly flagged as
`❌ No match` or `⚠️ Amount mismatch`.

### Reading the report

Open the generated `.md` file in any Markdown viewer (VS Code, Obsidian,
GitHub preview). The report contains, in order:

- a header with the period, total transaction count, and ✅ / ⚠️ / ❌ counts,
- a full chronological transactions table,
- an **⚠️ Items requiring attention** section with one entry per warning,
- an **❌ Unmatched transactions** section listing entries kontocheck could
  not pair with anything,
- a statistics block with counts per status and the matched / unmatched euro
  totals.

The intended workflow is: skim the header, then jump to the ⚠️ and ❌
sections — those are the only items that need human action.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — report was written |
| `1` | Configuration or runtime error (DB unreachable, PDF missing, LLM failed, etc.) |
| `130` | Interrupted by the user (Ctrl+C) |

### Re-running a month

kontocheck does not persist state between runs. Re-running it against the same
PDF simply overwrites the existing report for that statement period
(`kontocheck-YYYY-MM.md`). This is safe — the database is never modified.

---

## Project layout

```
kontocheck.py              # CLI entry point and orchestration
config.py                  # Loads .env, validates required vars, UTF-8 console
pipeline/
  extractor.py             # PDF → text → LLM JSON → Transaction list
  matcher.py               # Transaction → MatchResult (receipts + regpayment)
storage/
  db_client.py             # Pooled MariaDB connection + candidate queries
reporting/
  report.py                # MatchResult list → Markdown report
tests/                     # Step-by-step unit tests for each module
Design Docs/               # PRD, technical spec, implementation plan
```

---

## Tests

```bash
python -m pytest tests/
```

The test files mirror the implementation steps: config, db_client, extractor,
matcher, report.

---

## Notes and limitations

- Single user, single bank account, single PDF per run — by design.
- The PDF must be text-based; scanned images are not OCR'd.
- Foreign-currency transactions are not handled and will appear as ❌ No match.
- kontocheck is read-only against the database; it never updates `receipts`
  or `regpayment`.
- All processing — including the LLM — runs on the local network. No data
  leaves the machine.
