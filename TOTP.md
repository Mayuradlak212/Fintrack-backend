# TOTP Two-Factor Auth — Backend Reference

Server-side implementation of authenticator-app 2FA. For the end-to-end product flow
including the UI, see `frontend/TOTP.md`.

---

## Dependencies

```
pyotp>=2.9.0
qrcode[pil]>=7.4.2
cryptography>=42.0.0
```

Installed versions: `pyotp 2.10.0`, `qrcode 8.1`, `cryptography 44.0.0`.

---

## Migration

`migrations/versions/dd6d2dc2dc38_add_totp_two_factor_auth_columns.py`
(revises `219ff340232d`)

```bash
flask --app run:app db upgrade
```

**Already applied to the Neon database.** Purely additive — 7 nullable-or-defaulted columns
plus one index. Existing rows get `totp_enabled = false`, so accounts that never opt in keep
the original single-step login. Safe to run against a live DB; no table rewrite, no
backfill.

### Columns on `users`

| Column | Type | Notes |
|---|---|---|
| `totp_secret` | `String(255)` | Base32 secret, Fernet-encrypted. Ciphertext measures ~120 chars |
| `totp_enabled` | `Boolean` | `server_default="false"` |
| `totp_confirmed_at` | `DateTime(tz)` | Set at activation |
| `totp_backup_codes` | `Text` | JSON array of sha256 hashes |
| `mfa_challenge_token` | `String(128)` | sha256 of the pending-login token, **indexed** |
| `mfa_challenge_expires` | `DateTime(tz)` | |
| `mfa_attempts` | `Integer` | `server_default="0"` |

A row with `totp_secret` set but `totp_enabled = false` means setup was started and
abandoned. Harmless — the next `/totp/setup` call overwrites it.

---

## Settings

`app/core/config.py`, overridable in `.env`:

```python
TOTP_ISSUER: str = "FinTrack"              # label in the authenticator app
MFA_CHALLENGE_EXPIRES_MINUTES: int = 5     # life of a pending login
MFA_MAX_ATTEMPTS: int = 5                  # wrong codes before the challenge is burned
BACKUP_CODE_COUNT: int = 10
```

---

## Service layer

`app/services/totp_service.py`

| Method | Behaviour |
|---|---|
| `begin_setup(user)` | New base32 secret, stored encrypted, **not** enabled. Returns `secret`, `otpauth_uri`, `qr_base64`. Raises if already enabled |
| `activate(user, code)` | Verifies the code, sets `totp_enabled`, generates + hashes backup codes, returns them in plaintext (the only time they exist) |
| `disable(user, password)` | Verifies the password, clears secret, backup codes, and any pending challenge |
| `start_challenge(user)` | Issues `secrets.token_urlsafe(32)`, stores its sha256, resets `mfa_attempts` |
| `verify_challenge(token, code)` | Looks up by hash, checks expiry and attempt count, tries TOTP then backup code, clears the challenge on success. Raises `ValueError` on every failure path |
| `_check(secret, code)` | `pyotp.TOTP(...).verify(code, valid_window=1)`; strips whitespace |
| `_consume_backup_code(user, code)` | Removes a matching hash from the array. Case-insensitive. Returns `False` on `NULL`/malformed JSON rather than raising |

Encryption helpers live in `app/core/security.py`:

```python
def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))
```

`decrypt_secret` returns `None` on `InvalidToken` instead of raising, so a rotated
`SECRET_KEY` degrades to "code never verifies" rather than a 500.

---

## Endpoints

`app/api/auth.py`

| Method | Endpoint | Auth | Success | Failure |
|---|---|---|---|---|
| POST | `/api/auth/totp/setup` | JWT | 200 `{secret, otpauth_uri, qr_base64}` | 400 already enabled, 404 no user |
| POST | `/api/auth/totp/activate` | JWT | 200 `{message, backup_codes[]}` | 400 bad code, 422 validation |
| POST | `/api/auth/totp/disable` | JWT | 200 `{message}` | 400 wrong password |
| POST | `/api/auth/login/verify` | — | 200 `TokenResponse` | 401 bad/expired/exhausted |

### The `/login` fork

`AuthService.login` returns a challenge instead of tokens when 2FA is on:

