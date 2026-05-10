"""Test script for Step 3: pipeline/extractor.py

Tests PDF text extraction and LLM transaction parsing.
Requires a .env file with valid OLLAMA_URL and OLLAMA_MODEL.
Provide a real bank statement PDF as argument or set the path below.

Usage:
    python tests/test_step3_extractor.py path/to/statement.pdf
"""

import sys
import logging
from pathlib import Path

from pipeline.extractor import extract_text, parse_transactions, Transaction

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

print("=" * 60)
print("Step 3 Test: pipeline/extractor.py")
print("=" * 60)

# Resolve PDF path
if len(sys.argv) > 1:
    pdf_path = Path(sys.argv[1])
else:
    pdf_path = Path("statement.pdf")  # <-- change this default if needed

if not pdf_path.exists():
    print(f"\nERROR: PDF not found at '{pdf_path}'")
    print("Usage: python tests/test_step3_extractor.py path/to/statement.pdf")
    sys.exit(1)

# 1. Test PDF text extraction
print(f"\n1. extract_text('{pdf_path}')")
try:
    raw_text = extract_text(pdf_path)
    print(f"   Extracted {len(raw_text)} characters")
    print(f"   First 500 chars:\n{'-' * 40}")
    print(raw_text[:500])
    print(f"{'-' * 40}")
except RuntimeError as e:
    print(f"   FAILED: {e}")
    sys.exit(1)

# 2. Test LLM transaction parsing
print("\n2. parse_transactions(raw_text)")
try:
    transactions = parse_transactions(raw_text)
    print(f"   Extracted {len(transactions)} transactions\n")
    for i, tx in enumerate(transactions, 1):
        print(f"   {i:3d}. {tx.date}  {tx.direction:6s}  €{str(tx.amount):>10s}  {tx.description}")
except RuntimeError as e:
    print(f"   FAILED: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("All Step 3 tests complete.")
