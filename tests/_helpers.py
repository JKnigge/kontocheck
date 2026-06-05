"""
tests/_helpers.py — shared test fixtures for matcher test files.

Lifted from test_step4_matcher.py so they can be reused by
test_matcher_helpers.py and test_matcher_branches.py.
"""

from datetime import date, timedelta
from decimal import Decimal

from dataclasses import dataclass


@dataclass
class Transaction:
    date:        date
    description: str
    amount:      Decimal
    direction:   str
    raw_text:    str = ""


def make_receipt(
    id: int,
    issuer: str,
    amount: float,
    days_before_bank: int,
    confidence: str = "high",
    manually_checked=1,
    file_name: str = "20240101-Test.pdf",
) -> dict:
    """Build a fake receipts table row."""
    bank_date = date(2024, 4, 15)
    receipt_date = bank_date - timedelta(days=days_before_bank)
    return {
        "id": id,
        "issuer": issuer,
        "receipt_date": receipt_date,
        "total_amount": Decimal(str(amount)),
        "confidence": confidence,
        "manually_checked": manually_checked,
        "file_name": file_name,
    }


def make_regpayment(
    id: int,
    reason: str,
    amount_cents: int,
    start_date: date = date(2023, 1, 1),
    end_date=None,
) -> dict:
    """Build a fake regpayment table row."""
    return {
        "id": id,
        "reason": reason,
        "amount": amount_cents,
        "startDate": start_date,
        "endDate": end_date,
    }


def make_tx(
    description: str,
    amount: float,
    direction: str = "debit",
    tx_date: date = date(2024, 4, 15),
) -> Transaction:
    """Build a fake Transaction for testing."""
    return Transaction(
        date=tx_date,
        description=description,
        amount=Decimal(str(amount)),
        direction=direction,
    )
