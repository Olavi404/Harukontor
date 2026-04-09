from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
import logging
import re
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import or_
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
    AccountSummary,
    ErrorOut,
    InterBankTransferRequest,
    InterBankTransferResponse,
    TransferRequest,
    TransferResponse,
    TransferStatusResponse,
    TransfersListResponse,
    TokenExchangeRequest,
    TokenExchangeResponse,
    UserAccountsListResponse,
    UserProfileResponse,
    UserRegistrationRequest,
    UserRegistrationResponse,
    to_money_str,
    to_rate_str,
)


settings = get_settings()
logger = logging.getLogger("branch_bank")


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_utc_opt(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return as_utc(dt)


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


app = FastAPI(
    title="Branch Bank API",
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "System", "description": "Service metadata and health endpoints"},
        {"name": "Users", "description": "User registration and profile operations"},
        {"name": "Accounts", "description": "Account creation and lookup operations"},
        {"name": "Transfers", "description": "Fund transfer operations"},
    ],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "code" in exc.detail and "message" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"code": "ERROR", "message": str(exc.detail)})


@app.get("/", tags=["System"])
def root():
    return {
        "service": "Branch Bank API",
        "status": "ok",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
    }


@app.post(
    "/auth/token",
    response_model=TokenExchangeResponse,
    tags=["Users"],
    responses={401: {"model": ErrorOut}},
)
@app.post(
    "/api/v1/auth/token",
    include_in_schema=False,
    response_model=TokenExchangeResponse,
    responses={401: {"model": ErrorOut}},
)
def issue_token(
    payload: TokenExchangeRequest,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    api_key = payload.apiKey or x_api_key
    if not api_key:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Missing API key"})

    user = db.query(User).filter(User.api_key == api_key).first()
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid API key"})

    token = create_user_token(user.id)
    return TokenExchangeResponse(accessToken=token, tokenType="Bearer", expiresInSeconds=settings.user_jwt_ttl_minutes * 60)


@app.post(
    "/users",
    status_code=201,
    response_model=UserRegistrationResponse,
    tags=["Users"],
    responses={
        201: {
            "headers": {
                "X-API-Key": {
                    "description": "API key for authenticating protected user operations.",
                    "schema": {"type": "string"},
                }
            }
        },
        400: {"model": ErrorOut},
        409: {"model": ErrorOut},
    },
)
@app.post(
    "/api/v1/users",
    include_in_schema=False,
    status_code=201,
    response_model=UserRegistrationResponse,
    responses={
        201: {
            "headers": {
                "X-API-Key": {
                    "description": "API key for authenticating protected user operations.",
                    "schema": {"type": "string"},
                }
            }
        },
        400: {"model": ErrorOut},
        409: {"model": ErrorOut},
    },
)
def register_user(payload: UserRegistrationRequest, response: Response, db: Session = Depends(get_db)):
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
    response.headers["X-API-Key"] = api_key
    logger.info("user.registered user_id=%s email=%s", user.id, user.email or "")
    return UserRegistrationResponse(userId=user.id, fullName=user.full_name, email=user.email, createdAt=as_utc(user.created_at))


@app.get("/api/v1/users/{userId}", include_in_schema=False, response_model=UserProfileResponse, responses={401: {"model": ErrorOut}, 403: {"model": ErrorOut}, 404: {"model": ErrorOut}})
def get_user_profile(userId: str, current_user_id: str = Depends(get_current_user_id), db: Session = Depends(get_db)):
    if userId != current_user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "You can only access your own profile"})
    user = services.ensure_user_exists(db, userId)
    return UserProfileResponse(userId=user.id, fullName=user.full_name, email=user.email, createdAt=as_utc(user.created_at))


@app.get("/api/v1/users/{userId}/accounts", include_in_schema=False, response_model=UserAccountsListResponse, responses={401: {"model": ErrorOut}, 403: {"model": ErrorOut}, 404: {"model": ErrorOut}})
def list_user_accounts(userId: str, current_user_id: str = Depends(get_current_user_id), db: Session = Depends(get_db)):
    if userId != current_user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "You can only access your own accounts"})
    services.ensure_user_exists(db, userId)
    accounts = db.query(Account).filter(Account.owner_id == userId).all()
    account_summaries = [
        AccountSummary(accountNumber=acc.account_number, currency=acc.currency, balance=f"{acc.balance:.2f}", createdAt=as_utc(acc.created_at))
        for acc in accounts
    ]
    return UserAccountsListResponse(userId=userId, accounts=account_summaries)


