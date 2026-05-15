"""One-shot script: refresh every PENDING payment by hitting D-Money's
queryOrder API. Use this when you've paid on D-Money but the webhook
never arrived (e.g. D-Money TEST sandbox doesn't always POST).

Logs in as admin@gmail.com / password, rotates each third-party's token
just long enough to hit GET /api/payments/{order_id} (which internally
calls D-Money's queryOrder and updates the local status).
"""
import requests
import sys
import time

BASE = "http://127.0.0.1:8000"


def main():
    print("Logging in as admin@gmail.com ...")
    r = requests.post(
        f"{BASE}/admin/login",
        json={"email": "admin@gmail.com", "password": "password"},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"  FAILED: HTTP{r.status_code} {r.text}")
        sys.exit(1)
    jwt = r.json()["access_token"]
    H = {"Authorization": f"Bearer {jwt}"}

    tps = requests.get(f"{BASE}/admin/third-parties", headers=H, timeout=10).json()
    print(f"Found {len(tps)} third party records")

    total_refreshed = 0
    total_flipped   = 0
    for tp in tps:
        if not tp.get("is_active"):
            continue
        tp_id = tp["id"]
        name  = tp["name"]

        # Rotate token (we need a valid live token for the per-TP API)
        rotated = requests.post(
            f"{BASE}/admin/third-parties/{tp_id}/rotate-token",
            headers=H, timeout=10,
        ).json()
        token = rotated["api_token"]
        TH = {"Authorization": f"Bearer {token}"}

        # Pull the TP's PENDING payments
        pending = requests.get(
            f"{BASE}/api/payments?status=PENDING&limit=500",
            headers=TH, timeout=15,
        ).json()
        print(f"\n[TP #{tp_id} - {name}]  PENDING: {len(pending)}")

        for p in pending:
            order_id = p["order_id"]
            # Hitting the detail endpoint triggers a live queryOrder for PENDING
            try:
                d = requests.get(
                    f"{BASE}/api/payments/{order_id}",
                    headers=TH, timeout=20,
                ).json()
                before = p["status"]
                after  = d.get("status", "?")
                marker = "->" if before != after else "=="
                print(f"  {order_id:24} {before:8} {marker} {after}")
                total_refreshed += 1
                if before != after and after == "PAID":
                    total_flipped += 1
            except Exception as e:
                print(f"  {order_id:24} ERROR {e}")
            time.sleep(0.3)  # be polite to D-Money

    print()
    print("=" * 60)
    print(f"  Refreshed: {total_refreshed}  ·  Flipped to PAID: {total_flipped}")
    print("=" * 60)


if __name__ == "__main__":
    main()
