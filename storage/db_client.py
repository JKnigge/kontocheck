import logging
from datetime import date
from decimal import Decimal

import mysql.connector

import config

logger = logging.getLogger(__name__)

_connection = None


def _get_connection():
    global _connection
    if _connection is not None and _connection.is_connected():
        return _connection
    _connection = mysql.connector.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
    )
    return _connection


def test_connection() -> bool:
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        logger.info("Database connection successful.")
        return True
    except mysql.connector.Error as e:
        logger.error("Database connection failed: %s", e)
        return False


def get_receipt_candidates_by_date(
    bank_date: date, window_days: int, amount: Decimal | None = None
) -> list[dict]:
    """
    Return receipt rows within [bank_date - window_days, bank_date], with an
    optional exact-amount filter. Used both for amount-matching (Pass A Step 1,
    with ``amount`` set) and the name-only fallback (Pass A Step 3, without).

    When ``amount`` is None the total_amount predicate is omitted and results
    are ordered by date proximity (ascending ABS(DATEDIFF)) then receipt_date
    DESC so the closest receipts surface first. When ``amount`` is given the
    predicate ``total_amount = %s`` is added and results are ordered by
    receipt_date DESC (the amount match already narrows the pool; date
    proximity ordering is only needed for the name-only fallback).
    """
    select = (
        "SELECT id, file_name, issuer, receipt_date, total_amount, "
        "confidence, manually_checked FROM receipts "
        "WHERE receipt_date <= %s "
        "AND receipt_date >= DATE_SUB(%s, INTERVAL %s DAY) "
        "AND issuer IS NOT NULL AND issuer <> ''"
    )
    if amount is not None:
        select += " AND total_amount = %s"
        order_by = " ORDER BY receipt_date DESC"
        params: tuple = (bank_date, bank_date, window_days, amount)
    else:
        order_by = " ORDER BY ABS(DATEDIFF(receipt_date, %s)) ASC, receipt_date DESC"
        params = (bank_date, bank_date, window_days, bank_date)
    sql = select + order_by
    try:
        conn = _get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params)
        return cursor.fetchall()
    except mysql.connector.Error as exc:
        logger.warning("Could not fetch receipt candidates by date: %s", exc)
        return []


def get_receipt_candidates(amount: Decimal, bank_date: date) -> list[dict]:
    """Backwards-compatible wrapper around get_receipt_candidates_by_date
    that adds the exact-amount predicate using the configured window."""
    return get_receipt_candidates_by_date(
        bank_date, config.RECEIPT_DATE_WINDOW_DAYS, amount
    )


def get_regpayment_candidates_by_date(
    bank_date: date, signed_cents: int | None = None
) -> list[dict]:
    """
    Return regpayment rows active on bank_date for the configured user,
    with an optional exact-amount filter. Used both for amount-matching
    (Pass A Step 1, with ``signed_cents`` set) and the name-only fallback /
    amount-mismatch detection (Pass A Step 3, without).
    """
    sql = (
        "SELECT id, amount, reason, frequency, startDate, endDate "
        "FROM regpayment "
        "WHERE startDate <= %s "
        "AND (endDate IS NULL OR endDate >= %s) "
        "AND reason IS NOT NULL AND reason <> '' "
        "AND user = %s"
    )
    if signed_cents is not None:
        sql += " AND amount = %s"
        params: tuple = (bank_date, bank_date, config.REGPAYMENT_USER_ID, signed_cents)
    else:
        params = (bank_date, bank_date, config.REGPAYMENT_USER_ID)
    try:
        conn = _get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params)
        return cursor.fetchall()
    except mysql.connector.Error as exc:
        logger.warning("Could not fetch regpayment candidates by date: %s", exc)
        return []


def get_regpayment_candidates(signed_cents: int, bank_date: date) -> list[dict]:
    """Backwards-compatible wrapper around get_regpayment_candidates_by_date
    that adds the exact-amount predicate."""
    return get_regpayment_candidates_by_date(bank_date, signed_cents)
