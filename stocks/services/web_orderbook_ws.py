"""
Web_06 실시간 호가 WebSocket 스트리밍
KIS WebSocket API — 실시간 호가 (H0STASP0)

실전/VPS(모의투자) 모두 지원. 포트만 다름.
  - 실전: ws://ops.koreainvestment.com:21000
  - 모의: ws://ops.koreainvestment.com:31000
KIS_ENV 값에 따라 자동 분기됨.

백엔드 연동 흐름:
    1. WebOrderBookStream 인스턴스 생성 (싱글톤)
    2. connect() 호출 (내부적으로 백그라운드 스레드 실행)
    3. subscribe(ticker, channel_layer, group_name) — 종목 구독 + 데이터 수신 시 채널 레이어로 푸시
    4. 종목 변경 시 unsubscribe(old) → subscribe(new)
    5. 서버 종료 시 disconnect()

의존 패키지:
    pip install websocket-client
"""

import json
import os
import threading
import time
from typing import Callable, Dict, Optional
import asyncio

import requests
from dotenv import load_dotenv
from channels.layers import get_channel_layer # Channels의 channel_layer 가져오기
from asgiref.sync import async_to_sync # 비동기 함수를 동기적으로 호출하기 위함

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

    def __new__(cls):
        """싱글톤 패턴 구현"""
        with _web_order_book_stream_lock:
            if _web_order_book_stream_instance is None:
                instance = super().__new__(cls)
                instance._initialized = False # __init__이 한 번만 호출되도록 플래그
                _web_order_book_stream_instance = instance
            return _web_order_book_stream_instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._env        = os.environ.get("KIS_ENV", "prod").strip().lower()
        
        # HantuStock과 동일하게 KIS_ENV에 따라 키를 동적으로 읽어옴
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
        # self._callbacks: Dict[str, Callable]     = {}   # ticker → callback (이제 channel_layer로 대체)
        self._connected                    = False
        self._subscribed_groups: Dict[str, str] = {} # ticker -> room_group_name

        self._channel_layer = get_channel_layer() # Channels의 channel_layer 인스턴스 가져오기

    # ================================================
    # Public — 연결 관리
    # ================================================

    def connect(self) -> None:
        """
        WebSocket 연결 (백그라운드 데몬 스레드).
        connect() 호출 후 최대 5초 내에 연결 완료.
        """
        if self._connected and self._ws and self._ws.connected: # 이미 연결되어 있으면 다시 연결하지 않음
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
        """WebSocket 연결 종료 + 콜백 전체 해제."""
        if self._ws:
            self._ws.close()
        self._connected = False
        self._subscribed_groups.clear()
        print("[WebOrderBookStream] KIS WebSocket disconnected.")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ================================================
    # Public — 구독 관리
    # ================================================

    def subscribe(self, ticker: str, room_group_name: str) -> None:
        """
        종목 호가 구독.
        KIS WebSocket에 구독 요청을 보내고, 수신된 데이터를 channel_layer로 전송합니다.
        """
        if ticker in self._subscribed_groups:
            return # 이미 구독 중

        self._subscribed_groups[ticker] = room_group_name
        self._send_control(ticker, subscribe=True)
        print(f"[WebOrderBookStream] Subscribed to KIS WS for {ticker}, group: {room_group_name}")

    def unsubscribe(self, ticker: str) -> None:
        """종목 호가 구독 해제."""
        if ticker not in self._subscribed_groups:
            return # 구독 중이 아님

        self._send_control(ticker, subscribe=False)
        self._subscribed_groups.pop(ticker, None)
        print(f"[WebOrderBookStream] Unsubscribed from KIS WS for {ticker}")

    # ================================================
    # Internal — WebSocket 이벤트 핸들러
    # ================================================

    def _on_open(self, ws) -> None:
        self._connected = True
        print(f"[WebOrderBookStream] KIS WS _on_open. Re-subscribing {len(self._subscribed_groups)} tickers.")
        # 재연결 시 기존 구독 종목 복구
        for ticker in list(self._subscribed_groups.keys()):
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
        
        if ticker in self._subscribed_groups:
            room_group_name = self._subscribed_groups[ticker]
            # 비동기 channel_layer.group_send를 동기 컨텍스트에서 호출
            async_to_sync(self._channel_layer.group_send)(
                room_group_name,
                {
                    'type': 'orderbook_message', # 컨슈머의 메서드 이름과 일치해야 함
                    'data': parsed_data
                }
            )

    def _on_error(self, ws, error) -> None:
        print(f"[WebOrderBookStream] KIS WS ERROR: {error}")
        self._connected = False # 에러 발생 시 연결 상태를 False로

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self._connected = False
        print(f"[WebOrderBookStream] KIS WS CLOSED: {close_status_code} {close_msg}")
        # 연결이 끊어졌으므로 재연결 로직을 여기에 추가할 수 있습니다.
        # 예: self.connect()를 일정 시간 후 다시 호출

    # ================================================
    # Internal — 메시지 송신
    # ================================================

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
        print(f"[WebOrderBookStream] Sent KIS WS control: {message}")

    # ================================================
    # Internal — 데이터 파싱
    # ================================================

    def _parse_asking(self, data_str: str) -> Dict:
        """
        H0STASP0 실시간 호가 파싱.
        """
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

            # KIS API 문서에 따르면 체결량 필드 인덱스가 다를 수 있으므로 확인 필요
            # 현재는 f[43]과 f[44]를 사용하지만, 실제 데이터와 다를 경우 수정 필요
            sell_vol = int(f[43]) if len(f) > 43 and f[43] else 0
            buy_vol  = int(f[44]) if len(f) > 44 and f[44] else 0
            total    = sell_vol + buy_vol
            trade_strength = round(buy_vol / total * 100, 1) if total > 0 else 50.0

            return {"symbol": symbol, "asks": asks, "bids": bids, "trade_strength": trade_strength}

        except (IndexError, ValueError) as e:
            print(f"[WebOrderBookStream] KIS WS PARSE ERROR: {e} for data: {data_str[:50]}...")
            return {"symbol": f[0] if f else "", "asks": [], "bids": [], "trade_strength": 50.0}

    # ================================================
    # Internal — 접속키 발급
    # ================================================

    def _get_approval_key(self) -> str:
        """KIS WebSocket 접속키 발급."""
        # HantuStock과 동일하게 KIS_ENV에 따라 키를 동적으로 읽어옴
        _env = os.environ.get("KIS_ENV", "prod").strip().lower()
        if _env == "prod":
            app_key    = os.getenv("KIS_APP_KEY_PROD", "").strip() or os.getenv("KIS_APP_KEY", "").strip()
            app_secret = os.getenv("KIS_APP_SECRET_PROD", "").strip() or os.getenv("KIS_APP_SECRET", "").strip()
        else:
            app_key    = os.getenv("KIS_APP_KEY", "").strip()
            app_secret = os.getenv("KIS_APP_SECRET", "").strip()

        resp = requests.post(
            self.APPROVAL_URL,
            headers={"content-type": "application/json"},
            json={
                "grant_type": "client_credentials",
                "appkey": app_key,
                "secretkey": app_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        key = resp.json().get("approval_key", "")
        if not key:
            raise RuntimeError("WebSocket 접속키 발급 실패")
        print(f"[WebOrderBookStream] KIS WS Approval Key obtained: {key[:10]}...")
        return key


# ================================================
# 싱글톤 인스턴스 헬퍼 함수
# ================================================

def get_web_order_book_stream() -> WebOrderBookStream:
    """WebOrderBookStream 싱글톤 인스턴스를 반환합니다."""
    global _web_order_book_stream_instance
    if _web_order_book_stream_instance is None:
        with _web_order_book_stream_lock:
            if _web_order_book_stream_instance is None:
                _web_order_book_stream_instance = WebOrderBookStream()
    return _web_order_book_stream_instance


# ================================================
# 단독 실행 테스트
# ================================================

if __name__ == "__main__":
    # 이 부분은 단독 실행 테스트 코드이므로, 실제 Channels 컨슈머에서는 사용하지 않습니다.
    # Channels 컨슈머는 get_web_order_book_stream()을 통해 인스턴스를 가져와야 합니다.
    ws_stream = get_web_order_book_stream()
    print(f"연결 중... ({ws_stream._ws_url})")
    ws_stream.connect()

    # 테스트용 콜백 함수 (컨슈머의 orderbook_message와 유사)
    def test_callback(data):
        asks = data["asks"][0]["price"] if data["asks"] else "-"
        bids = data["bids"][0]["price"] if data["bids"] else "-"
        print(f"[{data['symbol']}] 매도1호가: {asks:,}  매수1호가: {bids:,}  체결강도: {data['trade_strength']}%")

    # 임시로 channel_layer를 모의하여 테스트
    class MockChannelLayer:
        async def group_send(self, group_name, message):
            print(f"[MockChannelLayer] Sending to {group_name}: {message}")
            test_callback(message['data']) # 실제 컨슈머의 orderbook_message가 하는 일을 모의

    ws_stream._channel_layer = MockChannelLayer() # Mock Channel Layer 주입

    ws_stream.subscribe("005930", "stock_005930")
    time.sleep(10)
    ws_stream.unsubscribe("005930")
    ws_stream.disconnect()
    print("종료")
