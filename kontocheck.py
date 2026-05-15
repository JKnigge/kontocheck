"""
kontocheck.py — entry point for the bank statement reconciliation tool.

Reads a bank statement PDF, matches every transaction against the receipts
and regpayment tables in MariaDB, and writes a Markdown reconciliation
report to OUTPUT_FOLDER.

Usage:
    python kontocheck.py path/to/statement.pdf
    python kontocheck.py path/to/statement.pdf --log-level DEBUG
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# config is imported first because its module-level setup (UTF-8 console,
# .env loading) must run before any other module logs or prints anything.
import config
from pipeline.extractor import extract_text, parse_transactions
from pipeline.matcher import match_all
from reporting.report import generate
from storage import db_client

logger = logging.getLogger("kontocheck")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kontocheck",
        description=(
            "Reconcile a bank account statement PDF against known receipts "
            "and regular payments."
        ),
    )
    parser.add_argument(
        "pdf_path",
        type=Path,
        help="Path to the bank statement PDF.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args(argv)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(args.log_level)

    # Show effective configuration and ensure the output folder exists.
    config.print_config()
    config.ensure_folders()

    if not args.pdf_path.is_file():
        logger.error("PDF file not found: %s", args.pdf_path)
        return 1

    # Verify DB is reachable before doing any LLM work, so a misconfigured
    # database fails fast instead of after a long extraction step.
    if not db_client.test_connection():
        logger.error("Cannot reach the database — check DB_* values in .env")
        return 1

    try:
        logger.info("Extracting text from %s", args.pdf_path)
        raw_text = extract_text(args.pdf_path)

        logger.info("Parsing transactions with LLM")
        transactions = parse_transactions(raw_text)

        logger.info("Matching %d transactions against receipts and regpayment",
                    len(transactions))
        results = match_all(transactions)

        logger.info("Rendering report")
        report_path = generate(results, args.pdf_path)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except (RuntimeError, OSError) as exc:
        logger.error("kontocheck failed: %s", exc)
        return 1

    # Final user-facing line goes to stdout so it can be captured/piped
    # independently of the logger stream.
    print(f"\nReport written to: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
