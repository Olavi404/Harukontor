import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///./test.db")
os.environ.setdefault("USER_JWT_SECRET", "test-secret")
os.environ.setdefault("BANK_REGISTRATION_ID", "EST001")
os.environ.setdefault("BANK_PREFIX", "EST")
os.environ.setdefault("CENTRAL_BANK_BASE_URL", "https://test.diarainfra.com/central-bank/api/v1")

from fastapi.testclient import TestClient

from app.auth import create_user_token
from app.models import Account
from app.database import Base, engine
from app.database import SessionLocal
from app.main import app


client = TestClient(app)


def auth_header(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_user_token(user_id)}"}


def setup_module():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_register_and_lookup_flow():
    register = client.post(
        "/api/v1/users",
        json={"fullName": "Jane Doe", "email": "jane@example.com"},
    )
    assert register.status_code == 201
    user = register.json()

    create_account = client.post(
        f"/api/v1/users/{user['userId']}/accounts",
        headers=auth_header(user["userId"]),
        json={"currency": "EUR"},
    )
    assert create_account.status_code == 201
    account = create_account.json()

    lookup = client.get(f"/api/v1/accounts/{account['accountNumber']}")
    assert lookup.status_code == 200
    assert lookup.json()["ownerName"] == "Jane Doe"


def test_same_bank_transfer_and_status():
    r1 = client.post("/api/v1/users", json={"fullName": "Alice"})
    r2 = client.post("/api/v1/users", json={"fullName": "Bob"})
    assert r1.status_code == 201
    assert r2.status_code == 201
    u1 = r1.json()
    u2 = r2.json()

    a1 = client.post(
        f"/api/v1/users/{u1['userId']}/accounts",
        headers=auth_header(u1["userId"]),
        json={"currency": "EUR"},
    ).json()
    a2 = client.post(
        f"/api/v1/users/{u2['userId']}/accounts",
        headers=auth_header(u2["userId"]),
        json={"currency": "EUR"},
    ).json()

    db = SessionLocal()
    try:
        src = db.get(Account, a1["accountNumber"])
        src.balance = Decimal("250.00")
        db.commit()
    finally:
        db.close()

    transfer_id = "550e8400-e29b-41d4-a716-446655440000"
    transfer = client.post(
        "/api/v1/transfers",
        headers=auth_header(u1["userId"]),
        json={
            "transferId": transfer_id,
            "sourceAccount": a1["accountNumber"],
            "destinationAccount": a2["accountNumber"],
            "amount": "10.00",
        },
    )
    assert transfer.status_code == 201

    status = client.get(
        f"/api/v1/transfers/{transfer_id}",
        headers=auth_header(u1["userId"]),
    )
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
