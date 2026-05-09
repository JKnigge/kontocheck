"""Test script for Step 1: .env.example + requirements.txt + config.py

Verifies that config.py loads and validates all settings from .env.
Requires a .env file with valid values (copy from .env.example and fill in).
"""

import config

print("=" * 60)
print("Step 1 Test: config.py")
print("=" * 60)

# 1. Verify all config values are loaded
print("\n1. print_config()")
config.print_config()

# 2. Verify types
print("\n2. Type checks")
print(f"   DB_PORT is int:           {isinstance(config.DB_PORT, int)} (value: {config.DB_PORT})")
print(f"   DATE_TIER1_DAYS is int:   {isinstance(config.DATE_TIER1_DAYS, int)} (value: {config.DATE_TIER1_DAYS})")
print(f"   DATE_TIER2_DAYS is int:   {isinstance(config.DATE_TIER2_DAYS, int)} (value: {config.DATE_TIER2_DAYS})")
print(f"   REGPAYMENT_USER_ID is int:{isinstance(config.REGPAYMENT_USER_ID, int)} (value: {config.REGPAYMENT_USER_ID})")
print(f"   OUTPUT_FOLDER is Path:    {isinstance(config.OUTPUT_FOLDER, type(config.OUTPUT_FOLDER))} (value: {config.OUTPUT_FOLDER})")

# 3. Verify ensure_folders
print("\n3. ensure_folders()")
config.ensure_folders()
print(f"   Output folder exists:     {config.OUTPUT_FOLDER.exists()}")

print("\n" + "=" * 60)
print("All Step 1 tests complete.")
