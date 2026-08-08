from __future__ import annotations

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token
from pydantic import ValidationError

from app.schemas.user import (
    RegisterRequest,
    LoginRequest,
    TokenResponse,
    UserResponse,
    RefreshResponse,
    TotpCodeRequest,
    TotpDisableRequest,
    TotpSetupResponse,
    TotpActivateResponse,
    MfaVerifyRequest,
    MfaRequiredResponse,
)
from app.services.auth_service import AuthService
from app.services.totp_service import TotpService
from app.core.config import settings
from app.core.rate_limit import body_email, rate_limit

auth_bp = Blueprint("auth", __name__)


def _validation_error(exc: ValidationError):
    return jsonify({"error": "Validation failed", "detail": exc.errors()}), 422


@auth_bp.post("/register")
@rate_limit("auth:register")
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
@rate_limit("auth:login")
@rate_limit("auth:login_account", discriminator=body_email)
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

    # 2FA is on — password verified, but no session until the code is supplied.
    if result.get("mfa_required"):
        return jsonify(MfaRequiredResponse(mfa_token=result["mfa_token"]).model_dump()), 200

    response = TokenResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        user=UserResponse.model_validate(result["user"]),
    )
    return jsonify(response.model_dump()), 200


@auth_bp.post("/refresh")
@jwt_required(refresh=True)
@rate_limit("auth:refresh")
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
@rate_limit("api:read")
def me():
    """GET /api/auth/me — returns the current authenticated user."""
    user_id = get_jwt_identity()
    user = AuthService.get_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify(UserResponse.model_validate(user).model_dump()), 200

@auth_bp.patch("/me")
@jwt_required()
@rate_limit("api:write")
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
@rate_limit("auth:forgot_password")
@rate_limit("auth:forgot_password_account", discriminator=body_email)
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

        # Dev convenience: surface the link in the console so password reset is
        # testable without working SMTP. Never enable in production — the token
        # in this URL is enough to take over the account.
        if settings.FLASK_ENV == "development":
            print(f"\n[PASSWORD RESET] {email}\n[PASSWORD RESET] {reset_url}\n", flush=True)

        EmailService.send_password_reset_email(
            to_email=email,
            user_name=user.name if user else "there",
            reset_url=reset_url,
            expires_minutes=settings.PASSWORD_RESET_TOKEN_EXPIRES_MINUTES,
        )

    return jsonify({"message": "If that email is registered, a reset link has been sent."}), 200


@auth_bp.post("/reset-password")
@rate_limit("auth:reset_password")
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


# ── TOTP two-factor auth ──────────────────────────────────────────────────────


@auth_bp.post("/login/verify")
@rate_limit("auth:mfa")
def login_verify():
    """
    POST /api/auth/login/verify
    Body: { "mfa_token": "...", "code": "123456" }

    Second step of login when 2FA is enabled. `code` accepts either a TOTP code
    or one of the single-use backup codes.
    """
    try:
        body = MfaVerifyRequest.model_validate(request.get_json(force=True))
    except ValidationError as e:
        return _validation_error(e)

    try:
        result = AuthService.verify_mfa(body.mfa_token, body.code)
    except ValueError as e:
        return jsonify({"error": str(e)}), 401

    response = TokenResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        user=UserResponse.model_validate(result["user"]),
    )
    return jsonify(response.model_dump()), 200


@auth_bp.post("/totp/setup")
@jwt_required()
@rate_limit("auth:totp_manage")
def totp_setup():
    """
    POST /api/auth/totp/setup
    Returns the QR code + manual key for the authenticator app.
    2FA is not active until /totp/activate succeeds.
    """
    user = AuthService.get_by_id(get_jwt_identity())
    if not user:
        return jsonify({"error": "User not found"}), 404

    try:
        data = TotpService.begin_setup(user)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(TotpSetupResponse(**data).model_dump()), 200


@auth_bp.post("/totp/activate")
@jwt_required()
@rate_limit("auth:totp_manage")
def totp_activate():
    """
    POST /api/auth/totp/activate
    Body: { "code": "123456" }
    Confirms the app is working, enables 2FA, and returns the backup codes.
    These are shown once and never retrievable again.
    """
    try:
        body = TotpCodeRequest.model_validate(request.get_json(force=True))
    except ValidationError as e:
        return _validation_error(e)

    user = AuthService.get_by_id(get_jwt_identity())
    if not user:
        return jsonify({"error": "User not found"}), 404

    try:
        backup_codes = TotpService.activate(user, body.code)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(TotpActivateResponse(backup_codes=backup_codes).model_dump()), 200


@auth_bp.post("/totp/disable")
@jwt_required()
@rate_limit("auth:totp_manage")
def totp_disable():
    """
    POST /api/auth/totp/disable
    Body: { "password": "..." }
    Password is required so a stolen session token alone cannot strip 2FA.
    """
    try:
        body = TotpDisableRequest.model_validate(request.get_json(force=True))
    except ValidationError as e:
        return _validation_error(e)

    user = AuthService.get_by_id(get_jwt_identity())
    if not user:
        return jsonify({"error": "User not found"}), 404

    try:
        TotpService.disable(user, body.password)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"message": "Two-factor authentication disabled."}), 200
