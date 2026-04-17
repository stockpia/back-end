from pydantic import BaseModel, Field
from typing import List, Literal

# =================== 주문 관련 스키마 ===================

class OrderPayload(BaseModel):
    """POST /api/v1/orders 요청 본문"""
    ticker: str = Field(..., description="종목코드 (6자리)", example="005930")
    side: Literal["buy", "sell"] = Field(..., description="매수/매도 구분", example="buy")
    order_type: Literal["limit", "market"] = Field(..., description="지정가/시장가 구분", example="limit")
    price: int = Field(0, description="주문 단가 (시장가 주문 시 0 또는 생략)", example=75000)
    quantity: int = Field(..., description="주문 수량", example=10)

class OrderResponse(BaseModel):
    """주문 응답"""
    success: bool
    order_id: str | None = None
    message: str

class PendingOrder(BaseModel):
    """미체결 주문 정보"""
    order_id: str
    symbol: str
    company_name: str
    side: str
    price: int
    pending_quantity: int
    ordered_at: str

class PendingOrdersResponse(BaseModel):
    """미체결 주문 목록 응답"""
    pending_orders: List[PendingOrder]
    total_count: int

class CancelOrderPayload(BaseModel):
    """DELETE /api/v1/orders/{order_id} 요청 본문"""
    ticker: str = Field(..., description="종목코드", example="005930")
    quantity: int = Field(..., description="취소 수량", example=10)


# =================== 시세 및 계좌 관련 스키마 ===================

class OrderBookItem(BaseModel):
    price: int
    quantity: int

class OrderBookResponse(BaseModel):
    """GET /api/v1/stocks/{ticker}/orderbook 응답"""
    symbol: str
    asks: List[OrderBookItem]
    bids: List[OrderBookItem]
    trade_strength: float

class AccountBalanceResponse(BaseModel):
    """GET /api/v1/account/balance 응답"""
    available_cash: int

class HoldingResponse(BaseModel):
    """GET /api/v1/account/holdings/{ticker} 응답"""
    ticker: str
    holding_quantity: int
    average_price: float
    current_price: int