```python
if user.totp_enabled:
    return {"mfa_required": True, "mfa_token": TotpService.start_challenge(user)}
```

so `POST /api/auth/login` has two 200 shapes:

```jsonc
{ "access_token": "…", "refresh_token": "…", "token_type": "Bearer", "user": { … } }
{ "mfa_required": true, "mfa_token": "…", "message": "Enter the code from your authenticator app." }
```

**Any client that assumes `access_token` is always present will break** on 2FA accounts.
The Next.js frontend handles this; the Spring Boot backend does not implement it at all
(see [Parity](#parity-warning)).

`UserResponse` gained `totp_enabled: bool = False`, so `GET /api/auth/me` reports the
current state.

---

## Manual testing

```bash
# 1. Log in normally to get an access token
curl -s -X POST localhost:5000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…"}'

TOKEN=<access_token>

# 2. Start setup — qr_base64 is a PNG; decode it to view
curl -s -X POST localhost:5000/api/auth/totp/setup -H "Authorization: Bearer $TOKEN"

# 3. Add the secret to your authenticator, then activate
curl -s -X POST localhost:5000/api/auth/totp/activate \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"code":"123456"}'
# → returns 10 backup codes, once

# 4. Log in again — now returns mfa_token, no session
curl -s -X POST localhost:5000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…"}'

# 5. Complete it (a backup code works here too)
curl -s -X POST localhost:5000/api/auth/login/verify \
  -H 'Content-Type: application/json' \
  -d '{"mfa_token":"…","code":"123456"}'
```

To view the QR without a browser:

```bash
python -c "import base64,sys; open('qr.png','wb').write(base64.b64decode(sys.argv[1]))" <qr_base64>
```

---

## Design decisions

**Encrypted secret at rest.** A DB dump alone should not hand over every user's second
factor. Cost: rotating `SECRET_KEY` orphans every existing secret and forces re-enrolment.
If you ever rotate it, plan a migration that clears `totp_secret`/`totp_enabled` and
notifies users, rather than leaving them unable to log in.

**The challenge token is deliberately not a JWT.** A short-lived access token carrying a
`scope: mfa` claim would still satisfy `@jwt_required()` on every other endpoint — a
half-authenticated caller could read transactions. An opaque random token stored hashed in
its own column cannot be confused for a session by anything.

**Rate limiting lives on the challenge, not the IP.** `mfa_attempts` increments per wrong
code and the challenge is destroyed at `MFA_MAX_ATTEMPTS`, forcing the attacker back through
the password step. This needs no Redis or extra infrastructure, which suits the current
deployment.

**`valid_window=1`.** Accepts the adjacent 30-second steps to tolerate phone clock drift.
Wider windows meaningfully enlarge the attack surface.

**Password required to disable.** A stolen access token should not be able to strip the
second factor.

**Uniform error text.** Expired and non-existent challenges return the same message, so the
endpoint does not confirm whether a challenge ever existed.

---

## Verified

`pytest` is not set up in this repo; verification was a standalone script exercising the
real crypto — 17 checks, all passing:

- encrypt/decrypt round-trip; ciphertext differs from plaintext and fits `String(255)`
- undecryptable ciphertext → `None`, no exception
- valid code accepted; wrong code, foreign-secret code, and whitespace-padded code handled
- previous 30s step accepted; a 5-minute-old code rejected
- backup code works once then fails; a second code still works; array empties correctly
- `NULL` and malformed `totp_backup_codes` return `False` rather than raising

The HTTP round-trip has not been run end-to-end — doing so means creating a user in the live
Neon database. Use the curl sequence above once you have a throwaway account.

---

## Parity warning

The Spring Boot backend in `spring-boot/` implements **none** of this. Its
`/api/auth/login` returns tokens immediately regardless of `totp_enabled`, so pointing the
frontend at it silently bypasses 2FA for every user who enabled it. Either port the feature
or keep that backend off the login path.

---

## Known gaps

- **No backup-code regeneration endpoint.** Once all 10 are consumed, the only path to more
  is disable + re-enable.
- **`/totp/activate` is not attempt-limited** the way `/login/verify` is. Lower risk since
  it requires a valid session, but it is an asymmetry.
- **One pending challenge per user.** Starting a second login invalidates the first.
- **No audit trail.** Enabling, disabling, and failed attempts are not recorded anywhere.
