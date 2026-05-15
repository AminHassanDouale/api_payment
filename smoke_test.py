"""End-to-end smoke test of every endpoint."""
import json
import re
import sys
import time
import requests

BASE = "http://127.0.0.1:8000"
results = []


def t(label, ok, detail=""):
    icon = "[OK]" if ok else "[FAIL]"
    results.append((label, ok, detail))
    print(f"{icon} {label}" + (f"  -- {detail}" if detail else ""))


def req(method, path, expect, **kw):
    r = requests.request(method, BASE + path, timeout=15, **kw)
    ok = r.status_code == expect
    try:
        body = r.json()
    except Exception:
        body = r.text[:120]
    return r, body, ok


print("\n=== 1. SYSTEM ===")
r, b, ok = req("GET", "/health", 200)
t("/health", ok and b.get("database_ok"), f"db={b.get('database_ok')}")

r, b, ok = req("GET", "/", 200)
t("/ root", ok and b.get("service"))

r, b, ok = req("GET", "/docs", 200)
t("/docs", ok)

r, b, ok = req("GET", "/openapi.json", 200)
t("/openapi.json", ok, f"{len(b.get('paths', {}))} paths")

print("\n=== 2. ADMIN AUTH ===")
r, b, ok = req("GET", "/admin/me", 403)
t("403 unauthorized on /admin/me", ok)

r, b, ok = req("POST", "/admin/login", 200,
               json={"email": "admin@gmail.com", "password": "password"})
t("/admin/login", ok)
JWT = b["access_token"]
H = {"Authorization": f"Bearer {JWT}"}

r, b, ok = req("GET", "/admin/me", 200, headers=H)
t("/admin/me (authed)", ok, b.get("email"))

r, b, ok = req("POST", "/admin/login", 401,
               json={"email": "admin@gmail.com", "password": "wrong"})
t("/admin/login wrong password -> 401", ok)

print("\n=== 3. THIRD-PARTY CRUD ===")
with open(".env") as f:
    content = f.read()
m = re.search(r'DMONEY_PRIVATE_KEY="([^"]+)"', content, re.S)
privkey = m.group(1) if m else None
assert privkey, "Could not find private key in .env"

# Use a unique appid in case earlier test runs left one
unique_appid = f"TEST{int(time.time())}"

tp_payload = {
    "name": "Acme Test",
    "company": "Acme Corp",
    "email": "ops@acme.com",
    "phone": "+25377000000",
    "appid": unique_appid,
    "app_key": "452fe2b7-4105-4fc8-b002-937d10a970b1",
    "app_secret": "d5d750fdcf550a6ae2f4fd15acd1357a",
    "private_key": privkey,
    "merch_code": "200012",
    "business_type": "OnlineMerchant",
    "dmoney_base_url": "https://pgtest.d-money.dj:38443",
    "dmoney_query_base_url": "https://pgtest.d-money.dj:38443",
    "dmoney_checkout_base_url": "https://pgtest.d-money.dj:38443/payment/web/paygate",
    "notify_url":   "https://acme.test/webhook",
    "redirect_url": "https://acme.test/done",
}
r, b, ok = req("POST", "/admin/third-parties", 201, headers=H, json=tp_payload)
t("create third party", ok, f"id={b.get('id') if ok else b}")
if not ok:
    print("Cannot continue without a third party. Aborting.")
    sys.exit(1)
TP_ID = b["id"]
TP_TOKEN = b["api_token"]
print(f"   token (one-shot): {TP_TOKEN[:24]}...")

r, b, ok = req("POST", "/admin/third-parties", 409, headers=H, json=tp_payload)
t("duplicate appid -> 409", ok)

bad = {**tp_payload, "appid": unique_appid + "X", "private_key": "not-a-key"}
r, b, ok = req("POST", "/admin/third-parties", 422, headers=H, json=bad)
t("invalid private_key -> 422", ok)

r, b, ok = req("GET", "/admin/third-parties", 200, headers=H)
t("list third parties", ok and len(b) >= 1, f"count={len(b)}")

r, b, ok = req("GET", f"/admin/third-parties/{TP_ID}", 200, headers=H)
t(f"get third party {TP_ID}", ok, b.get("name"))

r, b, ok = req("PATCH", f"/admin/third-parties/{TP_ID}", 200, headers=H,
               json={"company": "Acme - Updated"})
t("patch third party", ok and b["company"] == "Acme - Updated")

print("\n=== 4. THIRD-PARTY API (tp_live_ token) ===")
TH = {"Authorization": f"Bearer {TP_TOKEN}"}

r, b, ok = req("GET", "/api/me", 200, headers=TH)
t("/api/me", ok, b.get("name"))

r, b, ok = req("GET", "/api/me", 401, headers={"Authorization": "Bearer tp_live_INVALID"})
t("invalid token -> 401", ok)

