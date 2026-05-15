"""Verify the status-mapping fix handles real D-Money webhooks (PAY_SUCCESS etc.)."""
import json
import time
import requests

BASE = "http://127.0.0.1:8000"

# Login + find Scolapp's token (we rotate so we know it)
jwt = requests.post(f"{BASE}/admin/login",
                    json={"email": "admin@gmail.com", "password": "password"}).json()["access_token"]
H = {"Authorization": f"Bearer {jwt}"}
tps = requests.get(f"{BASE}/admin/third-parties", headers=H).json()
scolapp = next((t for t in tps if t["appid"] == "1598852445107200" and t["is_active"]), None)
if not scolapp:
    print("Scolapp not found — run setup_scolapp.py first")
    raise SystemExit(1)
tp_id = scolapp["id"]
print(f"Scolapp third-party id={tp_id}")

# Rotate token so we have it
r = requests.post(f"{BASE}/admin/third-parties/{tp_id}/rotate-token", headers=H).json()
TOKEN = r["api_token"]
TH = {"Authorization": f"Bearer {TOKEN}"}
print(f"Token: {TOKEN[:24]}...")

# Create a fresh payment
resp = requests.post(f"{BASE}/api/payments", headers=TH,
                  json={"amount": 10, "description": "Status-fix test"})
r = resp.json()
if "order_id" not in r:
    print(f"Payment create failed: HTTP{resp.status_code} {r}")
    raise SystemExit(1)
order = r["order_id"]
print(f"\nCreated payment: order={order} status={r['status']}")

# ── Simulate real D-Money webhook with PAY_SUCCESS (the value they actually send) ──
fake = {
    "trans_end_time":   "2026-05-15 14:30:00",
    "notify_time":      "2026-05-15 14:30:00",
    "trans_currency":   "DJF",
    "total_amount":     "10.00",
    "merch_order_id":   order,
    "appid":            "1598852445107200",
    "trade_status":     "PAY_SUCCESS",            # <-- real D-Money value
    "merch_code":       "200012",
    "callback_info":    "oid-99_uid-1",
    "notify_url":       "https://api.scolapp.com/payment/notify",
    "payment_order_id": f"PO{int(time.time())}",
    "sign":             "fake-signature-for-test",
    "sign_type":        "SHA256WithRSA",
}
print("\nSending PAY_SUCCESS webhook to /payment/notify ...")
resp = requests.post(f"{BASE}/payment/notify", json=fake)
print(f"  HTTP {resp.status_code}")
print(f"  Response: {resp.json()}")

# Wait for status update
time.sleep(2)

# Check payment status
detail = requests.get(f"{BASE}/api/payments/{order}", headers=TH).json()
print(f"\nPayment status after webhook: {detail['status']}")
assert detail["status"] == "PAID", f"Expected PAID, got {detail['status']}"
print("[OK] PAY_SUCCESS -> PAID")

# Also test the other documented variants
print("\nTesting all documented D-Money status values...")
for raw, expected in [
    ("PAY_SUCCESS", "PAID"),
    ("Completed",   "PAID"),
    ("PAYED",       "PAID"),
    ("PAYING",      "PAYING"),
    ("Failure",     "FAILED"),
    ("Expired",     "EXPIRED"),
]:
    # Create + webhook
    r = requests.post(f"{BASE}/api/payments", headers=TH,
                      json={"amount": 10, "description": f"Test {raw}"}).json()
    o = r["order_id"]
    requests.post(f"{BASE}/payment/notify", json={
        **fake, "merch_order_id": o, "trade_status": raw,
        "payment_order_id": f"PO{int(time.time()*1000)}",
    })
    time.sleep(0.5)
    p = requests.get(f"{BASE}/api/payments/{o}", headers=TH).json()
    ok = p["status"] == expected
    icon = "[OK]" if ok else "[FAIL]"
    print(f"  {icon} {raw:14} -> {p['status']:10} (expected {expected})")

# Check forwarded webhook log
print("\nLatest webhook deliveries:")
deliveries = requests.get(f"{BASE}/admin/webhooks/deliveries?third_party_id={tp_id}",
                          headers=H).json()
for d in deliveries[:5]:
    print(f"  attempt={d['attempt']} http={d['status_code']} "
          f"delivered={d['delivered']} target={d['target_url']}")

# Final response format check
print(f"\nD-Money ack format: {requests.post(f'{BASE}/payment/notify', json={}).json()}")
print("(D-Money docs expect: {code, msg, result})")
