import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import secrets

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException, status
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppState, BankCache


settings = get_settings()
PRIVATE_KEY_FILE = Path(settings.keys_dir) / "bank_private_key.pem"
PUBLIC_KEY_FILE = Path(settings.keys_dir) / "bank_public_key.pem"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_keys() -> tuple[str, str]:
    Path(settings.keys_dir).mkdir(parents=True, exist_ok=True)
    if not PRIVATE_KEY_FILE.exists() or not PUBLIC_KEY_FILE.exists():
        private_key = ec.generate_private_key(ec.SECP256R1())
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        PRIVATE_KEY_FILE.write_bytes(private_bytes)
        PUBLIC_KEY_FILE.write_bytes(public_bytes)
    return PRIVATE_KEY_FILE.read_text(encoding="utf-8"), PUBLIC_KEY_FILE.read_text(encoding="utf-8")


async def register_bank(db: Session) -> str:
    _, public_key = ensure_keys()
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{settings.central_bank_base_url}/banks",
            json={"name": settings.bank_name, "address": settings.bank_public_url, "publicKey": public_key},
        )
        if response.status_code not in (200, 201, 409):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "CENTRAL_BANK_UNAVAILABLE", "message": "Bank registration failed"})
        if response.status_code == 409:
            # Duplicate registration can happen after restart; use configured or cached value.
            state = db.get(AppState, "bank_id")
            if state:
                return state.value
            if settings.bank_registration_id:
                return settings.bank_registration_id
            raise HTTPException(status_code=503, detail={"code": "CENTRAL_BANK_UNAVAILABLE", "message": "Duplicate registration and no local bankId"})
        bank_id = response.json()["bankId"]
        state = db.get(AppState, "bank_id")
        if state is None:
            state = AppState(key="bank_id", value=bank_id)
            db.add(state)
        else:
            state.value = bank_id
        db.commit()
        return bank_id


def get_local_bank_id(db: Session) -> str:
    state = db.get(AppState, "bank_id")
    if state:
        return state.value
    if settings.bank_registration_id:
        return settings.bank_registration_id
    raise HTTPException(status_code=503, detail={"code": "CENTRAL_BANK_UNAVAILABLE", "message": "Local bank is not registered yet"})


async def send_heartbeat(db: Session) -> None:
    bank_id = get_local_bank_id(db)
    body = {"timestamp": now_utc().isoformat()}
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(f"{settings.central_bank_base_url}/banks/{bank_id}/heartbeat", json=body)


async def refresh_banks_cache(db: Session) -> None:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{settings.central_bank_base_url}/banks")
        response.raise_for_status()
    payload = response.json()
    synced_at = datetime.fromisoformat(payload["lastSyncedAt"].replace("Z", "+00:00"))
    for bank in payload.get("banks", []):
        last_hb = datetime.fromisoformat(bank["lastHeartbeat"].replace("Z", "+00:00"))
        row = db.get(BankCache, bank["bankId"])
        if row is None:
            row = BankCache(
                bank_id=bank["bankId"],
                name=bank["name"],
                address=bank["address"],
                public_key=bank["publicKey"],
                last_heartbeat=last_hb,
                status=bank.get("status", "active"),
                last_synced_at=synced_at,
            )
            db.add(row)
        else:
            row.name = bank["name"]
            row.address = bank["address"]
            row.public_key = bank["publicKey"]
            row.last_heartbeat = last_hb
            row.status = bank.get("status", "active")
            row.last_synced_at = synced_at
    db.commit()


def resolve_destination_bank_from_cache(db: Session, destination_prefix: str) -> BankCache | None:
    rows = db.execute(select(BankCache)).scalars().all()
    for row in rows:
        if row.bank_id.startswith(destination_prefix):
            return row
    return None


async def get_exchange_rates() -> tuple[dict[str, Decimal], datetime, str]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{settings.central_bank_base_url}/exchange-rates")
        response.raise_for_status()
    payload = response.json()
    rates = {k: Decimal(v) for k, v in payload["rates"].items()}
    ts = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
    return rates, ts, payload["baseCurrency"]


def build_interbank_jwt(transfer_id: str, source_account: str, destination_account: str, amount: Decimal, source_bank_id: str, destination_bank_id: str) -> str:
    private_pem, _ = ensure_keys()
    payload = {
        "transferId": transfer_id,
        "sourceAccount": source_account,
        "destinationAccount": destination_account,
        "amount": f"{amount:.2f}",
        "sourceBankId": source_bank_id,
        "destinationBankId": destination_bank_id,
        "timestamp": now_utc().isoformat(),
        "nonce": secrets.token_hex(8),
    }
    return jwt.encode(payload, private_pem, algorithm="ES256")


def verify_interbank_jwt(db: Session, token: str) -> dict:
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid inter-bank JWT"}) from exc

    source_bank_id = unverified.get("sourceBankId")
    if not source_bank_id:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Missing sourceBankId in JWT"})
    bank = db.get(BankCache, source_bank_id)
    if bank is None:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Source bank is not trusted"})
    try:
        return jwt.decode(token, bank.public_key, algorithms=["ES256"])
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Invalid inter-bank JWT signature or payload"}) from exc


async def post_interbank_transfer(destination_bank_address: str, jwt_token: str) -> dict:
    url = f"{destination_bank_address.rstrip('/')}/api/v1/transfers/receive"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json={"jwt": jwt_token})
        response.raise_for_status()
        return response.json()


async def safe_refresh(db: Session) -> None:
    try:
        await refresh_banks_cache(db)
    except Exception:
        pass


async def resilient_sleep(seconds: int) -> None:
    await asyncio.sleep(max(1, seconds))
