#!/bin/bash
set -e

# ─────────────────────────────────────────────────────────────────────────────
# FinTrack Backend — Run Script
# Usage: bash run.sh [dev|prod]
# Default: dev
# ─────────────────────────────────────────────────────────────────────────────

MODE=${1:-dev}

echo ""
echo "🚀 Starting FinTrack Backend [$MODE mode]"
echo "─────────────────────────────────────────"



# ── Check .env ────────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo "❌ .env file not found. Copy .env.example to .env and fill in your values."
    exit 1
fi

# ── Load environment variables ────────────────────────────────────────────────
export $(grep -v '^#' .env | grep -v '^$' | xargs)
echo "✅ Environment variables loaded."

# ── Run ───────────────────────────────────────────────────────────────────────
if [ "$MODE" = "prod" ]; then
    WORKERS=${GUNICORN_WORKERS:-4}
    PORT=${PORT:-5000}
    echo "🌐 Starting Gunicorn — $WORKERS workers on port $PORT"
    echo "─────────────────────────────────────────"
    exec gunicorn \
        --workers "$WORKERS" \
        --bind "0.0.0.0:$PORT" \
        --access-logfile - \
        --error-logfile - \
        run:app
else
    PORT=${PORT:-5000}
    echo "🔧 Starting Flask dev server on http://localhost:$PORT"
    echo "─────────────────────────────────────────"
    exec python run.py
fi
