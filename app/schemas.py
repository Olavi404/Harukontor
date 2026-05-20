from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, EmailStr, Field


class ErrorOut(BaseModel):
    code: str
    message: str


class UserRegistrationRequest(BaseModel):
    fullName: str = Field(min_length=2, max_length=200)
    email: EmailStr | None = None


class UserRegistrationResponse(BaseModel):
    userId: str
    fullName: str
    email: EmailStr | None = None
    createdAt: datetime


class TokenExchangeRequest(BaseModel):
    apiKey: str | None = None


class TokenExchangeResponse(BaseModel):
    accessToken: str
    tokenType: str = "Bearer"
    expiresInSeconds: int


class AccountCreationRequest(BaseModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class AccountCreationResponse(BaseModel):
    accountNumber: str
    ownerId: str
    currency: str
    balance: str
    createdAt: datetime


class AccountLookupResponse(BaseModel):
    accountNumber: str
    ownerName: str
    currency: str
    balance: str | None = None
    ownerId: str | None = None


class TransferRequest(BaseModel):
    transferId: str
    sourceAccount: str = Field(pattern=r"^[A-Z0-9]{8}$")
    destinationAccount: str = Field(pattern=r"^[A-Z0-9]{8}$")
    amount: str = Field(pattern=r"^\d+\.\d{2}$")


class TransferResponse(BaseModel):
    transferId: str
    status: str
    sourceAccount: str
    destinationAccount: str
    amount: str
    convertedAmount: str | None = None
    exchangeRate: str | None = None
    rateCapturedAt: datetime | None = None
    timestamp: datetime
    errorMessage: str | None = None


class InterBankTransferRequest(BaseModel):
    jwt: str


class InterBankTransferResponse(BaseModel):
    transferId: str
    status: str
    destinationAccount: str
    amount: str
    timestamp: datetime


class TransferStatusResponse(TransferResponse):
    pendingSince: datetime | None = None
    nextRetryAt: datetime | None = None
    retryCount: int | None = None


class ExchangeRatesResponse(BaseModel):
    baseCurrency: str
    rates: dict[str, str]
    timestamp: datetime


class UserProfileResponse(BaseModel):
    userId: str
    fullName: str
    email: EmailStr | None = None
    createdAt: datetime


class AccountSummary(BaseModel):
    accountNumber: str
    currency: str
    balance: str
    createdAt: datetime


class UserAccountsListResponse(BaseModel):
    userId: str
    accounts: list[AccountSummary]


class TransferListItem(TransferResponse):
    pass


class TransfersListResponse(BaseModel):
    transfers: list[TransferListItem]
    total: int
    limit: int
    offset: int


def to_money_str(v: Decimal | None) -> str | None:
    if v is None:
        return None
    return f"{v:.2f}"


def to_rate_str(v: Decimal | None) -> str | None:
    if v is None:
        return None
    return f"{v:.6f}"
