from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account, Transfer, User


settings = get_settings()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_amount(amount: str) -> Decimal:
    try:
        v = Decimal(amount)
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_REQUEST", "message": "Invalid amount format"}) from exc
    if v <= 0:
        raise HTTPException(status_code=400, detail={"code": "INVALID_REQUEST", "message": "Amount must be greater than zero"})
    return q2(v)


def supported_currency_set() -> set[str]:
    return {c.strip().upper() for c in settings.supported_currencies.split(",") if c.strip()}


def ensure_user_exists(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": f"User with ID '{user_id}' not found"})
    return user


def ensure_account_exists(db: Session, account_number: str) -> Account:
    acc = db.get(Account, account_number)
    if acc is None:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND", "message": f"Account with number '{account_number}' not found"})
    return acc


def create_unique_account_number(db: Session, prefix: str) -> str:
    import random
    import string

    alphabet = string.ascii_uppercase + string.digits
    for _ in range(200):
        suffix = "".join(random.choice(alphabet) for _ in range(5))
        account = f"{prefix.upper()}{suffix}"
        if db.get(Account, account) is None:
            return account
    raise HTTPException(status_code=503, detail={"code": "INTERNAL_ERROR", "message": "Could not generate unique account number"})


def ensure_transfer_not_duplicate(db: Session, transfer_id: str) -> None:
    existing = db.get(Transfer, transfer_id)
    if existing is None:
        return
    if existing.status == "pending":
        raise HTTPException(status_code=409, detail={"code": "TRANSFER_ALREADY_PENDING", "message": f"Transfer with ID '{transfer_id}' is already pending. Cannot submit duplicate transfer."})
    raise HTTPException(status_code=409, detail={"code": "DUPLICATE_TRANSFER", "message": f"A transfer with ID '{transfer_id}' already exists"})


def debit_credit_same_bank(db: Session, transfer_id: str, source: Account, destination: Account, amount: Decimal) -> Transfer:
    if source.balance < amount:
        raise HTTPException(status_code=422, detail={"code": "INSUFFICIENT_FUNDS", "message": "Insufficient funds in source account"})
    source.balance = q2(source.balance - amount)
    destination.balance = q2(destination.balance + amount)
    transfer = Transfer(
        transfer_id=transfer_id,
        status="completed",
        source_account=source.account_number,
        destination_account=destination.account_number,
        amount=amount,
        timestamp=now_utc(),
    )
    db.add(transfer)
    return transfer


def mark_pending_transfer(db: Session, transfer_id: str, source: Account, destination_account: str, amount: Decimal, destination_bank_id: str | None = None) -> Transfer:
    if source.balance < amount:
        raise HTTPException(status_code=422, detail={"code": "INSUFFICIENT_FUNDS", "message": "Insufficient funds in source account"})
    source.balance = q2(source.balance - amount)
    ts = now_utc()
    transfer = Transfer(
        transfer_id=transfer_id,
        status="pending",
        source_account=source.account_number,
        destination_account=destination_account,
        amount=amount,
        timestamp=ts,
        pending_since=ts,
        next_retry_at=ts + timedelta(minutes=1),
        retry_count=0,
        destination_bank_id=destination_bank_id,
    )
    db.add(transfer)
    return transfer


def finalize_completed_transfer(transfer: Transfer, converted_amount: Decimal | None = None, exchange_rate: Decimal | None = None, rate_ts: datetime | None = None) -> None:
    transfer.status = "completed"
    transfer.converted_amount = converted_amount
    transfer.exchange_rate = exchange_rate
    transfer.rate_captured_at = rate_ts
    transfer.error_message = None
    transfer.next_retry_at = None


def mark_failed_transfer(transfer: Transfer, message: str) -> None:
    transfer.status = "failed"
    transfer.error_message = message
    transfer.next_retry_at = None


def mark_timeout_and_refund(db: Session, transfer: Transfer) -> None:
    source = ensure_account_exists(db, transfer.source_account)
    source.balance = q2(source.balance + transfer.amount)
    transfer.status = "failed_timeout"
    transfer.error_message = "Transfer timed out after 4 hours. Funds refunded to source account."
    transfer.next_retry_at = None


def set_next_retry(transfer: Transfer) -> None:
    transfer.retry_count += 1
    mins = min(2 ** transfer.retry_count, 60)
    transfer.next_retry_at = now_utc() + timedelta(minutes=mins)


def list_due_pending(db: Session) -> list[Transfer]:
    now = now_utc()
    stmt = select(Transfer).where(Transfer.status == "pending", Transfer.next_retry_at <= now)
    return list(db.execute(stmt).scalars().all())
