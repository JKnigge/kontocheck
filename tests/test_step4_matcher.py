"""Test script for Step 4: pipeline/matcher.py

Requires a .env file with valid DB credentials, Ollama settings, and REGPAYMENT_USER_ID.
Also requires a real bank statement PDF to extract transactions from first.
Adjust the PDF path and test values as needed.

Usage:
    python tests/test_step4_matcher.py path/to/statement.pdf
"""

import sys
import logging
from decimal import Decimal
from datetime import date
from pathlib import Path

from pipeline.extractor import Transaction, extract_text, parse_transactions
from pipeline.matcher import match_all, MatchResult, _to_signed_cents, _STATUS_DISPLAY
from storage.db_client import test_connection

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

print("=" * 60)
print("Step 4 Test: pipeline/matcher.py")
print("=" * 60)

# 1. Test _to_signed_cents helper
print("\n1. _to_signed_cents()")
cases = [
    (Decimal("43.20"), "debit", -4320),
    (Decimal("43.20"), "credit", 4320),
    (Decimal("950.00"), "debit", -95000),
    (Decimal("2500.00"), "credit", 250000),
]
for amount, direction, expected in cases:
    result = _to_signed_cents(amount, direction)
    status = "OK" if result == expected else "FAIL"
    print(f"   {amount} {direction:6s} → {result:>8d} (expected {expected:>8d}) [{status}]")

# 2. Test DB connection
print("\n2. test_connection()")
if not test_connection():
    print("   FAILED — cannot proceed")
    sys.exit(1)
print("   OK")

# 3. Test matching with extracted transactions
pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("statement.pdf")
if pdf_path.exists():
    print(f"\n3. Full matching from PDF '{pdf_path}'")
    try:
        raw_text = extract_text(pdf_path)
        transactions = parse_transactions(raw_text)
        print(f"   Extracted {len(transactions)} transactions")
        results = match_all(transactions)

        print(f"\n   {'Date':12s} {'Dir':7s} {'Amount':>10s}  {'Status':30s}  {'Matched Name'}")
        print(f"   {'-'*12} {'-'*7} {'-'*10}  {'-'*30}  {'-'*20}")
        for r in results:
            tx = r.transaction
            display = _STATUS_DISPLAY.get(r.status, r.status)
            name = r.matched_name or ""
            print(f"   {str(tx.date):12s} {tx.direction:7s} €{str(tx.amount):>9s}  {display:30s}  {name}")

        # Summary
        print(f"\n   Summary:")
        for status_key, display in _STATUS_DISPLAY.items():
            count = sum(1 for r in results if r.status == status_key)
            if count:
                print(f"     {display}: {count}")
    except RuntimeError as e:
        print(f"   FAILED: {e}")
else:
    print(f"\n3. Skipped — no PDF at '{pdf_path}'")
    print("   Usage: python tests/test_step4_matcher.py path/to/statement.pdf")

# 4. Test 1-to-1 constraint with synthetic data
print("\n4. 1-to-1 constraint (synthetic test)")
from unittest.mock import patch

tx1 = Transaction(date=date(2026, 4, 1), description="REWE", amount=Decimal("43.20"), direction="debit", raw_text="")
tx2 = Transaction(date=date(2026, 4, 2), description="REWE", amount=Decimal("43.20"), direction="debit", raw_text="")

receipt1 = {"id": 1, "file_name": "rewe1.pdf", "issuer": "REWE", "receipt_date": date(2026, 3, 30), "total_amount": Decimal("43.20"), "confidence": "high", "manually_checked": True}
receipt2 = {"id": 2, "file_name": "rewe2.pdf", "issuer": "REWE Filiale", "receipt_date": date(2026, 3, 31), "total_amount": Decimal("43.20"), "confidence": "high", "manually_checked": True}

with patch("pipeline.matcher.db_client.get_receipt_candidates", side_effect=[[receipt1, receipt2], [receipt1, receipt2]]):
    results = match_all([tx1, tx2])
    used_ids = {r.matched_id for r in results if r.matched_id is not None}
    print(f"   Matched IDs: {used_ids}")
    print(f"   1-to-1 enforced: {len(used_ids) == len(results)}  [{'OK' if len(used_ids) == len(results) else 'FAIL'}]")

print("\n" + "=" * 60)
print("All Step 4 tests complete.")
