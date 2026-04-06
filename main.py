"""
D-Money Payment Gateway — FastAPI
Hosted at: https://api.scolapp.com
"""

from fastapi import FastAPI, HTTPException, Security, Depends, Request
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import Optional
import os, logging

from dmoney_gateway import DmoneyPaymentGateway

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DmoneyAPI")

app = FastAPI(
    title="D-Money Payment API",
    description="Hosted at https://api.scolapp.com",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://api.scolapp.com",
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5500",
        "null",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY        = os.getenv("API_KEY", "")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(key: str = Security(api_key_header)):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

gateway: Optional[DmoneyPaymentGateway] = None

@app.on_event("startup")
def startup():
    global gateway
    gateway = DmoneyPaymentGateway()
    logger.info("Gateway ready — https://api.scolapp.com")


# ── Models ────────────────────────────────────────────────────────────────────

class CreatePaymentRequest(BaseModel):
    amount:       float         = Field(...,    example=1000)
    title:        str           = Field(...,    example="Scolarite Janvier 2026")
    order_id:     Optional[str] = Field(None,   example="ORD20260101ABC")
    currency:     str           = Field("DJF",  example="DJF")
    timeout:      str           = Field("120m", example="120m")
    notify_url:   Optional[str] = Field(None,   example="https://api.scolapp.com/payment/notify")
    redirect_url: Optional[str] = Field(None,   example="https://api.scolapp.com/payment/success")
    language:     str           = Field("en",   example="en")

class CreatePaymentResponse(BaseModel):
    success:      bool
    order_id:     str
    prepay_id:    Optional[str]
    checkout_url: Optional[str]
    amount:       float
    currency:     str

class QueryOrderRequest(BaseModel):
    merch_order_id: Optional[str] = Field(None, example="ORD20260101ABC")
    trade_no:       Optional[str] = Field(None, example="DMONEY-TRADE-XYZ")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    return {
        "status": "ok",
        "gateway_ready": gateway is not None,
        "domain": "api.scolapp.com",
    }

@app.get("/payment/token", tags=["Payment"], dependencies=[Depends(verify_api_key)])
def get_token():
    try:
        data = gateway.get_token()
        return {"token": data.get("token"), "expiration_date": data.get("expirationDate")}
    except Exception as e:
        logger.error(f"Token error: {e}")
        raise HTTPException(status_code=502, detail=str(e))

@app.post("/payment/create", response_model=CreatePaymentResponse,
          tags=["Payment"], dependencies=[Depends(verify_api_key)])
def create_payment(req: CreatePaymentRequest):
    """
    Create a D-Money preorder and return checkout_url.
    The frontend calls window.open(checkout_url) to open the payment page.
    """
    try:
        result = gateway.create_payment(
            amount=req.amount,
            title=req.title,
            order_id=req.order_id,
            currency=req.currency,
            timeout=req.timeout,
            notify_url=req.notify_url   or "https://api.scolapp.com/payment/notify",
            redirect_url=req.redirect_url or "https://api.scolapp.com/payment/success",
            language=req.language,
        )
        return result
    except Exception as e:
        logger.error(f"create_payment error: {e}")
        raise HTTPException(status_code=502, detail=str(e))

@app.post("/payment/query", tags=["Payment"], dependencies=[Depends(verify_api_key)])
def query_order(req: QueryOrderRequest):
    """Query the status of an existing D-Money order."""
    if not req.merch_order_id and not req.trade_no:
        raise HTTPException(status_code=400, detail="Provide merch_order_id or trade_no")
    try:
        return gateway.query_order(
            merch_order_id=req.merch_order_id,
            trade_no=req.trade_no,
        )
    except Exception as e:
        logger.error(f"query_order error: {e}")
        raise HTTPException(status_code=502, detail=str(e))

@app.post("/payment/notify", tags=["Webhooks"])
async def payment_notify(request: Request):
    """D-Money calls this when payment status changes."""
    try:
        body = await request.json()
        logger.info(f"D-Money notify received: {body}")
    except Exception:
        pass
    return {"returnCode": "SUCCESS", "returnMsg": "OK"}

@app.get("/payment/success", tags=["Webhooks"])
def payment_success():
    """Redirect landing page after payment completes."""
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;text-align:center;padding:60px">
      <h2>✅ Payment Successful</h2>
      <p>Your payment has been processed successfully.</p>
      <p><a href="/payment.html">Make another payment</a></p>
    </body></html>
    """)