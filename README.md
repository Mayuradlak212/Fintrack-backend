# FinTrack Backend

Flask REST API with PostgreSQL, SQLAlchemy, Alembic migrations, Pydantic v2 validation, and JWT authentication.

## Folder Structure

```
backend/
├── app/
│   ├── __init__.py          # App factory
│   ├── api/
│   │   ├── auth.py          # POST /api/auth/register|login|refresh, GET /api/auth/me
│   │   └── transactions.py  # GET|POST /api/transactions, GET|PATCH|DELETE /api/transactions/:id
│   ├── core/
│   │   ├── config.py        # Pydantic Settings (env vars)
│   │   ├── database.py      # SQLAlchemy db + Migrate instances
│   │   └── security.py      # bcrypt + JWT helpers
│   ├── models/
│   │   ├── user.py          # User SQLAlchemy model
│   │   └── transaction.py   # Transaction SQLAlchemy model
│   ├── schemas/
│   │   ├── user.py          # Pydantic request/response schemas
│   │   └── transaction.py   # Pydantic request/response schemas
│   └── services/
│       ├── auth_service.py        # Register, login logic
│       └── transaction_service.py # CRUD + summary logic
├── migrations/              # Alembic auto-generated migrations
├── run.py                   # Entrypoint
├── alembic.ini
├── requirements.txt
└── .env.example
```

## Setup

### 1. Create a virtual environment

```bash
cd backend
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your PostgreSQL credentials and secret keys
```

### 4. Create the database

```sql
-- In psql
CREATE DATABASE finance_tracker;
```

### 5. Run migrations

```bash
# Generate initial migration (first time)
flask --app run:app db migrate -m "initial schema"

# Apply migrations
flask --app run:app db upgrade
```

### 6. Start the development server

```bash
python run.py
# or
flask --app run:app run --debug
```

Server runs at **http://localhost:5000**

---

## API Reference

### Auth

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/auth/register` | ❌ | Register new user |
| POST | `/api/auth/login` | ❌ | Login, get tokens |
| POST | `/api/auth/refresh` | Refresh token | Rotate access token |
| GET | `/api/auth/me` | Access token | Get current user |

### Transactions

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/transactions` | ✅ | List (paginated + filtered) |
| POST | `/api/transactions` | ✅ | Create transaction |
| GET | `/api/transactions/:id` | ✅ | Get single |
| PATCH | `/api/transactions/:id` | ✅ | Partial update |
| DELETE | `/api/transactions/:id` | ✅ | Delete |
| GET | `/api/transactions/summary` | ✅ | Balance + totals |

### Query params for list

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `page` | int | 1 | Page number |
| `per_page` | int | 20 | Items per page |
| `type` | string | - | Filter: `credit` or `debit` |
| `category` | string | - | Filter by category name |

---

## Production

```bash
gunicorn -w 4 -b 0.0.0.0:5000 run:app
```
