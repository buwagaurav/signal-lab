from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AuthRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=40)
    rules: dict
    enabled: bool = False


class StrategyResponse(StrategyCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    version: int
    enabled: bool


class PaperTradeCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    side: str = Field(pattern="^(LONG|SHORT)$")
    quantity: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    strategy_id: int | None = None
    notes: str | None = Field(default=None, max_length=1000)


class TradeResponse(PaperTradeCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    opened_at: datetime


class CandleInput(BaseModel):
    timestamp: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
