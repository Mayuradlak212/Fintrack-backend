from __future__ import annotations

import os
import base64
from datetime import datetime
from pathlib import Path

from app.core.database import db
from app.models.transaction import Transaction, Category, TransactionType
from app.schemas.transaction import TransactionCreate, TransactionUpdate, TransactionListResponse, TransactionResponse

def _save_receipt_locally(tx_id: str, b64_data: str | None):
    if not b64_data:
        return
    try:
        header, encoded = b64_data.split(",", 1) if "," in b64_data else ("", b64_data)
        ext = ".jpg"
        if "png" in header.lower(): ext = ".png"
        elif "pdf" in header.lower(): ext = ".pdf"
        
        upload_dir = Path("uploads/receipts")
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_path = upload_dir / f"{tx_id}{ext}"
        
        with open(file_path, "wb") as f:
            f.write(base64.b64decode(encoded))
    except Exception as e:
        print(f"Failed to save local receipt for {tx_id}: {e}")


class TransactionService:
    """CRUD operations for transactions, scoped per user."""

    @staticmethod
    def create(user_id: str, data: TransactionCreate) -> Transaction:
        tx = Transaction(
            user_id=user_id,
            type=TransactionType(data.type),
            amount=data.amount,
            description=data.description,
            category=Category(data.category),
            date=data.date,
            receipt_base64=data.receipt_base64,
            receipt_name=data.receipt_name,
            receipt_mime_type=data.receipt_mime_type,
        )
        db.session.add(tx)
        db.session.commit()
        db.session.refresh(tx)
        
        if tx.receipt_base64:
            _save_receipt_locally(tx.id, tx.receipt_base64)
            
        return tx

    @staticmethod
    def list_for_user(
        user_id: str,
        page: int = 1,
        per_page: int = 20,
        tx_type: str | None = None,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> TransactionListResponse:
        query = Transaction.query.filter_by(user_id=user_id, is_active=True)

        if tx_type:
            query = query.filter(Transaction.type == TransactionType(tx_type))
        if category:
            query = query.filter(Transaction.category == Category(category))
        if date_from:
            try:
                dt_from = datetime.fromisoformat(date_from)
                query = query.filter(Transaction.date >= dt_from)
            except ValueError:
                pass  # ignore malformed date
        if date_to:
            try:
                # Include the full end day by going to end-of-day
                dt_to = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
                query = query.filter(Transaction.date <= dt_to)
            except ValueError:
                pass  # ignore malformed date

        query = query.order_by(Transaction.date.desc())
        paginated = query.paginate(page=page, per_page=per_page, error_out=False)

        return TransactionListResponse(
            items=[TransactionResponse.model_validate(t) for t in paginated.items],
            total=paginated.total,
            page=paginated.page,
            per_page=paginated.per_page,
            pages=paginated.pages,
        )

    @staticmethod
    def get(user_id: str, tx_id: str) -> Transaction | None:
        return Transaction.query.filter_by(id=tx_id, user_id=user_id, is_active=True).first()

    @staticmethod
    def update(user_id: str, tx_id: str, data: TransactionUpdate) -> Transaction:
        tx = TransactionService.get(user_id, tx_id)
        if not tx:
            raise LookupError(f"Transaction '{tx_id}' not found.")

        # Apply only provided (non-None) fields
        patch = data.model_dump(exclude_none=True)
        for field, value in patch.items():
            if field == "type":
                setattr(tx, field, TransactionType(value))
            elif field == "category":
                setattr(tx, field, Category(value))
            else:
                setattr(tx, field, value)

        db.session.commit()
        db.session.refresh(tx)
        
        if "receipt_base64" in patch:
            _save_receipt_locally(tx.id, tx.receipt_base64)
            
        return tx

    @staticmethod
    def delete(user_id: str, tx_id: str) -> None:
        tx = TransactionService.get(user_id, tx_id)
        if not tx:
            raise LookupError(f"Transaction '{tx_id}' not found.")
        tx.is_active = False
        db.session.commit()

    @staticmethod
    def summary(
        user_id: str,
        tx_type: str | None = None,
        category: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """Returns total credit, debit, balance and counts filtered by optional query params."""
        query = Transaction.query.filter_by(user_id=user_id, is_active=True)

        if tx_type:
            query = query.filter(Transaction.type == TransactionType(tx_type))
        if category:
            query = query.filter(Transaction.category == Category(category))
        if date_from:
            try:
                dt_from = datetime.fromisoformat(date_from)
                query = query.filter(Transaction.date >= dt_from)
            except ValueError:
                pass
        if date_to:
            try:
                dt_to = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
                query = query.filter(Transaction.date <= dt_to)
            except ValueError:
                pass

        txs = query.all()
        total_credit = sum(float(t.amount) for t in txs if t.type == TransactionType.credit)
        total_debit  = sum(float(t.amount) for t in txs if t.type == TransactionType.debit)

        monthly_map = {}
        category_map = {}
        for t in txs:
            # month format: "Jan 24" for compatibility with frontend formatIST
            m = t.date.strftime("%b %y")
            if m not in monthly_map:
                monthly_map[m] = {"credit": 0.0, "debit": 0.0}
            if t.type == TransactionType.credit:
                monthly_map[m]["credit"] += float(t.amount)
            else:
                monthly_map[m]["debit"] += float(t.amount)
                cat = t.category.value if t.category else "Other"
                category_map[cat] = category_map.get(cat, 0.0) + float(t.amount)

        # Ensure correct order of months by storing original datetime or just let frontend sort?
        # Actually frontend expects them in order, so let's sort them. 
        # But wait, date.strftime("%b %y") can't be easily sorted. Let's just output them as they are
        # and sort by the minimum date in that month.
        def _get_min_date(month_str):
            # Find the earliest date matching this month string
            return min([tx.date for tx in txs if tx.date.strftime("%b %y") == month_str])

        monthly = []
        for k, v in monthly_map.items():
            monthly.append({"month": k, "credit": round(v["credit"], 2), "debit": round(v["debit"], 2), "sort_date": _get_min_date(k)})
        
        monthly.sort(key=lambda x: x["sort_date"])
        for item in monthly:
            del item["sort_date"]

        categories = [{"category": k, "amount": round(v, 2)} for k, v in category_map.items()]
        # sort categories by amount desc
        categories.sort(key=lambda x: x["amount"], reverse=True)

        return {
            "total_credit": round(total_credit, 2),
            "total_debit": round(total_debit, 2),
            "balance": round(total_credit - total_debit, 2),
            "count": len(txs),
            "credit_count": sum(1 for t in txs if t.type == TransactionType.credit),
            "debit_count":  sum(1 for t in txs if t.type == TransactionType.debit),
            "monthly": monthly,
            "categories": categories
        }
