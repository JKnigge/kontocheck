import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xxlimited_35 import Null

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
    "- Each transaction begins on a line that starts with a date (DD.MM.YYYY or YYYY-MM-DD). "
    "The date may be immediately followed by the type label with no space in between "
    "(e.g. '10.09.2025Basislastschrift'). Every subsequent line that does NOT start with a date "
    "is a continuation line and belongs to the same transaction — it carries the payee name "
    "and reference information.\n"
    "- Emit each transaction exactly once. A transaction spanning four lines is still ONE entry, "
    "not four. Never output the same date+amount combination twice.\n"
    "- Skip non-transaction lines: account headers, IBAN/BIC lines, opening/closing balances, "
    "page totals, column headings, and anything before the first dated line or after the last.\n"
    "- Always output dates as ISO YYYY-MM-DD.\n"
    "- Amounts use a period as the decimal separator (e.g. 43.20). "
    "Always output amounts as positive JSON numbers.\n"
    "- Determine direction from context: a '+' sign, the word 'Gutschrift'/'Haben', or a value in "
    "a credit column means 'credit'; a '-' sign, 'Lastschrift'/'Soll', or a value in a debit column "
    "means 'debit'.\n"
    "- The description must concatenate the header line AND all of its continuation lines with single "
    "spaces. It MUST contain the payee/merchant name (e.g. 'Gemeinde Berlin', 'EDEKA', 'ED.TANKSTELLE'). "
    "A description consisting only of a generic type label like 'Basislastschrift', 'Kartenzahlung', "
    "'Überweisung' or 'Lastschrift' is incomplete and wrong — you forgot the continuation lines.\n"
    "- Do not confuse running balance figures with transaction amounts.\n"
    "- If a field cannot be determined, omit the transaction rather than guessing."
)

