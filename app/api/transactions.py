from __future__ import annotations

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from pydantic import ValidationError

from app.schemas.transaction import TransactionCreate, TransactionUpdate, TransactionResponse
from app.services.transaction_service import TransactionService
from app.services.auth_service import AuthService
from app.services.email_service import EmailService

transactions_bp = Blueprint("transactions", __name__)


def _validation_error(exc: ValidationError):
    return jsonify({"error": "Validation failed", "detail": exc.errors()}), 422


@transactions_bp.get("")
@jwt_required()
def list_transactions():
    """
    GET /api/transactions
    Query params: page, per_page, type (credit|debit), category, date_from, date_to
    """
    user_id   = get_jwt_identity()
    page      = request.args.get("page", 1, type=int)
    per_page  = request.args.get("per_page", 20, type=int)
    tx_type   = request.args.get("type")
    category  = request.args.get("category")
    date_from = request.args.get("date_from")  # ISO date string e.g. "2024-01-01"
    date_to   = request.args.get("date_to")    # ISO date string e.g. "2024-12-31"

    result = TransactionService.list_for_user(
        user_id=user_id,
        page=page,
        per_page=per_page,
        tx_type=tx_type,
        category=category,
        date_from=date_from,
        date_to=date_to,
    )
    return jsonify(result.model_dump()), 200


@transactions_bp.post("")
@jwt_required()
def create_transaction():
    """POST /api/transactions"""
    user_id = get_jwt_identity()

    try:
        body = TransactionCreate.model_validate(request.get_json(force=True))
    except ValidationError as e:
        return _validation_error(e)

    tx = TransactionService.create(user_id, body)

    if tx.amount >= 10000:
        user = AuthService.get_by_id(user_id)
        if user and user.email:
            EmailService.send_large_transaction_alert(user, tx)

    return jsonify(TransactionResponse.model_validate(tx).model_dump()), 201


@transactions_bp.get("/<string:tx_id>")
@jwt_required()
def get_transaction(tx_id: str):
    """GET /api/transactions/:id"""
    user_id = get_jwt_identity()
    tx = TransactionService.get(user_id, tx_id)
    if not tx:
        return jsonify({"error": "Transaction not found"}), 404
    return jsonify(TransactionResponse.model_validate(tx).model_dump()), 200


@transactions_bp.patch("/<string:tx_id>")
@jwt_required()
def update_transaction(tx_id: str):
    """PATCH /api/transactions/:id — partial update"""
    user_id = get_jwt_identity()

    try:
        body = TransactionUpdate.model_validate(request.get_json(force=True))
    except ValidationError as e:
        return _validation_error(e)

    try:
        tx = TransactionService.update(user_id, tx_id, body)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404

    return jsonify(TransactionResponse.model_validate(tx).model_dump()), 200


@transactions_bp.delete("/<string:tx_id>")
@jwt_required()
def delete_transaction(tx_id: str):
    """DELETE /api/transactions/:id"""
    user_id = get_jwt_identity()

    try:
        TransactionService.delete(user_id, tx_id)
    except LookupError as e:
        return jsonify({"error": str(e)}), 404

    return "", 204


@transactions_bp.get("/summary")
@jwt_required()
def summary():
    """GET /api/transactions/summary — balance, totals, counts with optional filters"""
    user_id   = get_jwt_identity()
    tx_type   = request.args.get("type")
    category  = request.args.get("category")
    date_from = request.args.get("date_from")
    date_to   = request.args.get("date_to")

    stats = TransactionService.summary(
        user_id=user_id,
        tx_type=tx_type,
        category=category,
        date_from=date_from,
        date_to=date_to,
    )
    return jsonify(stats), 200