# XOR validation (no network)
r, b, ok = req("POST", "/api/payments", 422, headers=TH,
               json={"amount": 100, "description": "x", "items": [{"amount": 1, "description": "y"}]})
t("XOR validation single+bulk -> 422", ok)

r, b, ok = req("POST", "/api/payments", 422, headers=TH, json={})
t("empty body -> 422", ok)

# Live payment call hits D-Money sandbox — count any 2xx OR a gateway error (502) as 'route works'
print("   (Calling D-Money sandbox; gateway errors are OK to see)")
r = requests.post(BASE + "/api/payments", headers=TH, timeout=30,
                  json={"amount": 1000, "description": "Test single"})
try:
    body = r.json()
except Exception:
    body = r.text[:200]
created = (r.status_code == 200)
if created:
    t("create payment (single, live)", True, f"order={body.get('order_id')}")
    ORDER = body["order_id"]
else:
    t("create payment route reachable (gateway may be unreachable)",
      r.status_code in (200, 502), f"HTTP{r.status_code}")
    ORDER = None

r = requests.post(BASE + "/api/payments", headers=TH, timeout=30,
                  json={"items": [{"amount": 500, "description": "A"},
                                  {"amount": 300, "description": "B"}]})
t("create payment (bulk, live)",
  r.status_code in (200, 502), f"HTTP{r.status_code}")

r, b, ok = req("GET", "/api/payments", 200, headers=TH)
t("list my payments", ok, f"count={len(b)}")

if ORDER:
    r, b, ok = req("GET", f"/api/payments/{ORDER}", 200, headers=TH)
    t(f"get my payment {ORDER}", ok, b.get("status"))

r, b, ok = req("GET", "/api/activity", 200, headers=TH)
t("/api/activity (TP self-audit)", ok, f"total={b.get('total')}")

print("\n=== 5. WEBHOOK ===")
fake_webhook = {
    "appid": unique_appid,
    "merch_order_id": ORDER or "TESTFAKE001",
    "payment_order_id": f"PO{int(time.time())}",
    "trade_status": "PAYED",
    "total_amount": "1000",
    "trans_currency": "DJF",
    "merch_code": "200012",
    "notify_time": "20260515120000",
    "trans_end_time": "20260515120010",
}
r, b, ok = req("POST", "/payment/notify", 200, json=fake_webhook)
t("POST /payment/notify (simulated D-Money)", ok and b.get("returnCode") == "SUCCESS")

r, b, ok = req("POST", "/payment/notify", 200, json=fake_webhook)
t("idempotent: duplicate notify still 200", ok)

time.sleep(2)

print("\n=== 6. ADMIN MONITORING ===")
r, b, ok = req("GET", "/admin/payments", 200, headers=H)
t("/admin/payments", ok, f"count={len(b)}")

r, b, ok = req("GET", "/admin/webhooks/deliveries", 200, headers=H)
t("/admin/webhooks/deliveries", ok, f"count={len(b)}")

r, b, ok = req("GET", "/admin/activity?limit=50", 200, headers=H)
t("/admin/activity", ok, f"total={b.get('total')}")

r, b, ok = req("GET", f"/admin/third-parties/{TP_ID}/activity", 200, headers=H)
t(f"/admin/third-parties/{TP_ID}/activity", ok, f"total={b.get('total')}")

r, b, ok = req("GET", "/admin/activity?action=webhook_received", 200, headers=H)
t("activity filter action=webhook_received", ok, f"count={len(b.get('items', []))}")

print("\n=== 7. ROTATE + DELETE ===")
r, b, ok = req("POST", f"/admin/third-parties/{TP_ID}/rotate-token", 200, headers=H)
t("rotate token", ok and b.get("api_token", "").startswith("tp_live_"))
NEW_TOKEN = b["api_token"]

r, b, ok = req("GET", "/api/me", 401, headers=TH)
t("old token after rotate -> 401", ok)

r, b, ok = req("GET", "/api/me", 200, headers={"Authorization": f"Bearer {NEW_TOKEN}"})
t("new token works", ok)

r, b, ok = req("DELETE", f"/admin/third-parties/{TP_ID}", 200, headers=H)
t("soft delete TP", ok and b.get("deleted") and not b.get("hard"))

r, b, ok = req("GET", "/api/me", 401, headers={"Authorization": f"Bearer {NEW_TOKEN}"})
t("disabled TP token -> 401", ok)

print("\n=== SUMMARY ===")
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\n  {passed}/{total} checks passed")
if passed < total:
    print("\n  Failures:")
    for label, ok, detail in results:
        if not ok:
            print(f"    [FAIL] {label}  {detail}")
    sys.exit(1)
print("\n  All endpoints working.")
