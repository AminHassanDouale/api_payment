"""End-to-end diagnostic. Run on the SERVER from /var/www/api.scolapp.com.

It connects to the same DB the app uses, lists every record relevant to
the webhook pipeline, simulates a webhook against a known-PENDING order,
and prints a one-line verdict for each layer.
"""
import json
import os
import time
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv
load_dotenv("/var/www/api.scolapp.com/.env")

from sqlalchemy import create_engine, text
from urllib.parse import quote_plus


def line(c="─", n=72):
    print(c * n)


def section(title):
    print()
    line()
    print(f"  {title}")
    line()


# ── Build DB URL same way main.py does ───────────────────────────────────
def build_db_url():
    explicit = os.getenv("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    conn = os.getenv("DB_CONNECTION", "").strip().lower()
    if conn == "mysql":
        u = os.getenv("DB_USERNAME", "root")
        p = os.getenv("DB_PASSWORD", "")
        h = os.getenv("DB_HOST", "127.0.0.1")
        port = os.getenv("DB_PORT", "3306")
        d = os.getenv("DB_DATABASE", "scolapp_payments")
        return f"mysql+pymysql://{u}:{quote_plus(p)}@{h}:{port}/{d}?charset=utf8mb4"
    return "sqlite:///./scolapp_payments.db"


DB_URL = build_db_url()
print(f"DB URL: {DB_URL.replace(quote_plus(os.getenv('DB_PASSWORD','')) or 'X', '***')}")
print(f"Today: {datetime.now().isoformat()}")

BASE = "http://127.0.0.1:8000"
APPID = "1598852445107200"

engine = create_engine(DB_URL)


# ── 1. App reachable + which version of /payment/notify is loaded? ──────
section("1. App reachability")
try:
    health = requests.get(f"{BASE}/health", timeout=5).json()
    print(f"  /health         : {health.get('status')}  db={health.get('database_ok')}")
    print(f"  PLATFORM_BASE   : {health.get('platform_base_url')}")
    print(f"  PLATFORM_NOTIFY : {health.get('platform_notify_url')}")
except Exception as e:
    print(f"  ❌ /health failed: {e}")
    sys.exit(1)

# Check GET /payment/notify exists (new code) vs returns 405 (old code)
r = requests.get(f"{BASE}/payment/notify?test=1", allow_redirects=False, timeout=5)
got_get = (r.status_code == 303)
print(f"  GET /payment/notify : HTTP {r.status_code}  "
      f"{'✓ new code loaded' if got_get else '❌ OLD CODE — `systemctl restart scolapp-api` needed'}")


# ── 2. Tables exist and have data? ─────────────────────────────────────
section("2. Database state")
with engine.connect() as conn:
    for tbl in ["admins", "third_parties", "payments",
                "payment_notifications", "webhook_deliveries", "activity_logs"]:
        try:
            n = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
            print(f"  {tbl:25} {n} rows")
        except Exception as e:
            print(f"  {tbl:25} ❌ {e}")


# ── 3. Third-party config (the Scolapp row) ────────────────────────────
section("3. Scolapp third-party config")
with engine.connect() as conn:
    row = conn.execute(text("""
        SELECT id, name, appid, merch_code, notify_url, redirect_url,
               token_preview, is_active
        FROM third_parties WHERE appid = :a
    """), {"a": APPID}).mappings().first()
    if not row:
        print(f"  ❌ NO third party found with appid={APPID}")
        print(f"     Run: /var/www/api.scolapp.com/venv/bin/python setup_scolapp.py")
    else:
        for k, v in dict(row).items():
            print(f"  {k:15} {v}")
        if str(row["redirect_url"]).strip().rstrip("/") != "https://api.scolapp.com/payment/notify":
            print(f"\n  ⚠ redirect_url is NOT pointing at /payment/notify.")
            print(f"     D-Money will redirect the browser elsewhere instead of triggering")
            print(f"     the webhook handler. Patch it via /admin/third-parties/<id>.")


# ── 4. Recent payments ─────────────────────────────────────────────────
section("4. Last 10 payments")
with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT id, third_party_id, merch_order_id, status, total_amount, currency, created_at
        FROM payments ORDER BY id DESC LIMIT 10
    """)).mappings().all()
    if not rows:
        print("  (no payments in DB yet — no order was ever created through this platform)")
    else:
        for r in rows:
            print(f"  #{r['id']:3} tp={r['third_party_id']} "
                  f"order={r['merch_order_id']:24} status={r['status']:10} "
                  f"{r['total_amount']} {r['currency']}  {r['created_at']}")


# ── 5. Recent notifications + their raw payloads ───────────────────────
section("5. Last 5 D-Money notifications stored")
with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT id, third_party_id, merch_order_id, trade_status, processed,
               total_amount, trans_currency, appid, received_at,
               LEFT(raw_payload, 200) AS payload_preview
        FROM payment_notifications ORDER BY id DESC LIMIT 5
    """)).mappings().all()
    if not rows:
        print("  (no notifications stored — webhook handler has never run for a real call)")
    else:
        for r in rows:
            print(f"\n  #{r['id']}  order={r['merch_order_id']}  status={r['trade_status']}  "
                  f"processed={r['processed']}  tp={r['third_party_id']}")
            print(f"     appid={r['appid']}  amount={r['total_amount']} {r['trans_currency']}")
            print(f"     received_at={r['received_at']}")
            print(f"     payload={r['payload_preview']}")


