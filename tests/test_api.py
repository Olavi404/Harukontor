import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///./test.db")
os.environ.setdefault("USER_JWT_SECRET", "test-secret")
os.environ.setdefault("BANK_REGISTRATION_ID", "EST001")
os.environ.setdefault("BANK_PREFIX", "EST")
os.environ.setdefault("CENTRAL_BANK_BASE_URL", "https://test.diarainfra.com/central-bank/api/v1")

from fastapi.testclient import TestClient

from app import main as app_main
from app.auth import create_user_token
from app.models import Account, BankCache
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
    assert register.headers.get("X-API-Key")
    user = register.json()
    assert user["createdAt"].endswith("Z")

    create_account = client.post(
        f"/api/v1/users/{user['userId']}/accounts",
        headers=auth_header(user["userId"]),
        json={"currency": "EUR"},
    )
    assert create_account.status_code == 201
    account = create_account.json()
    assert account["createdAt"].endswith("Z")

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


def test_api_key_authentication_on_protected_endpoint():
    register = client.post("/api/v1/users", json={"fullName": "Api Key User", "email": "apikey@example.com"})
    assert register.status_code == 201
    user_id = register.json()["userId"]
    api_key = register.headers.get("X-API-Key")
    assert api_key

    create_account = client.post(
        f"/api/v1/users/{user_id}/accounts",
        headers={"X-API-Key": api_key},
        json={"currency": "EUR"},
    )
    assert create_account.status_code == 201


def test_transfer_rejects_same_source_and_destination_account():
    r = client.post("/api/v1/users", json={"fullName": "Same Account User"})
    assert r.status_code == 201
    user = r.json()

    account = client.post(
        f"/api/v1/users/{user['userId']}/accounts",
        headers=auth_header(user["userId"]),
        json={"currency": "EUR"},
    ).json()

    db = SessionLocal()
    try:
        src = db.get(Account, account["accountNumber"])
        src.balance = Decimal("100.00")
        db.commit()
    finally:
        db.close()

    transfer = client.post(
        "/api/v1/transfers",
        headers=auth_header(user["userId"]),
        json={
            "transferId": "9ddbc7f0-0cc3-4b20-a5c4-42a87ce54d3e",
            "sourceAccount": account["accountNumber"],
            "destinationAccount": account["accountNumber"],
            "amount": "10.00",
        },
    )
    assert transfer.status_code == 400
    body = transfer.json()
    assert body["code"] == "INVALID_REQUEST"


def test_validation_errors_return_422_http_validation_error_shape():
    bad_user = client.post("/users", json={"fullName": "A", "email": "not-an-email"})
    assert bad_user.status_code == 422
    assert isinstance(bad_user.json().get("detail"), list)

    ok_user = client.post("/api/v1/users", json={"fullName": "Validation Owner", "email": "validation.owner@example.com"})
    assert ok_user.status_code == 201
    api_key = ok_user.headers.get("X-API-Key")
    user_id = ok_user.json()["userId"]

    bad_currency = client.post(
        f"/users/{user_id}/accounts",
        headers={"X-API-Key": api_key},
        json={"currency": "eur"},
    )
    assert bad_currency.status_code == 422
    assert isinstance(bad_currency.json().get("detail"), list)


def test_issue_bearer_token_from_api_key_and_use_for_protected_endpoint():
    register = client.post("/users", json={"fullName": "Token Flow User", "email": "token.flow@example.com"})
    assert register.status_code == 201
    user_id = register.json()["userId"]
    api_key = register.headers.get("X-API-Key")
    assert api_key

    issue = client.post("/auth/token", json={"apiKey": api_key})
    assert issue.status_code == 200
    token = issue.json()["accessToken"]
    assert issue.json()["tokenType"] == "Bearer"
    assert issue.json()["expiresInSeconds"] > 0

    create = client.post(
        f"/users/{user_id}/accounts",
        headers={"Authorization": f"Bearer {token}"},
        json={"currency": "EUR"},
    )
    assert create.status_code == 201