_USER_PROMPT_TEMPLATE = (
    "Extract all transactions from this bank statement text.\n\n"
    "Each transaction must be a JSON object with exactly these fields:\n"
    '"date": booking date in YYYY-MM-DD format (convert DD.MM.YYYY if necessary)\n'
    '"description": the header line PLUS every following line up to (but not including) the next '
    "line that starts with a date, all joined into one string with spaces. Must include the "
    "payee/merchant name. A description consisting only of a generic term like 'Basislastschrift', "
    "'Kartenzahlung', 'Überweisung' or 'Abrechnung' is WRONG — you missed the continuation lines.\n"
    '"amount": the transaction amount as a positive JSON number with a period as decimal separator '
    "(e.g. 43.20, never 43,20)\n"
    '"direction": "debit" if money left the account, "credit" if money entered the account\n\n'
    "Grouping rule (critical):\n"
    "- A line beginning with a date OPENS a new transaction.\n"
    "- Every following line that does NOT begin with a date belongs to that same transaction.\n"
    "- The next line beginning with a date CLOSES the current transaction and opens the next one.\n"
    "- Emit each transaction exactly once. Never duplicate an entry just because its description "
    "spans several lines.\n\n"
    "Example 1 — three transactions in plain-text format (note: date and type label may be glued "
    "together with no space, and continuation lines have no leading date):\n"
    "  10.09.2025Basislastschrift -471,00\n"
    "  Gemeinde Berlin 120435/620 KOSTENBEITRAG\n"
    "  381,00 EUR VERPFLEGUNG 80,00 EUR GETRAENKEGEL D\n"
    "  10,00 EUR -25-533-218 MR-23-0449 Gläubiger-ID:\n"
    "  DE70GEM00000033047\n"
    "  15.09.2025Kartenzahlung -66,81\n"
    "  ED.TANKSTELLE/HAMM/../DE 2025-09-13T18:50\n"
    "  Debitk.29 2099-12 Zahl.System VISA Debit\n"
    "  15.09.2025Kartenzahlung -55,23\n"
    "  EDEKA.GEORG.HAMM/HAMM/../DE 2025-09-13T20:54\n"
    "  Debitk.29 2099-12 Zahl.System VISA Debit\n"
    "must produce exactly THREE objects (not six, not nine):\n"
    '[\n'
    '  {{"date": "2025-09-10", "description": "Basislastschrift Gemeinde Berlin 120435/620 KOSTENBEITRAG 381,00 EUR VERPFLEGUNG 80,00 EUR GETRAENKEGEL D 10,00 EUR -25-533-218 MR-23-0449 Gläubiger-ID: DE70GEM00000033047", "amount": 471.00, "direction": "debit"}},\n'
    '  {{"date": "2025-09-15", "description": "Kartenzahlung ED.TANKSTELLE/HAMM/../DE 2025-09-13T18:50 Debitk.29 2099-12 Zahl.System VISA Debit", "amount": 66.81, "direction": "debit"}},\n'
    '  {{"date": "2025-09-15", "description": "Kartenzahlung EDEKA.GEORG.HAMM/HAMM/../DE 2025-09-13T20:54 Debitk.29 2099-12 Zahl.System VISA Debit", "amount": 55.23, "direction": "debit"}}\n'
    ']\n\n'
    "Example 2 — a credit entry already collapsed onto one pipe-delimited line:\n"
    "  03.03.2024 | Lohn, Gehalt, Rente Muster GmbH Lohn/Gehalt | 2500,00\n"
    "must produce:\n"
    '[\n'
    '  {{"date": "2024-03-03", "description": "Lohn, Gehalt, Rente Muster GmbH Lohn/Gehalt", "amount": 2500.00, "direction": "credit"}}\n'
    ']\n\n'
    "Counter-example — what NOT to do. Given:\n"
    "  15.09.2025Kartenzahlung -55,23\n"
    "  EDEKA.GEORG.HAMM/HAMM/../DE 2025-09-13T20:54\n"
    "do NOT output two entries, and do NOT output a description of just 'Kartenzahlung'. There is "
    "exactly ONE transaction here and its description must include 'EDEKA.GEORG.HAMM'.\n\n"
    "Raw statement text:\n\n{text}"
)

_RETRY_PROMPT_TEMPLATE = (
    "Your previous response could not be parsed as a valid JSON array. "
    "The error was: {error}\n\n"
    "Common mistakes to avoid:\n"
    "- Wrapping the array in markdown fences (```json ... ```) — output raw JSON only\n"
    "- Using strings instead of numbers for 'amount' (use 43.20 not \"43.20\")\n"
    "- Using a comma as decimal separator (use 43.20 not 43,20)\n"
    "- Capturing only the first line of a multi-line entry — join the header line AND every "
    "following non-dated line into description\n"
    "- Emitting the same transaction twice because its description spans several lines — each "
    "dated line corresponds to exactly ONE output object\n"
    "- Producing a description that contains only a type label like 'Basislastschrift' or "
    "'Kartenzahlung' — the payee name (on the next line) MUST be included\n"
    "- Including explanatory text before or after the array\n"
    "- Trailing commas after the last element\n\n"
    "Output ONLY the JSON array. Raw statement text:\n\n{text}"
)


