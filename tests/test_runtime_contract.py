from datetime import datetime, timezone
from decimal import Decimal
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import jwt

from app.auth import create_user_token
from app.models import Account, BankCache


def register_user(client, full_name: str, email: str | None = None) -> dict:
    payload = {"fullName": full_name}
    if email:
        payload["email"] = email
    response = client.post("/api/v1/users", json=payload)
    assert response.status_code == 201
    return response.json()


def auth_header(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_user_token(user_id)}"}


def create_account(client, user: dict, currency: str = "EUR") -> dict:
    response = client.post(
        f"/api/v1/users/{user['userId']}/accounts",
        headers=auth_header(user["userId"]),
        json={"currency": currency},
    )
    assert response.status_code == 201
    return response.json()


def seed_balance(db_session, account_number: str, amount: str):
    account = db_session.get(Account, account_number)
    account.balance = Decimal(amount)
    db_session.commit()


def test_users_and_accounts_and_lookup_endpoints(client):
    user = register_user(client, "Jane Contract", "jane.contract@example.com")

    unauthorized = client.post(
        f"/api/v1/users/{user['userId']}/accounts",
        json={"currency": "EUR"},
    )
    assert unauthorized.status_code == 401

    account = create_account(client, user, "EUR")

    lookup = client.get(f"/api/v1/accounts/{account['accountNumber']}")
    assert lookup.status_code == 200
    data = lookup.json()
    assert data["ownerName"] == "Jane Contract"
    assert data["currency"] == "EUR"

    malformed = client.get("/api/v1/accounts/BAD")
    assert malformed.status_code == 400
    assert malformed.json()["code"] == "INVALID_ACCOUNT_NUMBER"


def test_transfers_and_transfer_status_endpoints(client, db_session):
    sender = register_user(client, "Sender")
    receiver = register_user(client, "Receiver")

    source = create_account(client, sender, "EUR")
    destination = create_account(client, receiver, "EUR")

    seed_balance(db_session, source["accountNumber"], "100.00")

    transfer_id = str(uuid.uuid4())
    transfer = client.post(
        "/api/v1/transfers",
        headers=auth_header(sender["userId"]),
        json={
            "transferId": transfer_id,
            "sourceAccount": source["accountNumber"],
            "destinationAccount": destination["accountNumber"],
            "amount": "25.00",
        },
    )
    assert transfer.status_code == 201
    assert transfer.json()["status"] == "completed"

    duplicate = client.post(
        "/api/v1/transfers",
        headers=auth_header(sender["userId"]),
        json={
            "transferId": transfer_id,
            "sourceAccount": source["accountNumber"],
            "destinationAccount": destination["accountNumber"],
            "amount": "25.00",
        },
    )
    assert duplicate.status_code == 409

    status = client.get(
        f"/api/v1/transfers/{transfer_id}",
        headers=auth_header(sender["userId"]),
    )
    assert status.status_code == 200
    assert status.json()["status"] == "completed"


def test_receive_interbank_transfer_endpoint(client, db_session):
    beneficiary = register_user(client, "Cross Bank Beneficiary")
    destination = create_account(client, beneficiary, "EUR")

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    trusted_bank = BankCache(
        bank_id="LAT002",
        name="Latvia Savings Bank",
        address="https://lat.example.test",
        public_key=public_key,
        last_heartbeat=datetime.now(timezone.utc),
        status="active",
        last_synced_at=datetime.now(timezone.utc),
    )
    db_session.add(trusted_bank)
    db_session.commit()

    transfer_id = str(uuid.uuid4())
    token = jwt.encode(
        {
            "transferId": transfer_id,
            "sourceAccount": "LAT12345",
            "destinationAccount": destination["accountNumber"],
            "amount": "40.00",
            "sourceBankId": "LAT002",
            "destinationBankId": "EST001",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nonce": "abc123nonce",
        },
        private_key,
        algorithm="ES256",
    )

    receive = client.post("/api/v1/transfers/receive", json={"jwt": token})
    assert receive.status_code == 200
    assert receive.json()["status"] == "completed"

    # Endpoint is idempotent for already completed incoming transfer.
    receive_again = client.post("/api/v1/transfers/receive", json={"jwt": token})
    assert receive_again.status_code == 200
    assert receive_again.json()["transferId"] == transfer_id


def test_receive_interbank_transfer_invalid_jwt_returns_auth_error(client):
    response = client.post("/api/v1/transfers/receive", json={"jwt": "test"})
    assert response.status_code in (401, 403)
