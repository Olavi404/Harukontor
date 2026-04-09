from contextlib import asynccontextmanager
from decimal import Decimal
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import services
from app.auth import create_user_token, generate_api_key, get_current_user_id
from app.central_bank import (
    build_interbank_jwt,
    ensure_keys,
    get_exchange_rates,
    get_local_bank_id,
    post_interbank_transfer,
    refresh_banks_cache,
    register_bank,
    resolve_destination_bank_from_cache,
    safe_refresh,
    verify_interbank_jwt,
)
from app.config import get_settings
from app.database import Base, engine, get_db, SessionLocal
from app.models import Account, Transfer, User
from app.schemas import (
    AccountCreationRequest,
    AccountCreationResponse,
    AccountLookupResponse,
    ErrorOut,
    InterBankTransferRequest,
    InterBankTransferResponse,
    TransferRequest,
    TransferResponse,
    TransferStatusResponse,
    UserRegistrationRequest,
    UserRegistrationResponse,
    to_money_str,
    to_rate_str,
)


settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_keys()
    db = SessionLocal()
    try:
        if not settings.bank_registration_id:
            try:
                await register_bank(db)
            except Exception:
                pass
        await safe_refresh(db)
    finally:
        db.close()
    yield


app = FastAPI(title="Branch Bank API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "code" in exc.detail and "message" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"code": "ERROR", "message": str(exc.detail)})


@app.post("/api/v1/users", status_code=201, response_model=UserRegistrationResponse, responses={400: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def register_user(payload: UserRegistrationRequest, db: Session = Depends(get_db)):
    if payload.email:
        existing = db.query(User).filter(User.email == payload.email).first()
        if existing:
            raise HTTPException(status_code=409, detail={"code": "DUPLICATE_USER", "message": "A user with this email address is already registered"})
    user_id = f"user-{uuid.uuid4()}"
    api_key = generate_api_key()
    user = User(id=user_id, full_name=payload.fullName, email=payload.email, api_key=api_key)
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_user_token(user.id)
    return UserRegistrationResponse(userId=user.id, fullName=user.full_name, email=user.email, createdAt=user.created_at, authToken=token, apiKey=api_key)


@app.post("/api/v1/users/{userId}/accounts", status_code=201, response_model=AccountCreationResponse, responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}, 404: {"model": ErrorOut}})
def create_account(userId: str, payload: AccountCreationRequest, current_user_id: str = Depends(get_current_user_id), db: Session = Depends(get_db)):
    if userId != current_user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "You can only access your own resources"})
    services.ensure_user_exists(db, userId)
    currency = payload.currency.upper()
    if currency not in services.supported_currency_set():
        raise HTTPException(status_code=400, detail={"code": "UNSUPPORTED_CURRENCY", "message": f"Currency '{currency}' is not supported by this bank"})
    account_number = services.create_unique_account_number(db, settings.bank_prefix)
    account = Account(account_number=account_number, owner_id=userId, currency=currency, balance=Decimal("0.00"))
    db.add(account)
    db.commit()
    db.refresh(account)
    return AccountCreationResponse(accountNumber=account.account_number, ownerId=account.owner_id, currency=account.currency, balance=f"{account.balance:.2f}", createdAt=account.created_at)


@app.get("/api/v1/accounts/{accountNumber}", response_model=AccountLookupResponse, responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}})
def lookup_account(accountNumber: str, db: Session = Depends(get_db)):
    account = db.get(Account, accountNumber.upper())
    if not account:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND", "message": f"Account with number '{accountNumber}' not found"})
    user = services.ensure_user_exists(db, account.owner_id)
    return AccountLookupResponse(accountNumber=account.account_number, ownerName=user.full_name, currency=account.currency)


