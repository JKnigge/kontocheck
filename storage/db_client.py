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


def get_receipt_candidates(amount: Decimal, bank_date: date) -> list[dict]:
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    # H4: bound receipt_date both ways so stale receipts from unrelated
    # periods are filtered at the SQL layer. The lower bound is
    # bank_date - RECEIPT_DATE_WINDOW_DAYS (configurable, defaults to
    # 2 * DATE_TIER2_DAYS). The upper bound (bank_date) prevents future
    # receipts from matching; DATE_SUB is used so the bound is computed
    # by the DB engine and stays correct across DST/timezone edges.
    query = (
        "SELECT id, file_name, issuer, receipt_date, total_amount, "
        "confidence, manually_checked "
        "FROM receipts "
        "WHERE total_amount = %s "
        "AND receipt_date <= %s "
        "AND receipt_date >= DATE_SUB(%s, INTERVAL %s DAY) "
        "ORDER BY receipt_date DESC"
    )
    cursor.execute(
        query,
        (amount, bank_date, bank_date, config.RECEIPT_DATE_WINDOW_DAYS),
    )
    rows = cursor.fetchall()
    cursor.close()
    logger.debug(
        "Receipt candidates for amount=%s, bank_date=%s, window=%d days: %d rows",
        amount, bank_date, config.RECEIPT_DATE_WINDOW_DAYS, len(rows),
    )
    return rows


def get_regpayment_candidates(signed_cents: int, bank_date: date) -> list[dict]:
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    query = (
        "SELECT id, amount, reason, frequency, startDate, endDate "
        "FROM regpayment "
        "WHERE amount = %s "
        "AND startDate <= %s "
        "AND (endDate IS NULL OR endDate >= %s) "
        "AND user = %s"
    )
    cursor.execute(query, (signed_cents, bank_date, bank_date, config.REGPAYMENT_USER_ID))
    rows = cursor.fetchall()
    cursor.close()
    logger.debug("Regpayment candidates for signed_cents=%d, bank_date=%s: %d rows", signed_cents, bank_date, len(rows))
    return rows

def get_regpayment_candidates_by_date(bank_date: date) -> list[dict]:
    """
    Return all regpayment rows active on bank_date for the configured user,
    regardless of amount. Used for amount mismatch detection.
    """
    sql = """
        SELECT id, amount, reason, frequency, startDate, endDate
        FROM regpayment
        WHERE startDate <= %s
          AND (endDate IS NULL OR endDate >= %s)
          AND user = %s
    """
    try:
        conn = _get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, (bank_date, bank_date, config.REGPAYMENT_USER_ID))
        return cursor.fetchall()
    except mysql.connector.Error as exc:
        logger.warning("Could not fetch regpayment candidates by date: %s", exc)
        return []