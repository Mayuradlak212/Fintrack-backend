from __future__ import annotations

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token
from pydantic import ValidationError

from app.schemas.user import RegisterRequest, LoginRequest, TokenResponse, UserResponse, RefreshResponse
from app.services.auth_service import AuthService
from app.core.config import settings

auth_bp = Blueprint("auth", __name__)


def _validation_error(exc: ValidationError):
    return jsonify({"error": "Validation failed", "detail": exc.errors()}), 422


@auth_bp.post("/register")
def register():
    """POST /api/auth/register"""
    try:
        body = RegisterRequest.model_validate(request.get_json(force=True))
    except ValidationError as e:
        return _validation_error(e)

    try:
        result = AuthService.register(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409

    response = TokenResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        user=UserResponse.model_validate(result["user"]),
    )
    return jsonify(response.model_dump()), 201


@auth_bp.post("/login")
def login():
    """POST /api/auth/login"""
    try:
        body = LoginRequest.model_validate(request.get_json(force=True))
    except ValidationError as e:
        return _validation_error(e)

    try:
        result = AuthService.login(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 401

    response = TokenResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        user=UserResponse.model_validate(result["user"]),
    )
    return jsonify(response.model_dump()), 200


@auth_bp.post("/refresh")
@jwt_required(refresh=True)
def refresh():
    """POST /api/auth/refresh — requires a valid refresh token."""
    user_id = get_jwt_identity()
    new_access = create_access_token(
        identity=user_id,
        expires_delta=settings.access_token_expires,
    )
    return jsonify(RefreshResponse(access_token=new_access).model_dump()), 200


@auth_bp.get("/me")
@jwt_required()
def me():
    """GET /api/auth/me — returns the current authenticated user."""
    user_id = get_jwt_identity()
    user = AuthService.get_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify(UserResponse.model_validate(user).model_dump()), 200

@auth_bp.patch("/me")
@jwt_required()
def update_me():
    """PATCH /api/auth/me — update current user profile."""
    from app.schemas.user import UserUpdateRequest
    
    try:
        body = UserUpdateRequest.model_validate(request.get_json(force=True))
    except ValidationError as e:
        return _validation_error(e)

    user_id = get_jwt_identity()
    try:
        updated = AuthService.update_user(user_id, body.model_dump(exclude_unset=True))
        return jsonify(UserResponse.model_validate(updated).model_dump()), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@auth_bp.post("/forgot-password")
def forgot_password():
    """
    POST /api/auth/forgot-password
    Body: { "email": "user@example.com" }
    Always returns 200 to prevent email enumeration.
    """
    from app.services.email_service import EmailService

    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()

    if not email:
        return jsonify({"message": "If that email is registered, a reset link has been sent."}), 200

    raw_token = AuthService.forgot_password(email)
    if raw_token:
        user = AuthService.get_by_id_email(email)
        reset_url = (
            f"{settings.FRONTEND_URL}/auth/reset-password?token={raw_token}"
        )
        EmailService.send_password_reset_email(
            to_email=email,
            user_name=user.name if user else "there",
            reset_url=reset_url,
            expires_minutes=settings.PASSWORD_RESET_TOKEN_EXPIRES_MINUTES,
        )

    return jsonify({"message": "If that email is registered, a reset link has been sent."}), 200


@auth_bp.post("/reset-password")
def reset_password():
    """
    POST /api/auth/reset-password
    Body: { "token": "...", "password": "newpassword123" }
    """
    data = request.get_json(force=True) or {}
    token    = (data.get("token") or "").strip()
    password = (data.get("password") or "").strip()

    if not token or not password:
        return jsonify({"error": "Token and new password are required."}), 400

    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    success = AuthService.reset_password(token, password)
    if not success:
        return jsonify({"error": "Invalid or expired reset link. Please request a new one."}), 400

    return jsonify({"message": "Password updated successfully. You can now log in."}), 200