@app.post("/api/v1/transfers", status_code=201, response_model=TransferResponse, responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}, 404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ErrorOut}, 503: {"model": ErrorOut}})
async def initiate_transfer(payload: TransferRequest, current_user_id: str = Depends(get_current_user_id), db: Session = Depends(get_db)):
    source_acc_num = payload.sourceAccount.upper()
    destination_acc_num = payload.destinationAccount.upper()
    amount = services.parse_amount(payload.amount)

    source = services.ensure_account_exists(db, source_acc_num)
    if source.owner_id != current_user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Source account does not belong to authenticated user"})

    services.ensure_transfer_not_duplicate(db, payload.transferId)

    source_prefix = source_acc_num[:3]
    destination_prefix = destination_acc_num[:3]

    if source_prefix != settings.bank_prefix.upper():
        raise HTTPException(status_code=400, detail={"code": "INVALID_REQUEST", "message": "Source account does not belong to this bank"})

    if destination_prefix == settings.bank_prefix.upper():
        destination = services.ensure_account_exists(db, destination_acc_num)
        transfer = services.debit_credit_same_bank(db, payload.transferId, source, destination, amount)
        db.commit()
        db.refresh(transfer)
        return TransferResponse(
            transferId=transfer.transfer_id,
            status=transfer.status,
            sourceAccount=transfer.source_account,
            destinationAccount=transfer.destination_account,
            amount=f"{transfer.amount:.2f}",
            timestamp=transfer.timestamp,
        )

    destination_bank = resolve_destination_bank_from_cache(db, destination_prefix)
    if destination_bank is None:
        try:
            await refresh_banks_cache(db)
            destination_bank = resolve_destination_bank_from_cache(db, destination_prefix)
        except Exception:
            pass
    if destination_bank is None:
        raise HTTPException(status_code=503, detail={"code": "CENTRAL_BANK_UNAVAILABLE", "message": "Central bank is temporarily unavailable. Using cached directory data for routing."})

    source_bank_id = get_local_bank_id(db)
    destination_bank_id = destination_bank.bank_id

    converted_amount = amount
    exchange_rate = None
    rate_captured_at = None

    if source.currency != "EUR":
        try:
            rates, rate_ts, base = await get_exchange_rates()
            if base != "EUR":
                raise HTTPException(status_code=503, detail={"code": "CENTRAL_BANK_UNAVAILABLE", "message": "Unsupported base currency from central bank"})
            if source.currency not in rates:
                raise HTTPException(status_code=400, detail={"code": "INVALID_REQUEST", "message": f"Missing exchange rate for {source.currency}"})
            source_to_eur = Decimal("1") / rates[source.currency]
            converted_amount = services.q2(amount * source_to_eur)
            exchange_rate = source_to_eur.quantize(Decimal("0.000001"))
            rate_captured_at = rate_ts
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=503, detail={"code": "CENTRAL_BANK_UNAVAILABLE", "message": "Central bank is temporarily unavailable. Using cached directory data for routing."})

    transfer = services.mark_pending_transfer(db, payload.transferId, source, destination_acc_num, amount, destination_bank_id=destination_bank_id)
    db.commit()

    jwt_token = build_interbank_jwt(payload.transferId, source_acc_num, destination_acc_num, converted_amount, source_bank_id, destination_bank_id)

    try:
        await post_interbank_transfer(destination_bank.address, jwt_token)
        services.finalize_completed_transfer(transfer, converted_amount=converted_amount if converted_amount != amount else None, exchange_rate=exchange_rate, rate_ts=rate_captured_at)
        db.commit()
        db.refresh(transfer)
    except Exception:
        raise HTTPException(status_code=503, detail={"code": "DESTINATION_BANK_UNAVAILABLE", "message": "Destination bank is temporarily unavailable. Transfer has been queued for retry."})

    return TransferResponse(
        transferId=transfer.transfer_id,
        status=transfer.status,
        sourceAccount=transfer.source_account,
        destinationAccount=transfer.destination_account,
        amount=f"{transfer.amount:.2f}",
        convertedAmount=to_money_str(transfer.converted_amount),
        exchangeRate=to_rate_str(transfer.exchange_rate),
        rateCapturedAt=transfer.rate_captured_at,
        timestamp=transfer.timestamp,
        errorMessage=transfer.error_message,
    )


@app.post("/api/v1/transfers/receive", response_model=InterBankTransferResponse, responses={401: {"model": ErrorOut}, 403: {"model": ErrorOut}, 404: {"model": ErrorOut}})
def receive_interbank_transfer(payload: InterBankTransferRequest, db: Session = Depends(get_db)):
    claims = verify_interbank_jwt(db, payload.jwt)
    destination_account = claims["destinationAccount"].upper()
    amount = services.parse_amount(claims["amount"])
    transfer_id = claims["transferId"]

    destination = services.ensure_account_exists(db, destination_account)

    existing = db.get(Transfer, transfer_id)
    if existing and existing.status == "completed":
        return InterBankTransferResponse(transferId=existing.transfer_id, status=existing.status, destinationAccount=existing.destination_account, amount=f"{existing.amount:.2f}", timestamp=existing.timestamp)

    destination.balance = services.q2(destination.balance + amount)
    if existing is None:
        transfer = Transfer(
            transfer_id=transfer_id,
            status="completed",
            source_account=claims["sourceAccount"],
            destination_account=destination_account,
            amount=amount,
            timestamp=services.now_utc(),
            source_bank_id=claims.get("sourceBankId"),
            destination_bank_id=claims.get("destinationBankId"),
        )
        db.add(transfer)
    else:
        transfer = existing
        transfer.status = "completed"
        transfer.timestamp = services.now_utc()
    db.commit()

    db.refresh(transfer)
    return InterBankTransferResponse(transferId=transfer.transfer_id, status=transfer.status, destinationAccount=transfer.destination_account, amount=f"{transfer.amount:.2f}", timestamp=transfer.timestamp)


@app.get("/api/v1/transfers/{transferId}", response_model=TransferStatusResponse, responses={401: {"model": ErrorOut}, 404: {"model": ErrorOut}, 423: {"model": ErrorOut}})
def get_transfer_status(transferId: str, current_user_id: str = Depends(get_current_user_id), db: Session = Depends(get_db)):
    transfer = db.get(Transfer, transferId)
    if not transfer:
        raise HTTPException(status_code=404, detail={"code": "TRANSFER_NOT_FOUND", "message": f"Transfer with ID '{transferId}' not found"})

    # Access is granted if user owns source account or destination account.
    src = db.get(Account, transfer.source_account)
    dst = db.get(Account, transfer.destination_account)
    owns_src = src is not None and src.owner_id == current_user_id
    owns_dst = dst is not None and dst.owner_id == current_user_id
    if not (owns_src or owns_dst):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "You do not have access to this transfer"})

    if transfer.status == "failed_timeout":
        raise HTTPException(status_code=423, detail={"code": "TRANSFER_TIMEOUT", "message": "Transfer has timed out and cannot be modified or retried. Status is failed_timeout with refund processed."})

    return TransferStatusResponse(
        transferId=transfer.transfer_id,
        status=transfer.status,
        sourceAccount=transfer.source_account,
        destinationAccount=transfer.destination_account,
        amount=f"{transfer.amount:.2f}",
        convertedAmount=to_money_str(transfer.converted_amount),
        exchangeRate=to_rate_str(transfer.exchange_rate),
        rateCapturedAt=transfer.rate_captured_at,
        timestamp=transfer.timestamp,
        pendingSince=transfer.pending_since,
        nextRetryAt=transfer.next_retry_at,
        retryCount=transfer.retry_count,
        errorMessage=transfer.error_message,
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": settings.app_name}
