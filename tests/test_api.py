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


def test_get_user_profile():
    r = client.post("/api/v1/users", json={"fullName": "Charlie", "email": "charlie@example.com"})
    assert r.status_code == 201
    user = r.json()
    
    profile = client.get(
        f"/api/v1/users/{user['userId']}",
        headers=auth_header(user["userId"])
    )
    assert profile.status_code == 200
    data = profile.json()
    assert data["userId"] == user["userId"]
    assert data["fullName"] == "Charlie"
    assert data["email"] == "charlie@example.com"


def test_list_user_accounts():
    r = client.post("/api/v1/users", json={"fullName": "David"})
    assert r.status_code == 201
    user = r.json()
    
    # Create 3 accounts
    accs = []
    for currency in ["EUR", "USD", "GBP"]:
        acc = client.post(
            f"/api/v1/users/{user['userId']}/accounts",
            headers=auth_header(user["userId"]),
            json={"currency": currency},
        ).json()
        accs.append(acc)
    
    # List accounts
    list_resp = client.get(
        f"/api/v1/users/{user['userId']}/accounts",
        headers=auth_header(user["userId"])
    )
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert data["userId"] == user["userId"]
    assert len(data["accounts"]) == 3
    assert all(acc["currency"] in ["EUR", "USD", "GBP"] for acc in data["accounts"])


def test_list_transfers_with_filters():
    r1 = client.post("/api/v1/users", json={"fullName": "Eve"})
    r2 = client.post("/api/v1/users", json={"fullName": "Frank"})
    user1 = r1.json()
    user2 = r2.json()
    
    acc1 = client.post(
        f"/api/v1/users/{user1['userId']}/accounts",
        headers=auth_header(user1["userId"]),
        json={"currency": "EUR"},
    ).json()
    acc2 = client.post(
        f"/api/v1/users/{user2['userId']}/accounts",
        headers=auth_header(user2["userId"]),
        json={"currency": "EUR"},
    ).json()
    
    # Add balance
    db = SessionLocal()
    try:
        src = db.get(Account, acc1["accountNumber"])
        src.balance = Decimal("500.00")
        db.commit()
    finally:
        db.close()
    
    # Create 2 transfers
    transfer1_id = "660e9511-f30c-52e5-b827-557766551111"
    transfer2_id = "770e0622-g41d-63f6-c938-668877662222"
    
    t1 = client.post(
        "/api/v1/transfers",
        headers=auth_header(user1["userId"]),
        json={
            "transferId": transfer1_id,
            "sourceAccount": acc1["accountNumber"],
            "destinationAccount": acc2["accountNumber"],
            "amount": "50.00",
        },
    )
    assert t1.status_code == 201
    
    t2 = client.post(
        "/api/v1/transfers",
        headers=auth_header(user1["userId"]),
        json={
            "transferId": transfer2_id,
            "sourceAccount": acc1["accountNumber"],
            "destinationAccount": acc2["accountNumber"],
            "amount": "75.00",
        },
    )
    assert t2.status_code == 201
    
    # List all transfers
    list_resp = client.get(
        "/api/v1/transfers",
        headers=auth_header(user1["userId"])
    )
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert data["total"] >= 2
    assert len(data["transfers"]) >= 2
    
    # Filter by account
    list_filtered = client.get(
        f"/api/v1/transfers?account={acc1['accountNumber']}",
        headers=auth_header(user1["userId"])
    )
    assert list_filtered.status_code == 200
    filtered_data = list_filtered.json()
    assert all(t["sourceAccount"] == acc1["accountNumber"] or t["destinationAccount"] == acc1["accountNumber"] for t in filtered_data["transfers"])