@app.post("/users/{userId}/accounts", status_code=201, response_model=AccountCreationResponse, tags=["Accounts"], responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}, 403: {"model": ErrorOut}, 404: {"model": ErrorOut}})
@app.post("/api/v1/users/{userId}/accounts", include_in_schema=False, status_code=201, response_model=AccountCreationResponse, responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}, 403: {"model": ErrorOut}, 404: {"model": ErrorOut}})
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
    logger.info("account.created user_id=%s account=%s currency=%s", userId, account.account_number, account.currency)
    return AccountCreationResponse(accountNumber=account.account_number, ownerId=account.owner_id, currency=account.currency, balance=f"{account.balance:.2f}", createdAt=as_utc(account.created_at))


@app.get("/accounts/{accountNumber}", response_model=AccountLookupResponse, tags=["Accounts"], responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}})
@app.get("/api/v1/accounts/{accountNumber}", include_in_schema=False, response_model=AccountLookupResponse, responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}})
def lookup_account(accountNumber: str, db: Session = Depends(get_db)):
    normalized = accountNumber.upper()
    if re.fullmatch(r"^[A-Z0-9]{8}$", normalized) is None:
        raise HTTPException(status_code=400, detail={"code": "INVALID_ACCOUNT_NUMBER", "message": "Account number must be exactly 8 characters"})

    account = db.get(Account, normalized)
    if not account:
        raise HTTPException(status_code=404, detail={"code": "ACCOUNT_NOT_FOUND", "message": f"Account with number '{accountNumber}' not found"})
    user = services.ensure_user_exists(db, account.owner_id)
    return AccountLookupResponse(accountNumber=account.account_number, ownerName=user.full_name, currency=account.currency)


@app.post("/transfers", status_code=201, response_model=TransferResponse, tags=["Transfers"], responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}, 404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ErrorOut}, 503: {"model": ErrorOut}})
@app.post("/api/v1/transfers", include_in_schema=False, status_code=201, response_model=TransferResponse, responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}, 404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 422: {"model": ErrorOut}, 503: {"model": ErrorOut}})
async def initiate_transfer(payload: TransferRequest, current_user_id: str = Depends(get_current_user_id), db: Session = Depends(get_db)):
    source_acc_num = payload.sourceAccount.upper()
    destination_acc_num = payload.destinationAccount.upper()
    amount = services.parse_amount(payload.amount)

    if source_acc_num == destination_acc_num:
        raise HTTPException(status_code=400, detail={"code": "INVALID_REQUEST", "message": "Source and destination accounts must be different"})

    source = services.ensure_account_exists(db, source_acc_num)
    if source.owner_id != current_user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Source account does not belong to authenticated user"})

    services.ensure_transfer_not_duplicate(db, payload.transferId)
    logger.info("transfer.initiated transfer_id=%s source=%s destination=%s", payload.transferId, source_acc_num, destination_acc_num)

    source_prefix = source_acc_num[:3]
    destination_prefix = destination_acc_num[:3]

    if source_prefix != settings.bank_prefix.upper():
        raise HTTPException(status_code=400, detail={"code": "INVALID_REQUEST", "message": "Source account does not belong to this bank"})

    if destination_prefix == settings.bank_prefix.upper():
        destination = services.ensure_account_exists(db, destination_acc_num)
        transfer = services.debit_credit_same_bank(db, payload.transferId, source, destination, amount)
        db.commit()
        db.refresh(transfer)
        logger.info("transfer.completed transfer_id=%s mode=same_bank", transfer.transfer_id)
        return TransferResponse(
            transferId=transfer.transfer_id,
            status=transfer.status,
            sourceAccount=transfer.source_account,
            destinationAccount=transfer.destination_account,
            amount=f"{transfer.amount:.2f}",
            timestamp=as_utc(transfer.timestamp),
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
        logger.info("transfer.completed transfer_id=%s mode=interbank destination_bank=%s", transfer.transfer_id, destination_bank_id)
    except Exception:
        logger.warning("transfer.pending transfer_id=%s destination_bank=%s", transfer.transfer_id, destination_bank_id)
        raise HTTPException(status_code=503, detail={"code": "DESTINATION_BANK_UNAVAILABLE", "message": "Destination bank is temporarily unavailable. Transfer has been queued for retry."})

    return TransferResponse(
        transferId=transfer.transfer_id,
        status=transfer.status,
        sourceAccount=transfer.source_account,
        destinationAccount=transfer.destination_account,
        amount=f"{transfer.amount:.2f}",
        convertedAmount=to_money_str(transfer.converted_amount),
        exchangeRate=to_rate_str(transfer.exchange_rate),
        rateCapturedAt=as_utc_opt(transfer.rate_captured_at),
        timestamp=as_utc(transfer.timestamp),
        errorMessage=transfer.error_message,
    )


@app.post("/transfers/receive", response_model=InterBankTransferResponse, tags=["Transfers"], responses={401: {"model": ErrorOut}, 403: {"model": ErrorOut}, 404: {"model": ErrorOut}})
@app.post("/api/v1/transfers/receive", include_in_schema=False, response_model=InterBankTransferResponse, responses={401: {"model": ErrorOut}, 403: {"model": ErrorOut}, 404: {"model": ErrorOut}})
def receive_interbank_transfer(payload: InterBankTransferRequest, db: Session = Depends(get_db)):
    claims = verify_interbank_jwt(db, payload.jwt)
    destination_account = claims["destinationAccount"].upper()
    amount = services.parse_amount(claims["amount"])
    transfer_id = claims["transferId"]

    destination = services.ensure_account_exists(db, destination_account)

    existing = db.get(Transfer, transfer_id)
    if existing and existing.status == "completed":
        return InterBankTransferResponse(transferId=existing.transfer_id, status=existing.status, destinationAccount=existing.destination_account, amount=f"{existing.amount:.2f}", timestamp=as_utc(existing.timestamp))

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
    logger.info("transfer.received transfer_id=%s destination=%s", transfer.transfer_id, transfer.destination_account)
    return InterBankTransferResponse(transferId=transfer.transfer_id, status=transfer.status, destinationAccount=transfer.destination_account, amount=f"{transfer.amount:.2f}", timestamp=as_utc(transfer.timestamp))


@app.get("/transfers/{transferId}", response_model=TransferStatusResponse, tags=["Transfers"], responses={401: {"model": ErrorOut}, 404: {"model": ErrorOut}, 423: {"model": ErrorOut}})
@app.get("/api/v1/transfers/{transferId}", include_in_schema=False, response_model=TransferStatusResponse, responses={401: {"model": ErrorOut}, 404: {"model": ErrorOut}, 423: {"model": ErrorOut}})
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
        rateCapturedAt=as_utc_opt(transfer.rate_captured_at),
        timestamp=as_utc(transfer.timestamp),
        pendingSince=as_utc_opt(transfer.pending_since),
        nextRetryAt=as_utc_opt(transfer.next_retry_at),
        retryCount=transfer.retry_count,
        errorMessage=transfer.error_message,
    )