def _normalize_table_rows(text: str) -> str:
    """Collapse multi-line table rows into single lines before sending to the LLM.

    Bank statements often spread one transaction across several rows, with the
    date and amount only on the first row and continuation lines containing the
    payee name or reference numbers.  pdfplumber extracts these as separate
    lines, so the LLM never sees them grouped together.

    This function detects pipe-delimited table blocks and merges continuation
    rows (rows whose first cell is blank) into the preceding data row, producing
    one line per logical transaction.  Non-table text is returned unchanged, so
    the function is safe to call on any statement layout.
    """
    lines = text.splitlines()
    result: list[str] = []
    pending: list[str] | None = None  # accumulated cells of the current logical row

    def _cells(line: str) -> list[str]:
        parts = line.split("|")
        # Trim the empty strings that appear before the first and after the last pipe
        return [p.strip() for p in parts[1:-1]]

    def _is_table_row(line: str) -> bool:
        return "|" in line and not re.match(r"^[|+\-=\s]+$", line)

    def _is_separator(line: str) -> bool:
        return bool(re.match(r"^[|+\-=\s]+$", line)) and "|" in line

    def _flush(cells: list[str]) -> str:
        return " | ".join(cells)

    for line in lines:
        if _is_separator(line):
            # Separator row (e.g. +---+---+) — flush pending logical row and discard
            if pending is not None:
                result.append(_flush(pending))
                pending = None
            continue

        if not _is_table_row(line):
            # Plain text line outside any table — flush pending row and pass through
            if pending is not None:
                result.append(_flush(pending))
                pending = None
            result.append(line)
            continue

        cells = _cells(line)
        if not cells:
            result.append(line)
            continue

        if cells[0]:
            # Non-empty first cell → start of a new logical transaction row
            if pending is not None:
                result.append(_flush(pending))
            pending = cells
        else:
            # Empty first cell → continuation row; merge into pending
            if pending is None:
                pending = cells
            else:
                for i, cell in enumerate(cells):
                    if cell:
                        if i < len(pending):
                            pending[i] = (pending[i] + " " + cell).strip()
                        else:
                            pending.append(cell)

    if pending is not None:
        result.append(_flush(pending))

    return "\n".join(result)


# BUG FIX 1: re.DOTAIL → re.DOTALL (typo caused <think> blocks to never be stripped)
def _strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# BUG FIX 2: Create the Ollama client once at module level (not inside every call)
_client = ollama.Client(host=config.OLLAMA_URL)


def _call_llm(prompt: str, system: str, num_predict: int) -> str:
    # BUG FIX 3: response is a dict — use subscript access, not attribute access.
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
    # BUG FIX 4: re.DOTAIL → re.DOTALL
    match = re.search(r"\[.*\]", raw_json, flags=re.DOTALL)
    if not match:
        raise ValueError("No JSON array found in LLM output")
    return json.loads(match.group())


def _validate_transaction(obj: dict, raw_text: str) -> Transaction | None:
    try:
        tx_date = _parse_date(obj["date"])
    except (KeyError, ValueError, TypeError):
        logger.warning("Skipping transaction with invalid date: %s", obj)
        return None

    try:
        # Normalise German decimal comma to period before parsing, as a safety
        # net in case the LLM ignores the prompt instruction despite best efforts.
        raw_amount = str(obj["amount"]).replace(",", ".")
        amount = Decimal(raw_amount)
    except (KeyError, InvalidOperation, TypeError):
        logger.warning("Skipping transaction with invalid amount: %s", obj)
        return None

    direction = obj.get("direction", "").lower()
    if direction not in ("debit", "credit"):
        logger.warning("Skipping transaction with invalid direction: %s", obj)

    if amount <= 0:
        logger.warning("Transaction with non-positive amount: %s", obj)
        if direction == "credit":
            logger.warning("Negative amount does not match direction: %s")
            return None
        direction = "debit"
        amount = (-1) * amount

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


def _parse_date(date_str):
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {date_str!r}")


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
    # Pre-process: collapse multi-line table rows into single lines so the LLM
    # always sees one complete logical transaction per line, regardless of layout.
    normalized_text = _normalize_table_rows(raw_text)

    prompt = _USER_PROMPT_TEMPLATE.format(text=normalized_text)
    raw_json = _call_llm(prompt, _SYSTEM_PROMPT, num_predict=2000)

    try:
        tx_list = _parse_transaction_list(raw_json)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Initial LLM parse failed: %s — retrying once", e)
        retry_prompt = _RETRY_PROMPT_TEMPLATE.format(error=e, text=normalized_text)
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
