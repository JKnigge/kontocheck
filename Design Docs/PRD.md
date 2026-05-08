# kontocheck — Product Requirements Document

**Version:** 1.0  
**Date:** 2026-05-08  
**Status:** Approved — ready for implementation  

---

## 1. Purpose

kontocheck is a command-line tool that reconciles monthly bank account statements
against known receipts and regular payments. It tells the user which bank entries
are accounted for, which are missing a receipt, and which need a second look —
without requiring manual cross-referencing.

kontocheck is a companion tool to belegbot. It is independent and runs separately.

---

## 2. Background and motivation

The user scans receipts and invoices using belegbot, which extracts and stores
structured data (issuer, date, amount, payment method) in a MariaDB database.
Once per month, the user downloads a bank account statement from their bank as a
PDF. Currently there is no automated way to verify that every booking on the
statement corresponds to a known receipt or regular payment. kontocheck fills
this gap.

---

## 3. Users

Single user. The tool is personal financial tooling running on a local network.
No multi-user interface is required.

---

## 4. Goals

- Extract every transaction from a bank account statement PDF
- Match each transaction against known receipts or regular payments
- Flag transactions that are unmatched, ambiguous, or suspicious
- Produce a human-readable Markdown report the user can keep for their records
- Keep all data on the local network — no cloud services

---

## 5. Non-goals (explicitly out of scope)

- Processing multiple bank accounts or banks simultaneously
- Persisting state between runs or tracking history across months
- Checking payment frequency across multiple months
- Handling foreign currency transactions (flagged as unmatched, handled manually)
- Modifying or updating the receipts or regpayment tables
- Any graphical user interface
- Any scheduling or automation — the tool is triggered manually

---

## 6. Functional requirements

### 6.1 Input
- The user provides a single PDF bank account statement as a command-line argument
- The PDF is text-based (not a scanned image) and produced digitally by the bank
- The tool must be resilient to layout changes in the PDF — extraction is
  LLM-based, not positional

### 6.2 Extraction
- Extract every transaction from the statement as a structured list
- Each transaction must include at minimum: date, description, amount, direction
  (debit/credit)
- The tool must handle statements where the layout or column order may vary
  between runs

### 6.3 Matching
For each extracted transaction, kontocheck attempts to find a match in one of
two sources:

**Source A — receipts table (belegbot output)**
- Match on exact amount
- Match on name similarity between bank description and receipt issuer (LLM-assisted)
- Date constraint: bank entry date must be on or after the receipt date
- Expanding date search: if no match is found within tier 1, widen the search
  window before giving up (see Technical Specification for tier definitions)
- Large date gaps are flagged in the report but do not prevent a match

**Source B — regpayment table (manually maintained)**
- Match on exact amount
- Match on name/reason similarity (LLM-assisted)
- Date constraint: bank entry date must fall within start_date and end_date
  of the regpayment row (end_date = NULL means open-ended)
- Applies to both outgoing payments and income entries (isIncome = true/false)

**Matching constraints**
- 1-to-1: each receipt or regpayment row can be matched to at most one bank
  entry per run
- Amount matching is exact — no tolerance
- Foreign currency transactions will appear as unmatched

### 6.4 Bonus feature — unreviewed receipt flag
If a matched receipt has manually_checked = NULL and confidence != 'high' in
the receipts table, the report includes a note indicating the receipt was
flagged by belegbot and may not have been manually reviewed yet.

### 6.5 Output
- A single Markdown file saved to a configurable output folder
- Filename includes the statement period or a timestamp for easy archiving
- Transactions listed in chronological order, one row per transaction
- Each row includes: date, description, amount, status verdict, matched file or
  reason name, any flags or notes
- Summary section at the end grouping counts by status
- The report is the only output — no database writes, no file moves

### 6.6 Status verdicts

| Status | Meaning |
|--------|---------|
| ✅ Matched | Exact match found in receipts or regpayment |
| ⚠️ Matched — large delay | Match found but date gap exceeds tier 1 threshold |
| ⚠️ Matched — please verify | Match found but receipt was not manually reviewed after belegbot flagged it |
| ⚠️ Amount mismatch | regpayment match found but amount differs — update regpayment table |
| ❌ No match found | No corresponding receipt or regular payment found |

---

## 7. Constraints

- All processing on local network — Ollama, MariaDB, and file storage are local
- Must share .env configuration with belegbot (DB credentials, Ollama settings)
- Must run on Linux and Windows from the command line
- No additional system-level binary dependencies beyond what belegbot already requires
  (pdfplumber is pure Python — no poppler or Tesseract needed for kontocheck)

---

## 8. Assumptions

- By the time kontocheck is run, all belegbot receipts flagged for manual review
  have already been checked and corrected by the user
- The receipts table is therefore treated as a trusted source of truth
- The regpayment table is maintained manually by the user and is assumed correct
- Bank statement PDFs are always text-based — OCR is not required
