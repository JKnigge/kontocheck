import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import ollama
import pdfplumber

import config

logger = logging.getLogger(__name__)


@dataclass
class Transaction:
    date: date
    description: str
    amount: Decimal
    direction: str
    raw_text: str


_SYSTEM_PROMPT = (
    "You are a bank statement parser. "
    "You receive the raw text of a bank statement and extract every transaction into a JSON array. "
    "Output ONLY the JSON array — no markdown, no explanation, no code fences. "
    "Skip header rows, balance lines, and non-transaction text."
)

_USER_PROMPT_TEMPLATE = (
    "Extract all transactions from this bank statement text.\n\n"
    "Each transaction must be a JSON object with these fields:\n"
    '- "date": booking date in YYYY-MM-DD format\n'
    '- "description": the description/payee text as shown on the statement\n'
    '- "amount": the transaction amount as a positive decimal string in euros (e.g. "43.20")\n'
    '- "direction": "debit" if money left the account, "credit" if money entered the account\n\n'
    "Raw statement text:\n\n{text}"
)

_RETRY_PROMPT_TEMPLATE = (
    "The previous output could not be parsed as a valid JSON array. "
    "Please try again. Output ONLY the JSON array with no extra text.\n\n"
    "Raw statement text:\n\n{text}"
)


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTAIL).strip()


def _call_llm(prompt: str, system: str, num_predict: int) -> str:
    client = ollama.Client(host=config.OLLAMA_URL)
    response = client.chat(
        model=config.OLLAMA_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        options={
            "temperature": 0.0,
            "num_predict": num_predict,
        },
        think=False,
    )
    content = response.message.content or ""
    return _strip_thinking(content)


def _parse_transaction_list(raw_json: str) -> list[dict]:
    match = re.search(r"\[.*\]", raw_json, flags=re.DOTAIL)
    if not match:
        raise ValueError("No JSON array found in LLM output")
    return json.loads(match.group())


def _validate_transaction(obj: dict, raw_text: str) -> Transaction | None:
    try:
        tx_date = date.fromisoformat(obj["date"])
    except (KeyError, ValueError, TypeError):
        logger.warning("Skipping transaction with invalid date: %s", obj)
        return None

    try:
        amount = Decimal(str(obj["amount"]))
    except (KeyError, InvalidOperation, TypeError):
        logger.warning("Skipping transaction with invalid amount: %s", obj)
        return None

    if amount <= 0:
        logger.warning("Skipping transaction with non-positive amount: %s", obj)
        return None

    direction = obj.get("direction", "").lower()
    if direction not in ("debit", "credit"):
        logger.warning("Skipping transaction with invalid direction: %s", obj)
        return None

    description = str(obj.get("description", "")).strip()
    if not description:
        logger.warning("Skipping transaction with empty description: %s", obj)
        return None

    return Transaction(
        date=tx_date,
        description=description,
        amount=amount,
        direction=direction,
        raw_text=raw_text,
    )


def extract_text(pdf_path: Path) -> str:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = [page.extract_text() for page in pdf.pages]
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF '{pdf_path}': {e}") from e

    raw_text = "\n\n".join(filter(None, pages))
    if not raw_text.strip():
        raise RuntimeError(f"PDF produced empty text — is '{pdf_path}' a text-based PDF?")
    return raw_text


def parse_transactions(raw_text: str) -> list[Transaction]:
    prompt = _USER_PROMPT_TEMPLATE.format(text=raw_text)
    raw_json = _call_llm(prompt, _SYSTEM_PROMPT, num_predict=2000)

    try:
        tx_list = _parse_transaction_list(raw_json)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Initial LLM parse failed: %s — retrying once", e)
        retry_prompt = _RETRY_PROMPT_TEMPLATE.format(text=raw_text)
        raw_json = _call_llm(retry_prompt, _SYSTEM_PROMPT, num_predict=2000)
        try:
            tx_list = _parse_transaction_list(raw_json)
        except (json.JSONDecodeError, ValueError) as e2:
            raise RuntimeError(f"LLM extraction failed after retry: {e2}") from e2

    transactions = []
    for obj in tx_list:
        tx = _validate_transaction(obj, raw_text)
        if tx is not None:
            transactions.append(tx)

    if not transactions:
        raise RuntimeError("LLM returned no valid transactions")

    logger.info("Extracted %d transactions", len(transactions))
    return transactions
