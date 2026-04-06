"""
D-Money Payment Gateway Integration
Test URL:       https://pgtest.d-money.dj:38443
Production API: https://api.scolapp.com
"""

import os, json, time, base64, secrets, string, logging, urllib.parse
from typing import Dict, Optional
from datetime import datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
import requests, urllib3

_DEFAULTS = {
    "DMONEY_BASE_URL":      "https://pgtest.d-money.dj:38443",
    "DMONEY_X_APP_KEY":     "452fe2b7-4105-4fc8-b002-937d10a970b1",
    "DMONEY_APP_SECRET":    "d5d750fdcf550a6ae2f4fd15acd1357a",
    "DMONEY_APPID":         "1598852445107200",
    "DMONEY_MERCH_CODE":    "200012",
    "DMONEY_BUSINESS_TYPE": "OnlineMerchant",
    "DMONEY_NOTIFY_URL":    "https://api.scolapp.com/payment/notify",
    "DMONEY_REDIRECT_URL":  "https://scolapp.com/payment/success",
    "DMONEY_VERIFY_SSL":    "false",
    "DMONEY_TIMEOUT_SEC":   "30",
    "DMONEY_LOG_LEVEL":     "INFO",
}

_PRIVATE_KEY_B64 = (
    "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"
    "A2RvMiPjgMiB5pO8pdLW9hI"
)


def _cfg(key):
    return os.getenv(key, _DEFAULTS.get(key, "")).strip()


