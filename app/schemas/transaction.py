from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

TransactionTypeEnum = Literal["credit", "debit"]

CategoryEnum = Literal[
    "Food & Dining",
    "Transport",
    "Shopping",
    "Entertainment",
    "Healthcare",
    "Housing",
    "Salary",
    "Investment",
    "Transfer",
    "Other",
]


# ── Request schemas ────────────────────────────────────────────────────────────

class TransactionCreate(BaseModel):
    type: TransactionTypeEnum
    amount: float = Field(gt=0, description="Must be a positive number")
    description: str = Field(min_length=1, max_length=200)
    category: CategoryEnum = "Other"
    date: datetime
    receipt_base64: Optional[str] = None
    receipt_name: Optional[str] = Field(default=None, max_length=255)
    receipt_mime_type: Optional[str] = Field(default=None, max_length=100)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location_text: Optional[str] = Field(default=None, max_length=255)

    @field_validator("amount")
    @classmethod
    def round_amount(cls, v: float) -> float:
        return round(v, 2)


class TransactionUpdate(BaseModel):
    """All fields optional for partial updates (PATCH)."""
    type: Optional[TransactionTypeEnum] = None
    amount: Optional[float] = Field(default=None, gt=0)
    description: Optional[str] = Field(default=None, min_length=1, max_length=200)
    category: Optional[CategoryEnum] = None
    date: Optional[datetime] = None
    receipt_base64: Optional[str] = None
    receipt_name: Optional[str] = Field(default=None, max_length=255)
    receipt_mime_type: Optional[str] = Field(default=None, max_length=100)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location_text: Optional[str] = Field(default=None, max_length=255)

    @field_validator("amount", mode="before")
    @classmethod
    def round_amount(cls, v: Optional[float]) -> Optional[float]:
        return round(v, 2) if v is not None else None


# ── Response schemas ───────────────────────────────────────────────────────────

class TransactionResponse(BaseModel):
    id: str
    user_id: str
    type: str
    amount: float
    description: str
    category: str
    date: datetime
    receipt_base64: str | None = None
    receipt_mime_type: str | None = None
    receipt_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_text: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TransactionListResponse(BaseModel):
    items: list[TransactionResponse]
    total: int
    page: int
    per_page: int
    pages: int
