"""Test script for Step 2: storage/db_client.py

Requires a .env file with valid DB credentials and REGPAYMENT_USER_ID.
Adjust the test values (amounts, dates) to match data in your database.
"""

from decimal import Decimal
from datetime import date
from storage.db_client import test_connection, get_receipt_candidates, get_regpayment_candidates

print("=" * 60)
print("Step 2 Test: storage/db_client.py")
print("=" * 60)

# 1. Test connection
print("\n1. test_connection()")
ok = test_connection()
print(f"   Result: {'OK' if ok else 'FAILED'}")
if not ok:
    print("   Cannot proceed — fix DB credentials in .env")
    exit(1)

# 2. Test receipt candidates
#    Adjust the amount and date to values that exist in your receipts table
print("\n2. get_receipt_candidates()")
test_amount = Decimal("43.20")  # <-- change to an amount in your DB
test_date = date(2026, 4, 30)   # <-- change to a date after some receipts
receipts = get_receipt_candidates(test_amount, test_date)
print(f"   Query: amount={test_amount}, bank_date={test_date}")
print(f"   Rows returned: {len(receipts)}")
for r in receipts:
    print(f"     id={r['id']} issuer={r['issuer']} date={r['receipt_date']} amount={r['total_amount']} file={r['file_name']}")

# 3. Test regpayment candidates
#    Adjust signed_cents to match a row in your regpayment table
#    Negative = expense, Positive = income (e.g. -95000 = €950.00 debit)
print("\n3. get_regpayment_candidates()")
test_cents = -95000  # <-- change to a value in your regpayment table
test_date2 = date(2026, 4, 30)
regpayments = get_regpayment_candidates(test_cents, test_date2)
print(f"   Query: signed_cents={test_cents}, bank_date={test_date2}")
print(f"   Rows returned: {len(regpayments)}")
for r in regpayments:
    print(f"     id={r['id']} reason={r['reason']} amount={r['amount']} start={r['startDate']} end={r['endDate']} freq={r['frequency']}")

# 4. Test with no-match values
print("\n4. No-match queries (should return 0 rows)")
no_receipts = get_receipt_candidates(Decimal("0.01"), date(2026, 4, 30))
no_regpayments = get_regpayment_candidates(1, date(2026, 4, 30))
print(f"   Receipts for €0.01: {len(no_receipts)} rows")
print(f"   Regpayments for 1 cent: {len(no_regpayments)} rows")

print("\n" + "=" * 60)
print("All Step 2 tests complete.")
