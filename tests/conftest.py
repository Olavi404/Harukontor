import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///./test.db")
os.environ.setdefault("USER_JWT_SECRET", "test-secret")
os.environ.setdefault("BANK_REGISTRATION_ID", "EST001")
os.environ.setdefault("BANK_PREFIX", "EST")
os.environ.setdefault("CENTRAL_BANK_BASE_URL", "https://test.diarainfra.com/central-bank/api/v1")
os.environ.setdefault("SUPPORTED_CURRENCIES", "EUR,USD,GBP,SEK")

from app.database import Base, SessionLocal, engine
from app.main import app


@pytest.fixture(autouse=True)
def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
