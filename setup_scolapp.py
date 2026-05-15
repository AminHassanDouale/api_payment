"""One-shot registration of the Scolapp third party using the real D-Money TEST
credentials. Outputs the lifetime API token to paste into payment.html.
"""
import requests, sys

BASE = "http://127.0.0.1:8000"
ADMIN_EMAIL = "admin@gmail.com"
ADMIN_PASSWORD = "password"

# ── D-Money TEST credentials (provided by the user) ──────────────────────────
APPID         = "1598852445107200"
MERCH_CODE    = "200012"
APP_KEY       = "452fe2b7-4105-4fc8-b002-937d10a970b1"
APP_SECRET    = "d5d750fdcf550a6ae2f4fd15acd1357a"
NOTIFY_URL    = "https://api.scolapp.com/payment/notify"   # where THIS platform forwards events to Scolapp
REDIRECT_URL  = "https://scolapp.com"

PRIVATE_KEY = """\
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
A2RvMiPjgMiB5pO8pdLW9hI"""


def main():
    print("Logging in as admin...")
    r = requests.post(f"{BASE}/admin/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                      timeout=10)
    r.raise_for_status()
    jwt = r.json()["access_token"]
    H = {"Authorization": f"Bearer {jwt}"}

    # If a TP with this appid already exists, hard-delete it (we want a fresh
    # token for testing).
    print("Checking for existing Scolapp third party...")
    existing = requests.get(f"{BASE}/admin/third-parties", headers=H, timeout=10).json()
    for tp in existing:
        if tp["appid"] == APPID:
            print(f"  Found existing TP id={tp['id']} — hard-deleting for clean test")
            requests.delete(f"{BASE}/admin/third-parties/{tp['id']}?hard=true",
                            headers=H, timeout=10).raise_for_status()
            break

    print("Registering Scolapp third party...")
    payload = {
        "name":     "Scolapp",
        "company":  "Scolapp SARL",
        "email":    "ops@scolapp.com",
        "phone":    "+25377000000",
        "appid":         APPID,
        "app_key":       APP_KEY,
        "app_secret":    APP_SECRET,
        "private_key":   PRIVATE_KEY,
        "merch_code":    MERCH_CODE,
        "business_type": "OnlineMerchant",
        "dmoney_base_url":          "https://pgtest.d-money.dj:38443",
        "dmoney_query_base_url":    "https://pgtest.d-money.dj:38443",
        "dmoney_checkout_base_url": "https://pgtest.d-money.dj:38443/payment/web/paygate",
        "notify_url":   NOTIFY_URL,
        "redirect_url": REDIRECT_URL,
    }
    r = requests.post(f"{BASE}/admin/third-parties", headers=H, json=payload, timeout=15)
    if r.status_code != 201:
        print("FAILED:", r.status_code, r.text)
        sys.exit(1)
    data = r.json()
    token = data["api_token"]

    print()
    print("=" * 72)
    print("  SCOLAPP REGISTERED")
    print("=" * 72)
    print(f"  Third-party ID : {data['id']}")
    print(f"  Name           : {data['name']}")
    print(f"  App ID         : {data['appid']}")
    print(f"  Merch code     : {data['merch_code']}")
    print(f"  Notify URL     : {data['notify_url']}")
    print(f"  Redirect URL   : {data['redirect_url']}")
    print()
    print("  LIFETIME API TOKEN (copy now — won't be shown again):")
    print(f"\n    {token}\n")
    print("=" * 72)
    print()
    print("Next steps:")
    print("  1. Open http://localhost:8000/payment.html")
    print("  2. Paste the token above into the 'API Token' field")
    print("  3. Leave amount=10 / description='Test payment 10 DJF'")
    print("  4. Click 'Pay Now' → get a real D-Money checkout URL")
    print("  5. Click 'Open Payment Page' → complete the payment on D-Money")
    print()
    print("  After payment, D-Money will fire a webhook to:")
    print("    https://api.scolapp.com/payment/notify")
    print("  (your production platform URL). Local server won't see it unless")
    print("  you tunnel with ngrok and set PLATFORM_BASE_URL accordingly.")
    print()


if __name__ == "__main__":
    main()
