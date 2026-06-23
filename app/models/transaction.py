from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import db


class TransactionType(str, enum.Enum):
    credit = "credit"
    debit = "debit"


class Category(str, enum.Enum):
    food_dining = "Food & Dining"
    transport = "Transport"
    shopping = "Shopping"
    entertainment = "Entertainment"
    healthcare = "Healthcare"
    housing = "Housing"
    salary = "Salary"
    investment = "Investment"
    transfer = "Transfer"
    other = "Other"


class Transaction(db.Model):
    """Financial transaction — belongs to a user."""

    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, name="transaction_type_enum"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[Category] = mapped_column(
        Enum(Category, name="category_enum"), nullable=False, default=Category.other
    )
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Receipt — stored as base64 in DB (swap for S3 URL later)
    receipt_base64: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt_mime_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    receipt_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    latitude: Mapped[float | None] = mapped_column(nullable=True)
    longitude: Mapped[float | None] = mapped_column(nullable=True)
    location_text: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="transactions")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<Transaction {self.type.value} {self.amount} by {self.user_id}>"
