"""
tests/test_db_client_queries.py — unit tests for storage/db_client.py SQL
query construction.

These tests mock the DB connection (mysql.connector.connect) so they run
without a real database. They assert that the SQL query string and the
bound parameters match what the H4 fix requires (a lower bound on
receipt_date) and that existing behaviour (upper bound, ORDER BY) is
preserved.

Run:  python -m pytest tests/test_db_client_queries.py -v
"""

import importlib.util
import os
import sys
import types
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import-time mock of config — must happen before db_client is loaded.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

mock_config = types.ModuleType("config")
mock_config.DB_HOST = "localhost"
mock_config.DB_PORT = 3306
mock_config.DB_NAME = "test"
mock_config.DB_USER = "test"
mock_config.DB_PASSWORD = "test"
mock_config.RECEIPT_DATE_WINDOW_DAYS = 28
mock_config.REGPAYMENT_USER_ID = 1
sys.modules["config"] = mock_config

# Load db_client from its source file under a unique module name so that
# sibling test files that replace sys.modules["storage.db_client"] with a
# MagicMock (e.g. test_matcher_branches.py) do not interfere.
_db_src = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "storage", "db_client.py",
)
_spec = importlib.util.spec_from_file_location("kontocheck_db_client_under_test", _db_src)
db_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db_client)


def _make_mock_connection():
    """Return (conn, cursor) mocks. Cursor records executed (sql, params)."""
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.is_connected.return_value = True
    return conn, cursor


# ═══════════════════════════════════════════════════════════════════════════
# get_receipt_candidates — SQL query shape (H4)
# ═══════════════════════════════════════════════════════════════════════════

class TestGetReceiptCandidatesQuery:
    """Verify the SQL query built by get_receipt_candidates includes the
    H4 lower bound on receipt_date and the existing upper bound / ORDER BY.
    """

    def test_query_includes_lower_bound_predicate(self):
        """The query string must contain a DATE_SUB-based lower bound."""
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates(Decimal("43.20"), date(2024, 4, 15))

        sql = cursor.execute.call_args[0][0]
        assert "receipt_date" in sql.lower()
        assert "date_sub" in sql.lower(), (
            "H4: expected a DATE_SUB lower bound on receipt_date in the query; "
            f"got: {sql!r}"
        )

    def test_query_includes_upper_bound_predicate(self):
        """The existing receipt_date <= bank_date predicate must remain."""
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates(Decimal("43.20"), date(2024, 4, 15))

        sql = cursor.execute.call_args[0][0]
        assert "receipt_date <= %s" in sql.lower() or "receipt_date <=%s" in sql.lower()

    def test_query_includes_order_by_date_desc(self):
        """ORDER BY receipt_date DESC must be preserved."""
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates(Decimal("43.20"), date(2024, 4, 15))

        sql = cursor.execute.call_args[0][0]
        assert "order by receipt_date desc" in sql.lower()

    def test_params_include_amount_bank_date_and_window(self):
        """The bound parameters must include (amount, bank_date, window_days, bank_date)
        or a shape consistent with the DATE_SUB call."""
        conn, cursor = _make_mock_connection()
        bank_date = date(2024, 4, 15)
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates(Decimal("43.20"), bank_date)

        params = cursor.execute.call_args[0][1]
        assert Decimal("43.20") in params or "43.20" in {str(p) for p in params}
        assert bank_date in params
        assert mock_config.RECEIPT_DATE_WINDOW_DAYS in params

    def test_window_days_drives_lower_bound(self):
        """The lower bound must be bank_date - RECEIPT_DATE_WINDOW_DAYS.
        With bank_date=2024-04-15 and window=28, lower bound = 2024-03-18.
        """
        conn, cursor = _make_mock_connection()
        bank_date = date(2024, 4, 15)
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates(Decimal("43.20"), bank_date)

        sql = cursor.execute.call_args[0][0]
        # The lower bound predicate must reference receipt_date and use %s
        # for both the bank_date and the window days.
        assert "receipt_date >=" in sql.lower(), (
            "H4: query must include a `receipt_date >= ...` lower bound"
        )


# ═══════════════════════════════════════════════════════════════════════════
# L12 — empty issuer/reason candidates must be filtered at the SQL layer
# ═══════════════════════════════════════════════════════════════════════════

class TestReceiptQueryFiltersEmptyIssuer:
    """L12: receipts query must exclude rows where issuer is NULL or empty."""

    def test_query_includes_issuer_not_null_predicate(self):
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates(Decimal("43.20"), date(2024, 4, 15))

        sql = cursor.execute.call_args[0][0].lower()
        assert "issuer is not null" in sql, (
            "L12: receipts query must include `issuer IS NOT NULL`"
        )

    def test_query_includes_issuer_nonempty_predicate(self):
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates(Decimal("43.20"), date(2024, 4, 15))

        sql = cursor.execute.call_args[0][0].lower()
        assert "issuer <>" in sql or "issuer !=" in sql, (
            "L12: receipts query must include `issuer <> ''`"
        )


