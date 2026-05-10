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
    "You are a bank statement parser for German and European bank statements. "
    "You receive the raw text of a bank statement and extract every transaction into a JSON array. "
    "Output ONLY the JSON array — no markdown, no explanation, no code fences. "
    "\n\n"
    "Rules:\n"
    "- Skip non-transaction lines: account headers, opening/closing balances, page totals, column headings.\n"
    "- Dates may appear as DD.MM.YYYY (German) or YYYY-MM-DD (ISO) — always output ISO YYYY-MM-DD.\n"
    "- Amounts appear as positive numbers regardless of direction; determine direction from context "
    "(a '+' sign, the word 'Gutschrift'/'Haben', or a dedicated credit column means 'credit'; "
    "a '-' sign, 'Lastschrift'/'Soll', or a debit column means 'debit').\n"
    "- Do not confuse running balance figures with transaction amounts.\n"
    "- If a field cannot be determined, omit the transaction rather than guessing."
)

_USER_PROMPT_TEMPLATE = (
    "Extract all transactions from this bank statement text.\n\n"
    "Each transaction must be a JSON object with exactly these fields:\n"
    '- "date": booking date in YYYY-MM-DD format (convert DD.MM.YYYY if necessary)\n'
    '- "description": the full description or payee text exactly as it appears\n'
    '- "amount": the transaction amount as a positive JSON number, not a string (e.g. 43.20)\n'
    '- "direction": "debit" if money left the account, "credit" if money entered the account\n\n'
    "Example output (two transactions):\n"
    '[\n'
    '  {{"date": "2024-03-01", "description": "REWE Filiale Hamburg", "amount": 43.20, "direction": "debit"}},\n'
    '  {{"date": "2024-03-03", "description": "Gehalt Muster GmbH", "amount": 2500.00, "direction": "credit"}}\n'
    ']\n\n'
    "Raw statement text:\n\n{text}"
)

_RETRY_PROMPT_TEMPLATE = (
    "Your previous response could not be parsed as a valid JSON array. "
    "The error was: {error}\n\n"
    "Common mistakes to avoid:\n"
    "- Wrapping the array in markdown fences (```json ... ```) — output raw JSON only\n"
    "- Using strings instead of numbers for 'amount' (use 43.20 not \"43.20\")\n"
    "- Including explanatory text before or after the array\n"
    "- Trailing commas after the last element\n\n"
    "Output ONLY the JSON array. Raw statement text:\n\n{text}"
)


def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


_client = ollama.Client(host=config.OLLAMA_URL)


def _call_llm(prompt: str, system: str, num_predict: int) -> str:
    response = _client.chat(
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
    content = response["message"]["content"] or ""
    return _strip_thinking(content)


def _parse_transaction_list(raw_json: str) -> list[dict]:
    match = re.search(r"\[.*\]", raw_json, flags=re.DOTALL)
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
        retry_prompt = _RETRY_PROMPT_TEMPLATE.format(error=e, text=raw_text)
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