def test_multiple_users_and_same_and_cross_bank_transfers(monkeypatch):
    sender = client.post("/api/v1/users", json={"fullName": "Transfer Sender", "email": "sender@example.com"})
    same_bank_receiver = client.post("/api/v1/users", json={"fullName": "Local Receiver", "email": "local@example.com"})
    assert sender.status_code == 201
    assert same_bank_receiver.status_code == 201

    sender_user = sender.json()
    receiver_user = same_bank_receiver.json()

    source_account = client.post(
        f"/api/v1/users/{sender_user['userId']}/accounts",
        headers=auth_header(sender_user["userId"]),
        json={"currency": "USD"},
    ).json()
    local_destination = client.post(
        f"/api/v1/users/{receiver_user['userId']}/accounts",
        headers=auth_header(receiver_user["userId"]),
        json={"currency": "USD"},
    ).json()

    db = SessionLocal()
    try:
        src = db.get(Account, source_account["accountNumber"])
        src.balance = Decimal("250.00")
        db.add(
            BankCache(
                bank_id="LAT002",
                name="Latvia Test Bank",
                address="https://latvia.example.test",
                public_key="test-public-key",
                last_heartbeat=datetime.now(timezone.utc),
                status="active",
                last_synced_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()

    same_bank_transfer_id = str(uuid4())
    same_bank_transfer = client.post(
        "/api/v1/transfers",
        headers=auth_header(sender_user["userId"]),
        json={
            "transferId": same_bank_transfer_id,
            "sourceAccount": source_account["accountNumber"],
            "destinationAccount": local_destination["accountNumber"],
            "amount": "25.00",
        },
    )
    assert same_bank_transfer.status_code == 201
    assert same_bank_transfer.json()["status"] == "completed"

    async def fake_get_exchange_rates():
        return ({"USD": Decimal("1.200000")}, datetime.now(timezone.utc), "EUR")

    async def fake_post_interbank_transfer(destination_bank_address: str, jwt_token: str):
        return {"status": "completed", "destinationBankAddress": destination_bank_address, "jwt": jwt_token}

    monkeypatch.setattr(app_main, "get_exchange_rates", fake_get_exchange_rates)
    monkeypatch.setattr(app_main, "post_interbank_transfer", fake_post_interbank_transfer)

    cross_bank_transfer_id = str(uuid4())
    cross_bank_transfer = client.post(
        "/api/v1/transfers",
        headers=auth_header(sender_user["userId"]),
        json={
            "transferId": cross_bank_transfer_id,
            "sourceAccount": source_account["accountNumber"],
            "destinationAccount": "LAT54321",
            "amount": "50.00",
        },
    )
    assert cross_bank_transfer.status_code == 201
    cross_data = cross_bank_transfer.json()
    assert cross_data["status"] == "completed"
    assert cross_data["convertedAmount"] == "41.67"
    assert cross_data["exchangeRate"] == "0.833333"

    status = client.get(
        f"/api/v1/transfers/{cross_bank_transfer_id}",
        headers=auth_header(sender_user["userId"]),
    )
    assert status.status_code == 200
    assert status.json()["status"] == "completed"


def test_assignment_scenario_create_bank_account_and_transfer_to_another_bank(monkeypatch):
    local_bank = client.post("/api/v1/users", json={"fullName": "Assignment Bank Owner", "email": "owner@example.com"})
    destination_owner = client.post("/api/v1/users", json={"fullName": "Destination Owner", "email": "destination@example.com"})
    assert local_bank.status_code == 201
    assert destination_owner.status_code == 201

    local_user = local_bank.json()
    destination_user = destination_owner.json()

    source_account = client.post(
        f"/api/v1/users/{local_user['userId']}/accounts",
        headers=auth_header(local_user["userId"]),
        json={"currency": "EUR"},
    ).json()
    destination_account = client.post(
        f"/api/v1/users/{destination_user['userId']}/accounts",
        headers=auth_header(destination_user["userId"]),
        json={"currency": "EUR"},
    ).json()

    db = SessionLocal()
    try:
        src = db.get(Account, source_account["accountNumber"])
        src.balance = Decimal("300.00")
        db.add(
            BankCache(
                bank_id="LAT001",
                name="Latvia Demo Bank",
                address="https://latvia.demo.test",
                public_key="demo-public-key",
                last_heartbeat=datetime.now(timezone.utc),
                status="active",
                last_synced_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()

    async def fake_get_exchange_rates():
        return ({"EUR": Decimal("1.000000")}, datetime.now(timezone.utc), "EUR")

    async def fake_post_interbank_transfer(destination_bank_address: str, jwt_token: str):
        return {"status": "completed", "destinationBankAddress": destination_bank_address, "jwt": jwt_token}

    monkeypatch.setattr(app_main, "get_exchange_rates", fake_get_exchange_rates)
    monkeypatch.setattr(app_main, "post_interbank_transfer", fake_post_interbank_transfer)

    transfer_id = str(uuid4())
    response = client.post(
        "/api/v1/transfers",
        headers=auth_header(local_user["userId"]),
        json={
            "transferId": transfer_id,
            "sourceAccount": source_account["accountNumber"],
            "destinationAccount": "LAT99887",
            "amount": "60.00",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["amount"] == "60.00"

    transfer_status = client.get(
        f"/api/v1/transfers/{transfer_id}",
        headers=auth_header(local_user["userId"]),
    )
    assert transfer_status.status_code == 200
    assert transfer_status.json()["status"] == "completed"

