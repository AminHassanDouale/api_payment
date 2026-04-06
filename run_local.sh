#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# run_local.sh — Run the API locally for testing
# Usage: bash run_local.sh
# ──────────────────────────────────────────────────────────────────────────────

set -e

echo ""
echo "======================================================"
echo "  D-Money API — LOCAL TEST SERVER"
echo "======================================================"

# ── Check .env exists ─────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo ""
    echo "  ERROR: .env file not found!"
    echo "  Copy the example and fill in your credentials:"
    echo "    cp .env.example .env"
    echo "    nano .env"
    echo ""
    exit 1
fi

# ── Create virtualenv if missing ──────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo ""
    echo "  Creating virtual environment..."
    python3 -m venv venv
fi

# ── Install / update dependencies ─────────────────────────────────────────────
echo "  Installing dependencies..."
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q
echo "  Dependencies ready."

# ── Start server ──────────────────────────────────────────────────────────────
echo ""
echo "  Starting API server..."
echo ""
echo "  Local URLs:"
echo "    Health : http://localhost:8000/health"
echo "    Docs   : http://localhost:8000/docs"
echo "    Create : POST http://localhost:8000/payment/create"
echo "    Query  : POST http://localhost:8000/payment/query"
echo "    Token  : GET  http://localhost:8000/payment/token"
echo ""
echo "  Press Ctrl+C to stop"
echo "======================================================"
echo ""

# Load .env and start uvicorn with auto-reload for development
set -a && source .env && set +a
venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --reload