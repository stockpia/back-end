"""
Web_06 실시간 호가 WebSocket 스트리밍
KIS WebSocket API — 실시간 호가 (H0STASP0)

실전/VPS(모의투자) 모두 지원. 포트만 다름.
  - 실전: ws://ops.koreainvestment.com:21000
  - 모의: ws://ops.koreainvestment.com:31000
KIS_ENV 값에 따라 자동 분기됨.

백엔드 연동 흐름:
    1. WebOrderBookStream 인스턴스 생성
    2. connect() 호출 (내부적으로 백그라운드 스레드 실행)
    3. subscribe(ticker, callback) — 종목 구독 + 데이터 수신 시 콜백 호출
    4. 콜백 안에서 프론트엔드로 push (SSE / 클라이언트 웹소켓) — 백엔드 담당
    5. 종목 변경 시 unsubscribe(old) → subscribe(new)
    6. 서버 종료 시 disconnect()

의존 패키지:
    pip install websocket-client
"""

import json
import os
import threading
import time
from typing import Callable, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()


class WebOrderBookStream:
    """
    KIS 실시간 호가 WebSocket 스트리밍 (Web_06)

    실전/VPS(모의투자) 모두 지원.
    KIS_ENV=vps  → ws://ops.koreainvestment.com:31000
    KIS_ENV=prod → ws://ops.koreainvestment.com:21000
    """

    WS_URL_PROD  = "ws://ops.koreainvestment.com:21000"
    WS_URL_VPS   = "ws://ops.koreainvestment.com:31000"
    APPROVAL_URL = "https://openapi.koreainvestment.com:9443/oauth2/Approval"
    TR_ID        = "H0STASP0"   # 실시간 호가 — 실전/모의 동일

    def __init__(self):
        self._env        = os.environ.get("KIS_ENV", "prod")   # "prod" | "vps"
        self._app_key    = os.environ.get("KIS_APP_KEY")
        self._app_secret = os.environ.get("KIS_APP_SECRET")
        self._ws_url     = self.WS_URL_VPS if self._env == "vps" else self.WS_URL_PROD

        self._approval_key: Optional[str]  = None
        self._ws                           = None
        self._thread: Optional[threading.Thread] = None
        self._callbacks: Dict[str, Callable]     = {}   # ticker → callback
        self._connected                    = False

    # ================================================
    # Public — 연결 관리
    # ================================================

    def connect(self) -> None:
        """
        WebSocket 연결 (백그라운드 데몬 스레드).
        connect() 호출 후 최대 5초 내에 연결 완료.
        """
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
                break
            time.sleep(0.5)

    def disconnect(self) -> None:
        """WebSocket 연결 종료 + 콜백 전체 해제."""
        if self._ws:
            self._ws.close()
        self._connected = False
        self._callbacks.clear()

    # ================================================
    # Public — 구독 관리
    # ================================================

    def subscribe(self, ticker: str, callback: Callable[[Dict], None]) -> None:
        """
        종목 호가 구독.

        Args:
            ticker:   종목코드 (예: "005930")
            callback: 호가 수신 시 호출. 인자:
                {
                    "symbol": "005930",
                    "asks": [{"price": int, "quantity": int}, ...],  # 매도호가 낮은가격→높은가격
                    "bids": [{"price": int, "quantity": int}, ...],  # 매수호가 높은가격→낮은가격
                    "trade_strength": float,  # 체결강도(%) — 50 초과: 매수세 우위
                }
        """
        self._callbacks[ticker] = callback
        self._send_control(ticker, subscribe=True)

    def unsubscribe(self, ticker: str) -> None:
        """종목 호가 구독 해제."""
        self._send_control(ticker, subscribe=False)
        self._callbacks.pop(ticker, None)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ================================================
    # Internal — WebSocket 이벤트 핸들러
    # ================================================

    def _on_open(self, ws) -> None:
        self._connected = True
        # 재연결 시 기존 구독 종목 복구
        for ticker in list(self._callbacks.keys()):
            self._send_control(ticker, subscribe=True)

    def _on_message(self, ws, raw: str) -> None:
        """
        수신 메시지 파싱.

        KIS WebSocket 메시지 형식:
          JSON  → 구독 확인/에러 응답 ('{' 시작)
          PIPE  → 실시간 데이터: 암호화여부|TR_ID|건수|데이터본문
                  데이터본문은 '^' 구분 필드
        """
        if raw.startswith("{"):
            body = json.loads(raw).get("body", {})
            if body.get("rt_cd") != "0":
                print(f"[WS WARN] {body.get('msg1', '')}")
            return

        parts = raw.split("|")
        if len(parts) < 4 or parts[1] != self.TR_ID:
            return

        parsed = self._parse_asking(parts[3])
        ticker = parsed.get("symbol", "")
        if ticker in self._callbacks:
            self._callbacks[ticker](parsed)

    def _on_error(self, ws, error) -> None:
        print(f"[WS ERROR] {error}")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self._connected = False
        print(f"[WS CLOSED] {close_status_code} {close_msg}")

    # ================================================
    # Internal — 메시지 송신
    # ================================================

    def _send_control(self, ticker: str, subscribe: bool) -> None:
        """구독/해제 제어 메시지 전송."""
        if not self._ws or not self._connected:
            return
        self._ws.send(json.dumps({
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "2",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": self.TR_ID,
                    "tr_key": ticker,
                }
            },
        }))

    # ================================================
    # Internal — 데이터 파싱
    # ================================================

    def _parse_asking(self, data_str: str) -> Dict:
        """
        H0STASP0 실시간 호가 파싱.

        KIS 응답 필드 ('^' 구분, 0-indexed):
          [0]     MKSC_SHRN_ISCD   종목코드
          [1]     BSOP_HOUR        영업시간
          [2]     HOUR_CLS_CODE    시간구분코드
          [3~12]  ASKP1~10         매도호가 1~10
          [13~22] BIDP1~10         매수호가 1~10
          [23~32] ASKP_RSQN1~10    매도호가잔량 1~10
          [33~42] BIDP_RSQN1~10    매수호가잔량 1~10
          [43]    SELN_CNQN_SMTN   총매도체결량
          [44]    SHNU_CNQN_SMTN   총매수체결량

        ※ 실제 연동 테스트 후 필드 인덱스 검증 필요 (KIS OpenAPI H0STASP0 문서 기준)
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

            sell_vol = int(f[43]) if len(f) > 43 and f[43] else 0
            buy_vol  = int(f[44]) if len(f) > 44 and f[44] else 0
            total    = sell_vol + buy_vol
            trade_strength = round(buy_vol / total * 100, 1) if total > 0 else 50.0

            return {"symbol": symbol, "asks": asks, "bids": bids, "trade_strength": trade_strength}

        except (IndexError, ValueError) as e:
            print(f"[WS PARSE ERROR] {e}")
            return {"symbol": f[0] if f else "", "asks": [], "bids": [], "trade_strength": 50.0}

    # ================================================
    # Internal — 접속키 발급
    # ================================================

    def _get_approval_key(self) -> str:
        """KIS WebSocket 접속키 발급."""
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
# 단독 실행 테스트
# ================================================

if __name__ == "__main__":
    def on_data(data):
        asks = data["asks"][0]["price"] if data["asks"] else "-"
        bids = data["bids"][0]["price"] if data["bids"] else "-"
        print(f"[{data['symbol']}] 매도1호가: {asks:,}  매수1호가: {bids:,}  체결강도: {data['trade_strength']}%")

    ws = WebOrderBookStream()
    print(f"연결 중... ({ws._ws_url})")
    ws.connect()
    ws.subscribe("005930", on_data)
    time.sleep(10)
    ws.unsubscribe("005930")
    ws.disconnect()
    print("종료")
