import pytest
from pydantic import ValidationError

from app.config import Settings


def test_bank_prefix_normalizes_to_uppercase():
    settings = Settings(bank_prefix="oll")
    assert settings.bank_prefix == "OLL"


def test_bank_prefix_must_be_exactly_three_alnum_chars():
    with pytest.raises(ValidationError):
        Settings(bank_prefix="OL")

    with pytest.raises(ValidationError):
        Settings(bank_prefix="OLLL")

    with pytest.raises(ValidationError):
        Settings(bank_prefix="O-1")