# ── 6. Webhook delivery attempts (forwards to third party) ─────────────
section("6. Last 5 webhook delivery attempts")
with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT id, third_party_id, target_url, attempt, status_code,
               delivered, error, created_at
        FROM webhook_deliveries ORDER BY id DESC LIMIT 5
    """)).mappings().all()
    if not rows:
        print("  (no delivery attempts — nothing has been forwarded yet)")
    else:
        for r in rows:
            print(f"  #{r['id']}  tp={r['third_party_id']}  attempt={r['attempt']}  "
                  f"HTTP={r['status_code']}  delivered={r['delivered']}  "
                  f"target={r['target_url']}  {r['created_at']}")
            if r['error']:
                print(f"     error: {r['error']}")


# ── 7. End-to-end smoke: simulate D-Money GET redirect on a PENDING order
section("7. End-to-end webhook simulation")
with engine.connect() as conn:
    pending = conn.execute(text("""
        SELECT merch_order_id FROM payments
        WHERE status = 'PENDING' ORDER BY id DESC LIMIT 1
    """)).scalar()

if not pending:
    print("  No PENDING payment to test against. Create one via payment.html first,")
    print("  then re-run this script.")
else:
    print(f"  Simulating PAY_SUCCESS for order: {pending}")
    pre = requests.get(f"{BASE}/api/payments", timeout=5,
                       headers={"Authorization": "Bearer x"})  # token doesn't matter for this probe
    # We want to call /payment/notify directly so no token needed
    r = requests.get(f"{BASE}/payment/notify", params={
        "merch_order_id":   pending,
        "payment_order_id": f"PO_DIAG_{int(time.time())}",
        "trade_status":     "PAY_SUCCESS",
        "appid":            APPID,
        "merch_code":       "200012",
        "total_amount":     "10.00",
        "trans_currency":   "DJF",
        "notify_time":      "2026-05-15 14:00:00",
        "trans_end_time":   "2026-05-15 14:00:00",
        "sign":             "diag",
        "sign_type":        "SHA256WithRSA",
    }, allow_redirects=False, timeout=10)
    print(f"  Response: HTTP {r.status_code}  Location={r.headers.get('Location')}")
    time.sleep(2)
    with engine.connect() as conn:
        after = conn.execute(text("""
            SELECT status FROM payments WHERE merch_order_id = :o
        """), {"o": pending}).scalar()
    print(f"  Status after simulated webhook: {after}")
    print(f"  Result: {'✓ webhook pipeline works' if after == 'PAID' else '❌ status did not update'}")


# ── 8. Verdict ─────────────────────────────────────────────────────────
section("8. Verdict / next step")
with engine.connect() as conn:
    n_notif = conn.execute(text("SELECT COUNT(*) FROM payment_notifications")).scalar()
    n_pay   = conn.execute(text("SELECT COUNT(*) FROM payments")).scalar()
    n_tp    = conn.execute(text("SELECT COUNT(*) FROM third_parties WHERE is_active=1")).scalar()

if not got_get:
    print("  → systemctl restart scolapp-api  (old code is still running)")
elif n_tp == 0:
    print("  → No active third party. Run setup_scolapp.py to register Scolapp.")
elif n_pay == 0:
    print("  → No payments yet. Use payment.html with the tp_live_ token to create one,")
    print("    then pay on D-Money. Webhook will land here.")
elif n_notif == 0:
    print("  → Payments exist but no notifications stored.")
    print("    Either D-Money has not called /payment/notify yet, or it called a")
    print("    different URL. Check: grep payment/notify /var/log/nginx/access.log | tail")
else:
    print("  → Notifications ARE stored. If admin UI 'Notifications' tab is empty,")
    print("    hard-refresh the page (Ctrl+Shift+R). Otherwise the data is there.")