logging.basicConfig(level=getattr(logging, _cfg("DMONEY_LOG_LEVEL"), logging.INFO),
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DmoneyGateway")


class DmoneyPaymentGateway:

    GATEWAY_PATH          = "/apiaccess/payment/gateway"
    TOKEN_PATH            = "/payment/v1/token"
    PREORDER_PATH         = "/payment/v1/merchant/preOrder"
    QUERY_ORDER_PATH      = "/payment/v1/merchant/queryOrder"
    DEFAULT_CHECKOUT_BASE = "https://pgtest.d-money.dj:38443/payment/web/paygate"

    _EXPIRY_FORMATS = ["%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S",
                       "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"]

    def __init__(self):
        self.base_url         = _cfg("DMONEY_BASE_URL").rstrip("/")
        self.x_app_key        = _cfg("DMONEY_X_APP_KEY")
        self.app_secret       = _cfg("DMONEY_APP_SECRET")
        self.verify_ssl       = _cfg("DMONEY_VERIFY_SSL").lower() == "true"
        self.appid            = _cfg("DMONEY_APPID")
        self.merch_code       = _cfg("DMONEY_MERCH_CODE")
        self.business_type    = _cfg("DMONEY_BUSINESS_TYPE") or "OnlineMerchant"
        self.notify_url       = _cfg("DMONEY_NOTIFY_URL")
        self.redirect_url     = _cfg("DMONEY_REDIRECT_URL")
        self.checkout_base_url = _cfg("DMONEY_CHECKOUT_BASE_URL") or self.DEFAULT_CHECKOUT_BASE

        missing = [k for k, v in {
            "DMONEY_BASE_URL": self.base_url, "DMONEY_X_APP_KEY": self.x_app_key,
            "DMONEY_APP_SECRET": self.app_secret, "DMONEY_APPID": self.appid,
            "DMONEY_MERCH_CODE": self.merch_code, "DMONEY_NOTIFY_URL": self.notify_url,
            "DMONEY_REDIRECT_URL": self.redirect_url,
        }.items() if not v]
        if missing:
            raise ValueError(f"Missing required config keys: {', '.join(missing)}")

        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            logger.warning("SSL verification is DISABLED")

        self._load_private_key()
        self.token: Optional[str] = None
        self.token_expiry: Optional[float] = None
        logger.info(f"Gateway ready | base={self.base_url} | merch={self.merch_code}")

    def _mask(self, v, head=8, tail=4):
        return v if not v or len(v) <= head+tail else f"{v[:head]}...{v[-tail:]}"

    def _api_url(self, path):
        base = self.base_url
        if self.GATEWAY_PATH in base:
            base = base[:base.index(self.GATEWAY_PATH)]
        return f"{base}{self.GATEWAY_PATH}{path if path.startswith('/') else '/'+path}"

    def _parse_expiry(self, s):
        for fmt in self._EXPIRY_FORMATS:
            try: return datetime.strptime(s.strip(), fmt).timestamp()
            except ValueError: pass
        try:
            from dateutil import parser as dp
            return dp.parse(s).timestamp()
        except Exception: pass
        logger.warning(f"Cannot parse expirationDate '{s}' — using +1h")
        return time.time() + 3600

    def _load_private_key(self):
        b64 = os.getenv("DMONEY_PRIVATE_KEY_B64", "").strip() or _PRIVATE_KEY_B64
        try:
            self.private_key = serialization.load_der_private_key(
                base64.b64decode(b64), password=None, backend=default_backend())
            logger.info("RSA private key loaded OK")
        except Exception as e:
            raise ValueError(f"Failed to load private key: {e}") from e

    def _generate_order_id(self):
        return "ORD" + datetime.now().strftime("%Y%m%d%H%M%S") + \
               "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))

    def _nonce(self, n=32):
        return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(n))

    def _timestamp(self):
        return str(int(time.time()))

    def _signing_string(self, params):
        exclude = {"sign", "sign_type", "biz_content"}
        items = sorted((k, str(v)) for k, v in params.items()
                       if k not in exclude and v is not None and str(v).strip())
        return "&".join(f"{k}={v}" for k, v in items)

    def _sign(self, params):
        sig = self.private_key.sign(
            self._signing_string(params).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256())
        return base64.b64encode(sig).decode()

    def _ensure_token(self):
        if self.token and self.token_expiry and time.time() < self.token_expiry - 60:
            return
        self.get_token()

    def _timeout(self):
        return int(_cfg("DMONEY_TIMEOUT_SEC") or 30)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_token(self) -> Dict:
        url  = self._api_url(self.TOKEN_PATH)
        resp = requests.post(url,
            json={"appSecret": self.app_secret},
            headers={"Content-Type": "application/json", "X-APP-Key": self.x_app_key},
            verify=self.verify_ssl, timeout=self._timeout())
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorCode"):
            raise RuntimeError(f"Token error [{data['errorCode']}]: {data.get('errorMsg')}")
        self.token = data["token"]
        expiry = data.get("expirationDate")
        self.token_expiry = self._parse_expiry(expiry) if expiry else time.time() + 3600
        logger.info(f"Token acquired — TTL {int(self.token_expiry - time.time())}s")
        return data

    def create_preorder(self, amount: float, title: str,
                        order_id: Optional[str] = None, currency: str = "DJF",
                        timeout: str = "120m", notify_url: Optional[str] = None,
                        redirect_url: Optional[str] = None) -> Dict:
        self._ensure_token()
        order_id     = order_id or self._generate_order_id()
        notify_url   = (notify_url   or self.notify_url).strip()
        redirect_url = (redirect_url or self.redirect_url).strip()

        if not order_id.isalnum():
            raise ValueError("order_id must be alphanumeric only")

        nonce_str = self._nonce()
        timestamp = self._timestamp()

        sign_params = {
            "appid": self.appid, "business_type": self.business_type,
            "merch_code": self.merch_code, "merch_order_id": order_id,
            "method": "payment.preorder", "nonce_str": nonce_str,
            "notify_url": notify_url, "redirect_url": redirect_url,
            "timeout_express": timeout, "timestamp": timestamp,
            "title": title, "total_amount": str(int(amount)),
            "trade_type": "Checkout", "trans_currency": currency, "version": "1.0",
        }

        payload = {
            "nonce_str": nonce_str, "method": "payment.preorder",
            "version": "1.0", "sign_type": "SHA256WithRSA",
            "timestamp": timestamp, "sign": self._sign(sign_params),
            "biz_content": {
                "appid": self.appid, "merch_code": self.merch_code,
                "merch_order_id": order_id, "business_type": self.business_type,
                "trade_type": "Checkout", "trans_currency": currency,
                "total_amount": str(int(amount)), "timeout_express": timeout,
                "title": title, "notify_url": notify_url, "redirect_url": redirect_url,
            },
        }

        resp = requests.post(self._api_url(self.PREORDER_PATH), json=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": self.token, "X-APP-Key": self.x_app_key},
            verify=self.verify_ssl, timeout=self._timeout())

        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"Non-JSON response ({resp.status_code}): {resp.text}")

        if resp.status_code != 200:
            raise RuntimeError(f"PreOrder failed [{resp.status_code}]: {data.get('errorMsg', resp.text)}")
        if data.get("errorCode"):
            raise RuntimeError(f"PreOrder error [{data['errorCode']}]: {data.get('errorMsg')} -- {data.get('errorSolution','')}")

        logger.info(f"PreOrder OK — order={order_id} amount={int(amount)} {currency}")
        return data

    def query_order(self, merch_order_id: Optional[str] = None,
                    trade_no: Optional[str] = None) -> Dict:
        if not merch_order_id and not trade_no:
            raise ValueError("Provide merch_order_id or trade_no")
        self._ensure_token()

        nonce_str = self._nonce()
        timestamp = self._timestamp()
        sign_params = {"appid": self.appid, "merch_code": self.merch_code,
                       "method": "payment.queryorder", "nonce_str": nonce_str,
                       "timestamp": timestamp, "version": "1.0"}
        biz = {"appid": self.appid, "merch_code": self.merch_code}
        if merch_order_id:
            sign_params["merch_order_id"] = merch_order_id
            biz["merch_order_id"] = merch_order_id
        if trade_no:
            sign_params["trade_no"] = trade_no
            biz["trade_no"] = trade_no

        payload = {"nonce_str": nonce_str, "method": "payment.queryorder",
                   "version": "1.0", "sign_type": "SHA256WithRSA",
                   "timestamp": timestamp, "sign": self._sign(sign_params),
                   "biz_content": biz}

        resp = requests.post(self._api_url(self.QUERY_ORDER_PATH), json=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": self.token, "X-APP-Key": self.x_app_key},
            verify=self.verify_ssl, timeout=self._timeout())

        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"Non-JSON response ({resp.status_code}): {resp.text}")

        if resp.status_code != 200:
            raise RuntimeError(f"QueryOrder failed [{resp.status_code}]: {data.get('errorMsg', resp.text)}")
        if data.get("errorCode"):
            raise RuntimeError(f"QueryOrder error [{data['errorCode']}]: {data.get('errorMsg')}")

        logger.info("QueryOrder OK")
        return data

    def generate_checkout_url(self, prepay_id: str, language: str = "en") -> str:
        nonce_str = self._nonce()
        timestamp = self._timestamp()
        sign_params = {"appid": self.appid, "merch_code": self.merch_code,
                       "nonce_str": nonce_str, "prepay_id": prepay_id, "timestamp": timestamp}
        query = urllib.parse.urlencode({
            **sign_params,
            "sign": self._sign(sign_params),
            "sign_type": "SHA256WithRSA", "version": "1.0",
            "trade_type": "Checkout", "language": language,
        })
        url = f"{self.checkout_base_url}?{query}"
        logger.info("Checkout URL generated")
        return url

    def create_payment(self, amount: float, title: str,
                       order_id: Optional[str] = None, currency: str = "DJF",
                       timeout: str = "120m", notify_url: Optional[str] = None,
                       redirect_url: Optional[str] = None, language: str = "en") -> Dict:
        order_id = order_id or self._generate_order_id()
        raw = self.create_preorder(amount=amount, title=title, order_id=order_id,
                                   currency=currency, timeout=timeout,
                                   notify_url=notify_url, redirect_url=redirect_url)
        biz = raw.get("biz_content")
        if isinstance(biz, str):
            try: biz = json.loads(biz)
            except Exception: biz = {}
        elif not isinstance(biz, dict):
            biz = {}

        prepay_id    = biz.get("prepay_id")
        checkout_url = self.generate_checkout_url(prepay_id, language) if prepay_id else None
        return {"success": True, "order_id": order_id, "prepay_id": prepay_id,
                "checkout_url": checkout_url, "amount": amount,
                "currency": currency, "raw_response": raw}