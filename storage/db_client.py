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
        cursor.close()
        logger.info("Database connection successful.")
        return True
    except mysql.connector.Error as e:
        logger.error("Database connection failed: %s", e)
        return False


def get_receipt_candidates(amount: Decimal, bank_date: date) -> list[dict]:
    conn = _get_connection()
    cursor = conn.cursor(dictionary=True)
    query = (
        "SELECT id, file_name, issuer, receipt_date, total_amount, "
        "confidence, manually_checked "
        "FROM receipts "
        "WHERE total_amount = %s "
        "AND receipt_date <= %s "
        "ORDER BY receipt_date DESC"
    )
    cursor.execute(query, (amount, bank_date))
    rows = cursor.fetchall()
    cursor.close()
    logger.debug("Receipt candidates for amount=%s, bank_date=%s: %d rows", amount, bank_date, len(rows))
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
