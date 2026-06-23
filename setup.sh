#!/bin/bash
set -e

# ─────────────────────────────────────────────────────────────────────────────
# FinTrack Backend — Setup Script
# Run once: bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "🚀 FinTrack Backend Setup"
echo "─────────────────────────────────────────"

# ── 1. Detect Python ─────────────────────────────────────────────────────────
if command -v python3 &>/dev/null; then
    PYTHON_CMD=python3
elif command -v python &>/dev/null; then
    PYTHON_CMD=python
else
    echo "❌ Python is not installed. Please install Python 3.10+ to continue."
    exit 1
fi

PY_VERSION=$($PYTHON_CMD --version 2>&1)
echo "✅ Using $PY_VERSION"

# ── 4. Upgrade pip ───────────────────────────────────────────────────────────
echo "⬆️  Upgrading pip..."
pip install --upgrade pip --quiet

# ── 5. Install dependencies ───────────────────────────────────────────────────
if [ -f "requirements.txt" ]; then
    echo "📥 Installing dependencies from requirements.txt..."
    pip install -r requirements.txt --quiet
    echo "✅ Dependencies installed."
else
    echo "❌ requirements.txt not found!"
    exit 1
fi

# ── 6. Copy .env if not present ───────────────────────────────────────────────
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "📋 .env created from .env.example — please fill in your values."
    else
        echo "⚠️  No .env or .env.example found. Create a .env file before running."
    fi
else
    echo "✅ .env already exists."
fi

# ── 7. Run Alembic migrations ─────────────────────────────────────────────────
echo ""
echo "🗄️  Running database migrations..."
if flask --app run:app db upgrade 2>/dev/null; then
    echo "✅ Migrations applied."
else
    echo "⚠️  Migration failed — make sure PostgreSQL is running and DATABASE_URL is correct in .env"
fi

echo ""
echo "─────────────────────────────────────────"
echo "✅ Setup complete!"
echo "   Run the server with: bash run.sh"
echo "─────────────────────────────────────────"
