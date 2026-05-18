"""
Web_06 실시간 호가 WebSocket 스트리밍
KIS WebSocket API — 실시간 호가 (H0STASP0)
"""

import json
import os
import threading
import time
from typing import Callable, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# 싱글톤 인스턴스 저장용 변수
_web_order_book_stream_instance: Optional['WebOrderBookStream'] = None
_web_order_book_stream_lock = threading.Lock()


class WebOrderBookStream:
    """
    KIS 실시간 호가 WebSocket 스트리밍 (Web_06)
    싱글톤으로 관리되어 애플리케이션 전체에서 단 하나의 인스턴스만 존재합니다.
    """

    WS_URL_PROD  = "ws://ops.koreainvestment.com:21000"
    WS_URL_VPS   = "ws://ops.koreainvestment.com:31000"
    APPROVAL_URL = "https://openapi.koreainvestment.com:9443/oauth2/Approval"
    TR_ID        = "H0STASP0"   # 실시간 호가 — 실전/모의 동일

    def __init__(self):
        self._initialized = False
        self._env = os.environ.get("KIS_ENV", "prod").strip().lower()
        
        if self._env == "prod":
            self._app_key    = os.getenv("KIS_APP_KEY_PROD", "").strip() or os.getenv("KIS_APP_KEY", "").strip()
            self._app_secret = os.getenv("KIS_APP_SECRET_PROD", "").strip() or os.getenv("KIS_APP_SECRET", "").strip()
        else:
            self._app_key    = os.getenv("KIS_APP_KEY", "").strip()
            self._app_secret = os.getenv("KIS_APP_SECRET", "").strip()

        self._ws_url     = self.WS_URL_VPS if self._env == "vps" else self.WS_URL_PROD

        self._approval_key: Optional[str]  = None
        self._ws                           = None
        self._thread: Optional[threading.Thread] = None
        self._callbacks: Dict[str, Callable]     = {}   # ticker → callback
        self._connected                    = False

    def connect(self) -> None:
        if self._connected:
            return

        try:
            import websocket
        except ImportError:
            raise ImportError("pip install websocket-client")

        self._approval_key = self._get_approval_key()

        self._ws = websocket.WebSocketApp(
            self._ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()

        # 연결 확인 대기 (최대 5초)
        for _ in range(10):
            if self._connected:
                print(f"[WebOrderBookStream] KIS WebSocket connected to {self._ws_url}")
                return
            time.sleep(0.5)
        raise RuntimeError("Failed to connect to KIS WebSocket after retries")

    def disconnect(self) -> None:
        if self._ws:
            self._ws.close()
        self._connected = False
        self._callbacks.clear()
        print("[WebOrderBookStream] KIS WebSocket disconnected.")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def subscribe(self, ticker: str, callback: Callable[[Dict], None]) -> None:
        if ticker in self._callbacks:
            return
        self._callbacks[ticker] = callback
        self._send_control(ticker, subscribe=True)
        print(f"[WebOrderBookStream] Subscribed to KIS WS for {ticker}")

    def unsubscribe(self, ticker: str) -> None:
        if ticker not in self._callbacks:
            return
        self._send_control(ticker, subscribe=False)
        self._callbacks.pop(ticker, None)
        print(f"[WebOrderBookStream] Unsubscribed from KIS WS for {ticker}")

    def _on_open(self, ws) -> None:
        self._connected = True
        print(f"[WebOrderBookStream] KIS WS _on_open. Re-subscribing {len(self._callbacks)} tickers.")
        for ticker in list(self._callbacks.keys()):
            self._send_control(ticker, subscribe=True)

    def _on_message(self, ws, raw: str) -> None:
        """
        수신 메시지 파싱.
        수신된 호가 데이터를 channel_layer를 통해 해당 그룹의 컨슈머에게 전송합니다.
        """
        if raw.startswith("{"):
            body = json.loads(raw).get("body", {})
            if body.get("rt_cd") != "0":
                print(f"[WS WARN] KIS WS control message error: {body.get('msg1', '')}")
            return

        parts = raw.split("|")
        if len(parts) < 4 or parts[1] != self.TR_ID:
            return

        parsed_data = self._parse_asking(parts[3])
        ticker = parsed_data.get("symbol", "")
        
        if ticker in self._callbacks:
            self._callbacks[ticker](parsed_data)

    def _on_error(self, ws, error) -> None:
        print(f"[WebOrderBookStream] KIS WS ERROR: {error}")
        self._connected = False # 에러 발생 시 연결 상태를 False로

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self._connected = False
        print(f"[WebOrderBookStream] KIS WS CLOSED: {close_status_code} {close_msg}")

    def _send_control(self, ticker: str, subscribe: bool) -> None:
        """구독/해제 제어 메시지 전송."""
        if not self._ws or not self._connected:
            print(f"[WebOrderBookStream] KIS WS not connected, cannot send control for {ticker}")
            return
        
        # KIS API WebSocket 구독/해제 메시지 형식
        message = json.dumps({
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "2", # 1: 구독, 2: 해제
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": self.TR_ID,
                    "tr_key": ticker,
                }
            },
        })
        self._ws.send(message)

    def _parse_asking(self, data_str: str) -> Dict:
        f = data_str.split("^")
        try:
            symbol = f[0]
            asks = [
                {"price": int(f[3 + i]), "quantity": int(f[23 + i])}
                for i in range(10)
                if len(f) > 23 + i and int(f[3 + i] or 0) > 0
            ]
            bids = [
                {"price": int(f[13 + i]), "quantity": int(f[33 + i])}
                for i in range(10)
                if len(f) > 33 + i and int(f[13 + i] or 0) > 0
            ]
            sell_vol = int(f[43]) if len(f) > 43 and f[43] else 0
            buy_vol  = int(f[44]) if len(f) > 44 and f[44] else 0
            total    = sell_vol + buy_vol
            trade_strength = round(buy_vol / total * 100, 1) if total > 0 else 50.0
            return {"symbol": symbol, "asks": asks, "bids": bids, "trade_strength": trade_strength}
        except (IndexError, ValueError) as e:
            print(f"[WebOrderBookStream] KIS WS PARSE ERROR: {e} for data: {data_str[:50]}...")
            return {"symbol": f[0] if f else "", "asks": [], "bids": [], "trade_strength": 50.0}

    def _get_approval_key(self) -> str:
        resp = requests.post(
            self.APPROVAL_URL,
            headers={"content-type": "application/json"},
            json={
                "grant_type": "client_credentials",
                "appkey": self._app_key,
                "secretkey": self._app_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        key = resp.json().get("approval_key", "")
        if not key:
            raise RuntimeError("WebSocket 접속키 발급 실패")
        return key


# ================================================
# 싱글톤 인스턴스 헬퍼 함수
# ================================================

def get_web_order_book_stream() -> WebOrderBookStream:
    """WebOrderBookStream 싱글톤 인스턴스를 반환합니다."""
    global _web_order_book_stream_instance
    with _web_order_book_stream_lock:
        if _web_order_book_stream_instance is None:
            _web_order_book_stream_instance = WebOrderBookStream()
    return _web_order_book_stream_instance