class TestRegpaymentQueryFiltersEmptyReason:
    """L12: regpayment queries must exclude rows where reason is NULL or empty."""

    def test_amount_query_includes_reason_not_null(self):
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_regpayment_candidates(-1099, date(2024, 4, 15))

        sql = cursor.execute.call_args[0][0].lower()
        assert "reason is not null" in sql, (
            "L12: get_regpayment_candidates query must include `reason IS NOT NULL`"
        )

    def test_amount_query_includes_reason_nonempty(self):
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_regpayment_candidates(-1099, date(2024, 4, 15))

        sql = cursor.execute.call_args[0][0].lower()
        assert "reason <>" in sql or "reason !=" in sql, (
            "L12: get_regpayment_candidates query must include `reason <> ''`"
        )

    def test_date_query_includes_reason_not_null(self):
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_regpayment_candidates_by_date(date(2024, 4, 15))

        sql = cursor.execute.call_args[0][0].lower()
        assert "reason is not null" in sql, (
            "L12: get_regpayment_candidates_by_date query must include `reason IS NOT NULL`"
        )

    def test_date_query_includes_reason_nonempty(self):
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_regpayment_candidates_by_date(date(2024, 4, 15))

        sql = cursor.execute.call_args[0][0].lower()
        assert "reason <>" in sql or "reason !=" in sql, (
            "L12: get_regpayment_candidates_by_date query must include `reason <> ''`"
        )


# ═══════════════════════════════════════════════════════════════════════════
# get_receipt_candidates_by_date — SQL query shape (name-only fallback)
# ═══════════════════════════════════════════════════════════════════════════

class TestGetReceiptCandidatesByDateQuery:
    """Verify the SQL query built by get_receipt_candidates_by_date.

    This function is used by the name-only fallback in Pass A Step 3 of
    the redesigned matcher. It must:
      - NOT filter by total_amount (no amount predicate)
      - bound receipt_date both ways (<= bank_date, >= DATE_SUB)
      - filter empty issuers (L12)
      - order by date proximity (ABS(DATEDIFF)) then receipt_date DESC
    """

    def test_query_has_no_amount_predicate(self):
        """The query must NOT contain a total_amount = %s WHERE predicate.
        (total_amount appears in the SELECT column list, but must not be
        in the WHERE clause.)"""
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates_by_date(date(2024, 4, 15), 28)

        sql = cursor.execute.call_args[0][0].lower()
        where_clause = sql.split("where")[1] if "where" in sql else ""
        assert "total_amount" not in where_clause, (
            "get_receipt_candidates_by_date must not filter by total_amount in WHERE"
        )

    def test_query_includes_upper_bound(self):
        """receipt_date <= bank_date must be present."""
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates_by_date(date(2024, 4, 15), 28)

        sql = cursor.execute.call_args[0][0].lower()
        assert "receipt_date <= %s" in sql or "receipt_date <=%s" in sql

    def test_query_includes_lower_bound(self):
        """DATE_SUB lower bound must be present."""
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates_by_date(date(2024, 4, 15), 28)

        sql = cursor.execute.call_args[0][0].lower()
        assert "date_sub" in sql, (
            "expected a DATE_SUB lower bound on receipt_date in the query"
        )

    def test_query_includes_proximity_order_by(self):
        """ORDER BY ABS(DATEDIFF(...)) must be present."""
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates_by_date(date(2024, 4, 15), 28)

        sql = cursor.execute.call_args[0][0].lower()
        assert "abs(datediff" in sql, (
            "expected ORDER BY ABS(DATEDIFF(receipt_date, bank_date)) in the query"
        )

    def test_query_includes_issuer_not_null(self):
        """L12: issuer IS NOT NULL must be present."""
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates_by_date(date(2024, 4, 15), 28)

        sql = cursor.execute.call_args[0][0].lower()
        assert "issuer is not null" in sql

    def test_query_includes_issuer_nonempty(self):
        """L12: issuer <> '' must be present."""
        conn, cursor = _make_mock_connection()
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates_by_date(date(2024, 4, 15), 28)

        sql = cursor.execute.call_args[0][0].lower()
        assert "issuer <>" in sql or "issuer !=" in sql

    def test_params_include_bank_date_and_window(self):
        """Bound parameters must include bank_date and window_days."""
        conn, cursor = _make_mock_connection()
        bank_date = date(2024, 4, 15)
        with patch.object(db_client, "_get_connection", return_value=conn):
            db_client.get_receipt_candidates_by_date(bank_date, 28)

        params = cursor.execute.call_args[0][1]
        assert bank_date in params
        assert 28 in params

    def test_returns_rows_from_fetchall(self):
        """The function should return the rows from cursor.fetchall()."""
        cursor = MagicMock()
        cursor.fetchall.return_value = [{"id": 1, "issuer": "REWE"}]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        conn.is_connected.return_value = True
        with patch.object(db_client, "_get_connection", return_value=conn):
            rows = db_client.get_receipt_candidates_by_date(date(2024, 4, 15), 28)
        assert rows == [{"id": 1, "issuer": "REWE"}]

    def test_db_error_returns_empty_list(self):
        """On mysql.connector.Error, return an empty list (not raise)."""
        import mysql.connector
        conn = MagicMock()
        conn.cursor.side_effect = mysql.connector.Error("connection lost")
        with patch.object(db_client, "_get_connection", return_value=conn):
            rows = db_client.get_receipt_candidates_by_date(date(2024, 4, 15), 28)
        assert rows == []
