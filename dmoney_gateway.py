"""D-Money Payment Gateway — multi-tenant client.

Each ThirdParty has its own appid / app_key / app_secret / merch_code /
RSA private key, so the gateway is now instantiated per request (with a
small in-memory token cache keyed by appid to avoid re-fetching auth tokens
on every call).

Test:       https://pgtest.d-money.dj:38443
Production: https://pg.d-moneyservice.dj:38443
"""
import base64
import json
import logging
import secrets
import string
import time
import urllib.parse
from datetime import datetime
from typing import Dict, Optional

import requests
import urllib3
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger("DmoneyGateway")

# Suppress insecure-request warnings if any gateway runs with verify_ssl=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class DmoneyPaymentGateway:
    """Per-tenant D-Money client.

    Construct directly with credentials, or via `from_third_party(tp, decrypt_fn)`.
    """

    GATEWAY_PATH     = "/apiaccess/payment/gateway"
    TOKEN_PATH       = "/payment/v1/token"
    PREORDER_PATH    = "/payment/v1/merchant/preOrder"
    QUERY_ORDER_PATH = "/payment/v1/merchant/queryOrder"

    _EXPIRY_FORMATS = [
        "%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S",
    ]

    # class-level token cache: appid -> (token, expiry_ts)
    _TOKEN_CACHE: Dict[str, tuple] = {}

    def __init__(
        self,
        *,
        base_url: str,
        app_key: str,
        app_secret: str,
        appid: str,
        merch_code: str,
        private_key,                       # PEM str, base64-DER str, or loaded RSA key
        query_base_url:    Optional[str] = None,
        checkout_base_url: Optional[str] = None,
        business_type:     str  = "OnlineMerchant",
        notify_url:        Optional[str] = None,
        redirect_url:      Optional[str] = None,
        verify_ssl:        bool = True,
        timeout_sec:       int  = 30,
    ):
        if not all([base_url, app_key, app_secret, appid, merch_code, private_key]):
            raise ValueError("base_url, app_key, app_secret, appid, merch_code, private_key are all required")

        self.base_url          = base_url.rstrip("/")
        self.query_base_url    = (query_base_url or base_url).rstrip("/")
        self.checkout_base_url = (
            checkout_base_url or f"{self.base_url}/payment/web/paygate"
        )
        self.x_app_key     = app_key
        self.app_secret    = app_secret
        self.appid         = appid
        self.merch_code    = merch_code
        self.business_type = business_type or "OnlineMerchant"
        self.notify_url    = notify_url
        self.redirect_url  = redirect_url
        self.verify_ssl    = verify_ssl
        self.timeout_sec   = timeout_sec

        self.private_key = (
            private_key if hasattr(private_key, "sign") else self._load_key(private_key)
        )

        self.token: Optional[str] = None
        self.token_expiry: Optional[float] = None

    # ── Factory from a DB row ────────────────────────────────────────────────
    @classmethod
    def from_third_party(cls, tp, decrypt_fn) -> "DmoneyPaymentGateway":
        """Build a gateway from a ThirdParty DB row.

        `decrypt_fn(str) -> str` is the function that unwraps Fernet-encrypted fields.
        """
        return cls(
            base_url           = tp.dmoney_base_url,
            query_base_url     = tp.dmoney_query_base_url,
            checkout_base_url  = tp.dmoney_checkout_base_url,
            app_key            = tp.app_key,
            app_secret         = decrypt_fn(tp.app_secret_enc),
            appid              = tp.appid,
            merch_code         = tp.merch_code,
            private_key        = decrypt_fn(tp.private_key_enc),
            business_type      = tp.business_type or "OnlineMerchant",
            notify_url         = tp.notify_url,
            redirect_url       = tp.redirect_url,
        )

    # ── Key loading ──────────────────────────────────────────────────────────
    @staticmethod
    def _load_key(raw: str):
        raw = (raw or "").strip()
        if not raw:
            raise ValueError("Empty private key")
        try:
            if raw.startswith("-----"):
                return serialization.load_pem_private_key(
                    raw.encode(), password=None, backend=default_backend()
                )
            return serialization.load_der_private_key(
                base64.b64decode(raw), password=None, backend=default_backend()
            )
        except Exception as e:
            raise ValueError(f"Failed to load private key: {e}") from e

    # ── URL helpers ──────────────────────────────────────────────────────────
    def _api_url(self, path: str) -> str:
        base = self.base_url
        if self.GATEWAY_PATH in base:
            base = base[:base.index(self.GATEWAY_PATH)]
        return f"{base}{self.GATEWAY_PATH}{path if path.startswith('/') else '/' + path}"

    def _query_api_url(self, path: str) -> str:
        base = self.query_base_url
        if self.GATEWAY_PATH in base:
            base = base[:base.index(self.GATEWAY_PATH)]
        return f"{base}{self.GATEWAY_PATH}{path if path.startswith('/') else '/' + path}"

    # ── Misc helpers ─────────────────────────────────────────────────────────
    def _parse_expiry(self, s: str) -> float:
        for fmt in self._EXPIRY_FORMATS:
            try:
                return datetime.strptime(s.strip(), fmt).timestamp()
            except ValueError:
                pass
        try:
            from dateutil import parser as dp
            return dp.parse(s).timestamp()
        except Exception:
            pass
        return time.time() + 3600

    def _generate_order_id(self) -> str:
        ts     = datetime.now().strftime("%Y%m%d%H%M%S")
        suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        return f"ORD{ts}{suffix}"

    def _nonce(self, n: int = 32) -> str:
        return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(n))

    def _timestamp(self) -> str:
        return str(int(time.time()))

    def _signing_string(self, params: dict) -> str:
        exclude = {"sign", "sign_type", "biz_content"}
        items = sorted(
            (k, str(v)) for k, v in params.items()
            if k not in exclude and v is not None and str(v).strip()
        )
        return "&".join(f"{k}={v}" for k, v in items)

    def _sign(self, params: dict) -> str:
        sig = self.private_key.sign(
            self._signing_string(params).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def _ensure_token(self):
        cached = self._TOKEN_CACHE.get(self.appid)
        if cached and time.time() < cached[1] - 60:
            self.token, self.token_expiry = cached
            return
        self.get_token()
        self._TOKEN_CACHE[self.appid] = (self.token, self.token_expiry)

    # ── Public API ───────────────────────────────────────────────────────────
    def get_token(self) -> Dict:
        url = self._api_url(self.TOKEN_PATH)
        resp = requests.post(
            url,
            json={"appSecret": self.app_secret},
            headers={"Content-Type": "application/json", "X-APP-Key": self.x_app_key},
            verify=self.verify_ssl,
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorCode"):
            raise RuntimeError(f"Token error [{data['errorCode']}]: {data.get('errorMsg')}")
        self.token        = data["token"]
        expiry            = data.get("expirationDate")
        self.token_expiry = self._parse_expiry(expiry) if expiry else time.time() + 3600
        logger.info(f"Token acquired appid={self.appid} ttl={int(self.token_expiry - time.time())}s")
        return data

    def create_preorder(
        self,
        amount: float,
        title:  str,
        order_id:     Optional[str] = None,
        currency:     str = "DJF",
        timeout:      str = "120m",
        notify_url:   Optional[str] = None,
        redirect_url: Optional[str] = None,
        callback_info: Optional[str] = None,
    ) -> Dict:
        self._ensure_token()
        order_id     = order_id or self._generate_order_id()
        notify_url   = (notify_url   or self.notify_url   or "").strip()
        redirect_url = (redirect_url or self.redirect_url or "").strip()

        if not order_id.isalnum():
            raise ValueError("order_id must be alphanumeric only")
        if not notify_url or not redirect_url:
            raise ValueError("notify_url and redirect_url are required")

        nonce_str = self._nonce()
        timestamp = self._timestamp()

        sign_params = {
            "appid":           self.appid,
            "business_type":   self.business_type,
            "merch_code":      self.merch_code,
            "merch_order_id":  order_id,
            "method":          "payment.preorder",
            "nonce_str":       nonce_str,
            "notify_url":      notify_url,
            "redirect_url":    redirect_url,
            "timeout_express": timeout,
            "timestamp":       timestamp,
            "title":           title,
            "total_amount":    str(int(amount)),
            "trade_type":      "Checkout",
            "trans_currency":  currency,
            "version":         "1.0",
        }
        if callback_info:
            sign_params["callback_info"] = callback_info

        biz = {
            "appid":           self.appid,
            "merch_code":      self.merch_code,
            "merch_order_id":  order_id,
            "business_type":   self.business_type,
            "trade_type":      "Checkout",
            "trans_currency":  currency,
            "total_amount":    str(int(amount)),
            "timeout_express": timeout,
            "title":           title,
            "notify_url":      notify_url,
            "redirect_url":    redirect_url,
        }
        if callback_info:
            biz["callback_info"] = callback_info

        payload = {
            "nonce_str":   nonce_str,
            "method":      "payment.preorder",
            "version":     "1.0",
            "sign_type":   "SHA256WithRSA",
            "timestamp":   timestamp,
            "sign":        self._sign(sign_params),
            "biz_content": biz,
        }

        url = self._api_url(self.PREORDER_PATH)
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": self.token,
                "X-APP-Key":     self.x_app_key,
            },
            verify=self.verify_ssl,
            timeout=self.timeout_sec,
        )
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"Non-JSON response ({resp.status_code}): {resp.text}")

        if resp.status_code != 200:
            raise RuntimeError(
                f"PreOrder failed [{resp.status_code}]: {data.get('errorMsg', resp.text)}"
            )
        if data.get("errorCode"):
            raise RuntimeError(
                f"PreOrder error [{data['errorCode']}]: {data.get('errorMsg')} "
                f"-- {data.get('errorSolution', '')}"
            )

        logger.info(f"PreOrder OK appid={self.appid} order={order_id} amount={int(amount)} {currency}")
        return data

    def query_order(
        self,
        merch_order_id: Optional[str] = None,
        trade_no:       Optional[str] = None,
    ) -> Dict:
        if not merch_order_id and not trade_no:
            raise ValueError("Provide merch_order_id or trade_no")
        self._ensure_token()

        nonce_str   = self._nonce()
        timestamp   = self._timestamp()
        sign_params = {
            "appid":      self.appid,
            "merch_code": self.merch_code,
            "method":     "payment.queryorder",
            "nonce_str":  nonce_str,
            "timestamp":  timestamp,
            "version":    "1.0",
        }
        biz = {"appid": self.appid, "merch_code": self.merch_code}
        if merch_order_id:
            sign_params["merch_order_id"] = merch_order_id
            biz["merch_order_id"] = merch_order_id
        if trade_no:
            sign_params["trade_no"] = trade_no
            biz["trade_no"] = trade_no

        payload = {
            "nonce_str":   nonce_str,
            "method":      "payment.queryorder",
            "version":     "1.0",
            "sign_type":   "SHA256WithRSA",
            "timestamp":   timestamp,
            "sign":        self._sign(sign_params),
            "biz_content": biz,
        }

        resp = requests.post(
            self._query_api_url(self.QUERY_ORDER_PATH),
            json=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": self.token,
                "X-APP-Key":     self.x_app_key,
            },
            verify=self.verify_ssl,
            timeout=self.timeout_sec,
        )

        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"Non-JSON response ({resp.status_code}): {resp.text}")

        if resp.status_code != 200:
            raise RuntimeError(
                f"QueryOrder failed [{resp.status_code}]: {data.get('errorMsg', resp.text)}"
            )
        if data.get("errorCode"):
            raise RuntimeError(
                f"QueryOrder error [{data['errorCode']}]: {data.get('errorMsg')}"
            )
        return data

    def generate_checkout_url(self, prepay_id: str, language: str = "en") -> str:
        nonce_str = self._nonce()
        timestamp = self._timestamp()
        sign_params = {
            "appid":      self.appid,
            "merch_code": self.merch_code,
            "nonce_str":  nonce_str,
            "prepay_id":  prepay_id,
            "timestamp":  timestamp,
        }
        query = urllib.parse.urlencode({
            **sign_params,
            "sign":       self._sign(sign_params),
            "sign_type":  "SHA256WithRSA",
            "version":    "1.0",
            "trade_type": "Checkout",
            "language":   language,
        })
        return f"{self.checkout_base_url}?{query}"

    # ── High-level: preorder + checkout URL in one call ──────────────────────
    def create_payment(
        self,
        amount: float,
        title:  str,
        order_id:      Optional[str] = None,
        currency:      str = "DJF",
        timeout:       str = "120m",
        notify_url:    Optional[str] = None,
        redirect_url:  Optional[str] = None,
        callback_info: Optional[str] = None,
        language:      str = "en",
    ) -> Dict:
        order_id = order_id or self._generate_order_id()
        raw = self.create_preorder(
            amount        = amount,
            title         = title,
            order_id      = order_id,
            currency      = currency,
            timeout       = timeout,
            notify_url    = notify_url,
            redirect_url  = redirect_url,
            callback_info = callback_info,
        )
        biz = raw.get("biz_content")
        if isinstance(biz, str):
            try:
                biz = json.loads(biz)
            except Exception:
                biz = {}
        elif not isinstance(biz, dict):
            biz = {}

        prepay_id    = biz.get("prepay_id")
        checkout_url = self.generate_checkout_url(prepay_id, language) if prepay_id else None
        return {
            "success":      True,
            "order_id":     order_id,
            "prepay_id":    prepay_id,
            "checkout_url": checkout_url,
            "amount":       amount,
            "currency":     currency,
            "raw_response": raw,
        }