@app.get("/api/v1/transfers", include_in_schema=False, response_model=TransfersListResponse, responses={401: {"model": ErrorOut}})
def list_transfers(
    current_user_id: str = Depends(get_current_user_id),
    status: str | None = None,
    account: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    if limit < 1 or limit > 100:
        limit = 50
    if offset < 0:
        offset = 0

    # Get all accounts owned by user
    user_accounts = db.query(Account).filter(Account.owner_id == current_user_id).all()
    user_account_numbers = [acc.account_number for acc in user_accounts]

    # Build query for transfers where user is source or destination
    query = db.query(Transfer)
    query = query.filter(
        or_(
            Transfer.source_account.in_(user_account_numbers),
            Transfer.destination_account.in_(user_account_numbers)
        )
    )

    # Filter by status if provided
    if status:
        query = query.filter(Transfer.status == status)

    # Filter by account if provided
    if account:
        account_upper = account.upper()
        query = query.filter(
            or_(
                Transfer.source_account == account_upper,
                Transfer.destination_account == account_upper
            )
        )

    # Get total count and apply pagination
    total = query.count()
    transfers = query.order_by(Transfer.timestamp.desc()).limit(limit).offset(offset).all()

    transfer_items = [
        {
            "transferId": t.transfer_id,
            "status": t.status,
            "sourceAccount": t.source_account,
            "destinationAccount": t.destination_account,
            "amount": f"{t.amount:.2f}",
            "convertedAmount": to_money_str(t.converted_amount),
            "exchangeRate": to_rate_str(t.exchange_rate),
            "rateCapturedAt": as_utc_opt(t.rate_captured_at),
            "timestamp": as_utc(t.timestamp),
            "errorMessage": t.error_message,
        }
        for t in transfers
    ]

    return TransfersListResponse(transfers=transfer_items, total=total, limit=limit, offset=offset)


@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "service": settings.app_name}

