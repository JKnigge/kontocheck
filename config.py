import sys
from pathlib import Path

from dotenv import load_dotenv
import os


def _require(key: str) -> str:
    value = os.getenv(key)
    if value is None:
        print(f"ERROR: Required environment variable {key} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def _int(key: str) -> int:
    value = _require(key)
    try:
        return int(value)
    except ValueError:
        print(f"ERROR: Environment variable {key} must be an integer, got: {value}", file=sys.stderr)
        sys.exit(1)


load_dotenv(Path(__file__).resolve().parent / ".env")

DB_HOST: str = _require("DB_HOST")
DB_PORT: int = _int("DB_PORT")
DB_NAME: str = _require("DB_NAME")
DB_USER: str = _require("DB_USER")
DB_PASSWORD: str = _require("DB_PASSWORD")

OLLAMA_URL: str = _require("OLLAMA_URL")
OLLAMA_MODEL: str = _require("OLLAMA_MODEL")

OUTPUT_FOLDER: Path = Path(os.getenv("OUTPUT_FOLDER", "kontocheck_reports"))

DATE_TIER1_DAYS: int = _int("DATE_TIER1_DAYS") if os.getenv("DATE_TIER1_DAYS") else 5
DATE_TIER2_DAYS: int = _int("DATE_TIER2_DAYS") if os.getenv("DATE_TIER2_DAYS") else 14

REGPAYMENT_USER_ID: int = _int("REGPAYMENT_USER_ID")


def print_config() -> None:
    print("kontocheck configuration:")
    print(f"  DB_HOST           = {DB_HOST}")
    print(f"  DB_PORT           = {DB_PORT}")
    print(f"  DB_NAME           = {DB_NAME}")
    print(f"  DB_USER           = {DB_USER}")
    print(f"  DB_PASSWORD       = {'*' * len(DB_PASSWORD)}")
    print(f"  OLLAMA_URL        = {OLLAMA_URL}")
    print(f"  OLLAMA_MODEL      = {OLLAMA_MODEL}")
    print(f"  OUTPUT_FOLDER     = {OUTPUT_FOLDER}")
    print(f"  DATE_TIER1_DAYS   = {DATE_TIER1_DAYS}")
    print(f"  DATE_TIER2_DAYS   = {DATE_TIER2_DAYS}")
    print(f"  REGPAYMENT_USER_ID= {REGPAYMENT_USER_ID}")


def ensure_folders() -> None:
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
