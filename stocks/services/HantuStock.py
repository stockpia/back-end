from __future__ import annotations

import os
import pandas as pd
import time
import requests
import json
from datetime import datetime
import sys

try:
    import FinanceDataReader as fdr
except Exception:
    fdr = None

try:
    from pykrx import stock as pystock
except Exception:
    pystock = None

from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

# .env 로드 (현재 작업 디렉토리 기준)
load_dotenv()

# 실전 API (데이터 조회 전용) 토큰 캐시 — 프로세스 내 공유
_PROD_BASE_URL = "https://openapi.koreainvestment.com:9443"
_prod_token: str | None = None
_prod_token_time: float = 0.0

# vps(모의) + 사용자별 KIS 토큰 캐시 — 프로세스 내 공유
# 한투 OAuth 발급 한도: "1분에 1회" (EGW00133). 토큰 자체 수명: 24시간.
# 같은 (app_key, env) 조합은 24시간 동안 1회만 발급하도록 보장.
# 사용자별로 app_key 가 다르면 자동으로 별개 토큰 보관됨.
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_TOKEN_TTL_SECONDS = 86000  # 24h - 약간의 안전 마진

# Daphne 재시작 직후 여러 view 가 동시에 HantuStock() 을 생성하면
# 모두 cache miss 로 판단하여 동시에 /oauth2/tokenP 를 호출 → 한투 1분 제한 충돌.
# Lock 으로 동시 발급 시도를 1건으로 직렬화.
import threading
_token_lock = threading.Lock()


class HantuStock:
    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        account_id: str | None = None,
        *,
        env: str | None = None,
    ):
        """
        env: "prod"(실전) | "vps"(모의)
        - 인자를 생략하면 .env 값을 사용함
          KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_ID, KIS_ACCOUNT_SUFFIX(optional), KIS_ENV(optional)
        """
        _env = (env or os.getenv("KIS_ENV", "prod")).strip().lower()
        if _env not in {"prod", "vps", "paper", "demo", "sandbox", "vts"}:
            raise ValueError("env must be one of {'prod','vps'}; alias {'paper','demo','sandbox'} allowed")
        # alias 처리
        self._env = "vps" if _env in {"vps", "paper", "demo", "sandbox", "vts"} else "prod"

        if self._env == "prod":
            self._api_key = api_key or os.getenv("KIS_APP_KEY_PROD", "").strip() or os.getenv("KIS_APP_KEY", "").strip()
            self._secret_key = secret_key or os.getenv("KIS_APP_SECRET_PROD", "").strip() or os.getenv("KIS_APP_SECRET", "").strip()
        else:
            self._api_key = api_key or os.getenv("KIS_APP_KEY", "").strip()
            self._secret_key = secret_key or os.getenv("KIS_APP_SECRET", "").strip()

        self._account_id = account_id or os.getenv("KIS_ACCOUNT_ID", "").strip()
        self._account_suffix = os.getenv("KIS_ACCOUNT_SUFFIX", "01").strip() or "01"

        # 필수값 검증
        missing = [k for k, v in {
            "KIS_APP_KEY": self._api_key,
            "KIS_APP_SECRET": self._secret_key,
            "KIS_ACCOUNT_ID": self._account_id,
        }.items() if not v]
        if missing:
            raise ValueError(
                "Missing credentials: " + ", ".join(missing) +
                ". Set them in .env or pass them to HantuStock(...)."
            )

        self._base_url = (
            "https://openapi.koreainvestment.com:9443"
            if self._env == "prod"
            else "https://openapivts.koreainvestment.com:29443"
        )
        self._access_token = self._get_access_token()

    # -------------------- 내부 공통 --------------------
    def _tr(self, key: str) -> str:
        prefix = "TTTC" if self._env == "prod" else "VTTC"
        codes = {
            "inquire-balance": "8434R",
            "order-buy": "0012U",
            "order-sell": "0011U",
            "inquire-daily-ccld": "8001R",  # 주식일별주문체결조회 (3개월 이내)
            "inquire-pending": "8036R",     # 미체결 주문 조회
            "order-cancel": "0803U",        # 주문 취소
        }
        return prefix + codes[key]

    def _get_access_token(self) -> str:
        """
        (app_key, env) 단위 캐시 → 한투 "1분당 1회 발급(EGW00133)" 제한 우회.
        같은 자격증명·환경 조합은 24시간 동안 같은 토큰 재사용.
        Lock 으로 동시 발급 race 보호 (double-check locking).
        """
        cache_key = (self._api_key, self._env)

        # 빠른 경로: 락 없이 캐시 hit 면 그대로 반환
        cached = _TOKEN_CACHE.get(cache_key)
        if cached is not None:
            token, issued_at = cached
            if (time.time() - issued_at) < _TOKEN_TTL_SECONDS:
                return token

        # 캐시 miss → 락 내부에서 한 번 더 확인 후 발급
        with _token_lock:
            cached = _TOKEN_CACHE.get(cache_key)
            if cached is not None:
                token, issued_at = cached
                if (time.time() - issued_at) < _TOKEN_TTL_SECONDS:
                    return token

            # 실전/모의 모두 토큰 발급 경로는 /oauth2/tokenP 입니다.
            token_path = "/oauth2/tokenP"
            url = self._base_url + token_path
            headers = {"content-type": "application/json"}
            body = {
                "grant_type": "client_credentials",
                "appkey": self._api_key,
                "appsecret": self._secret_key,
            }

            try:
                res = requests.post(url, headers=headers, data=json.dumps(body), timeout=30) # timeout 30초로 증가
                data = res.json()
                if "access_token" in data:
                    token = data["access_token"]
                    _TOKEN_CACHE[cache_key] = (token, time.time())
                    return token
                print(f"[WARN] token error: {data}")
            except Exception as e:
                print(f"[ERROR] get_access_token (exception): {e}")
            raise RuntimeError("Failed to get access token")

    def _header(self, tr_id: str) -> dict:
        return {
            "content-type": "application/json",
            "appkey": self._api_key,
            "appsecret": self._secret_key,
            "authorization": f"Bearer {self._access_token}",
            "tr_id": tr_id,
        }

    def _request(self, url: str, headers: dict, params: dict, *, method: str = "get"):
        # API 과호출 방지를 위한 기본 지연 (초당 거래건수 제한 대비)
        time.sleep(0.2)
        try:
            if method == "get":
                resp = requests.get(url, headers=headers, params=params, timeout=30)
            else:
                resp = requests.post(url, headers=headers, data=json.dumps(params), timeout=30)

            # 1. 상태 코드가 200이 아닌 경우 (예: 404 Not Found, 403 Forbidden 등)
            if resp.status_code != 200:
                print(f"[API-ERROR] HTTP {resp.status_code} - {resp.url}")
                try:
                    data = resp.json()
                except Exception:
                    data = {"rt_cd": "1", "msg1": f"HTTP {resp.status_code} Error: API 경로를 찾을 수 없거나 권한이 없습니다."}
                return resp.headers, data

            # 2. JSON 응답 파싱
            r_headers = resp.headers
            try:
                data = resp.json()
            except Exception as e:
                print(f"[API-ERROR] JSON 파싱 실패: {resp.text[:100]}...")
                return r_headers, {"rt_cd": "1", "msg1": "API 서버에서 잘못된 응답을 반환했습니다."}

            if data.get("rt_cd") != "0":
                # EGW00201: 초당 거래건수 초과 시 딱 1번만 깔끔하게 재시도
                if data.get("msg_cd") in {"EGW00201", "EGW00123"}:
                    time.sleep(0.5)
                    if method == "get":
                        resp = requests.get(url, headers=headers, params=params, timeout=30)
                    else:
                        resp = requests.post(url, headers=headers, data=json.dumps(params), timeout=30)
                    
                    r_headers = resp.headers
                    try:
                        data = resp.json()
                    except Exception:
                        data = {"rt_cd": "1", "msg1": "재시도 후 JSON 파싱 실패"}

                # 재시도 후에도 에러면 최종 에러 출력
                if data.get("rt_cd") != "0":
                    print(f"[API-ERROR] {data.get('msg1')} (msg_cd: {data.get('msg_cd')})")

            return r_headers, data
        except Exception as e:
            print(f"[REQUEST-ERROR] {e}")
            return {}, {"rt_cd": "1", "msg1": f"request failed: {e}"}

    # -------------------- 시세 --------------------
    def get_stock_price(self, ticker: str) -> dict:
        """
        주식 현재가 시세 조회 (PER, PBR 포함)

        Args:
            ticker: 종목코드 (6자리)

        Returns:
            dict: 현재가, 등락, PER, PBR, EPS, BPS 등
        """
        headers = self._header("FHKST01010100")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        }
        url = self._base_url + "/uapi/domestic-stock/v1/quotations/inquire-price"
        _, res = self._request(url, headers, params)

        if res.get("rt_cd") != "0":
            return {"error": res.get("msg1", "조회 실패")}

        output = res.get("output", {})

        return {
            "ticker": ticker,
            "name": output.get("hts_kor_isnm", ""),  # 종목명
            "current_price": int(output.get("stck_prpr", 0)),  # 현재가
            "price_change": int(output.get("prdy_vrss", 0)),  # 전일대비
            "change_rate": float(output.get("prdy_ctrt", 0)),  # 전일대비율
            "open": int(output.get("stck_oprc", 0)),  # 시가
            "high": int(output.get("stck_hgpr", 0)),  # 고가
            "low": int(output.get("stck_lwpr", 0)),  # 저가
            "volume": int(output.get("acml_vol", 0)),  # 누적거래량
            "trade_amount": int(output.get("acml_tr_pbmn", 0)),  # 누적거래대금
            "per": float(output.get("per", 0)),  # PER
            "pbr": float(output.get("pbr", 0)),  # PBR
            "eps": float(output.get("eps", 0)),  # EPS
            "bps": float(output.get("bps", 0)),  # BPS
            "w52_high": int(output.get("stck_dryy_hgpr", 0)),  # 52주 최고가
            "w52_low": int(output.get("stck_dryy_lwpr", 0)),  # 52주 최저가
            "market_cap": int(output.get("hts_avls", 0)),  # 시가총액 (억)
        }

    def get_minute_chart(self, ticker: str, interval: int = 5) -> dict:
        """
        주식 분봉 조회 (당일 데이터)

        Args:
            ticker: 종목코드 (6자리)
            interval: 분봉 간격 (1, 5, 10, 15, 30, 60분)

        Returns:
            dict: 분봉 OHLCV 데이터
                - times: 시간 리스트 (HH:MM)
                - open, high, low, close, volume: 가격/거래량 리스트
        """
        headers = self._header("FHKST03010200")

        # 현재 시간 (장 마감 후면 15:30으로 설정)
        now = datetime.now()
        if now.hour >= 16 or (now.hour == 15 and now.minute > 30):
            end_time = "153000"
        else:
            end_time = now.strftime("%H%M%S")

        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_HOUR_1": end_time,
            "FID_PW_DATA_INCU_YN": "N",
        }
        url = self._base_url + "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"

        all_data = []
        for _ in range(10):  # 최대 10회 반복 (약 300개 데이터)
            _, res = self._request(url, headers, params)

            if res.get("rt_cd") != "0":
                if not all_data:
                    return {"error": res.get("msg1", "조회 실패")}
                break

            output2 = res.get("output2", [])
            if not output2:
                break

            all_data.extend(output2)

            # 다음 조회를 위해 마지막 시간 설정
            last_time = output2[-1].get("stck_cntg_hour", "")
            if not last_time or last_time <= "090000":
                break
            params["FID_INPUT_HOUR_1"] = last_time

        if not all_data:
            return {"error": "분봉 데이터가 없습니다"}

        # 데이터 정리 (시간순 정렬)
        all_data.reverse()

        # interval에 맞게 필터링 (5분봉이면 5분 단위만)
        filtered = []
        for item in all_data:
            time_str = item.get("stck_cntg_hour", "")
            if len(time_str) >= 4:
                minute = int(time_str[2:4])
                if minute % interval == 0:
                    filtered.append(item)

        times = []
        opens = []
        highs = []
        lows = []
        closes = []
        volumes = []

        for item in filtered:
            time_str = item.get("stck_cntg_hour", "")
            if len(time_str) >= 4:
                times.append(f"{time_str[:2]}:{time_str[2:4]}")
            opens.append(int(item.get("stck_oprc", 0)))
            highs.append(int(item.get("stck_hgpr", 0)))
            lows.append(int(item.get("stck_lwpr", 0)))
            closes.append(int(item.get("stck_prpr", 0)))
            volumes.append(int(item.get("cntg_vol", 0)))

        return {
            "ticker": ticker,
            "interval": interval,
            "count": len(times),
            "data": {
                "times": times,
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes
            }
        }

    @staticmethod
    def get_past_data(ticker: str, days: int = 100):
        if fdr is None:
            raise ImportError("FinanceDataReader not installed")
        df = fdr.DataReader(ticker)
        df.columns = [c.lower() for c in df.columns]
        df.index.name = "timestamp"
        df = df.reset_index()
        return df.iloc[-1] if days == 1 else df.tail(days)

    @staticmethod
    def get_past_data_total(days: int = 10):
        if pystock is None:
            raise ImportError("pykrx not installed")
        total = None
        got = 0
        passed = 0
        today = datetime.now()
        while (got < days) and passed < max(10, days * 2):
            d = str(today - relativedelta(days=passed)).split(" ")[0]
            k1 = pystock.get_market_ohlcv(d, market="KOSPI")
            k2 = pystock.get_market_ohlcv(d, market="KOSDAQ")
            data = pd.concat([k1, k2])
            passed += 1
            if data["거래대금"].sum() == 0:
                continue
            got += 1
            data.columns = ["open", "high", "low", "close", "volume", "trade_amount", "diff"]
            data.index.name = "ticker"
            data["timestamp"] = d
            total = data.copy() if total is None else pd.concat([total, data])
        total = total.sort_values("timestamp").reset_index()
        for col in ["open", "high", "low"]:
            total[col] = total[col].where(total[col] > 0, other=total["close"])
        return total

    # -------------------- 계좌 --------------------
    def _inquire_balance_raw(self, *, account_info=False):
        headers = self._header(self._tr("inquire-balance"))
        out = []
        cont = True
        fk100 = ""
        nk100 = ""
        while cont:
            params = {
                "CANO": self._account_id,
                "ACNT_PRDT_CD": self._account_suffix,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "N",
                "INQR_DVSN": "01",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "01",
                "CTX_AREA_FK100": fk100,
                "CTX_AREA_NK100": nk100,
            }
            url = self._base_url + "/uapi/domestic-stock/v1/trading/inquire-balance"
            hd, res = self._request(url, headers, params)
            if account_info:
                return res.get("output2", [{}])[0]
            cont = hd.get("tr_cont") in {"F", "M"}
            headers["tr_cont"] = "N"
            fk100 = res.get("ctx_area_fk100", "")
            nk100 = res.get("ctx_area_nk100", "")
            out += res.get("output1", [])
        return out

    def get_holding_stock(self, ticker: str | None = None, *, remove_stock_warrant: bool = True):
        """보유 종목 조회 (간단한 dict 반환)"""
        rows = self._inquire_balance_raw(account_info=False)
        if ticker is not None:
            for r in rows:
                if r.get("pdno") == ticker:
                    return int(r.get("hldg_qty", 0))
            return 0
        res = {}
        for r in rows:
            tkr = r.get("pdno", "")
            if remove_stock_warrant and tkr.startswith("J"):
                continue
            res[tkr] = int(r.get("hldg_qty", 0))
        return res

    def get_holding_stock_detail(self, *, remove_stock_warrant: bool = True):
        """보유 종목 상세 정보 조회 (평가액, 매입가, 손익 포함)

        Returns:
            list[dict]: 보유 종목 상세 정보 리스트
                - pdno: 종목코드
                - prdt_name: 종목명
                - hldg_qty: 보유수량
                - pchs_avg_prc: 매입평균가
                - prpr: 현재가
                - evlu_amt: 평가금액
                - evlu_pfls_amt: 평가손익금액
                - evlu_pfls_rt: 평가손익률
        """
        rows = self._inquire_balance_raw(account_info=False)
        result = []
        for r in rows:
            tkr = r.get("pdno", "")
            if remove_stock_warrant and tkr.startswith("J"):
                continue

            # 수량이 0인 종목 제외
            qty = int(r.get("hldg_qty", 0))
            if qty == 0:
                continue

            result.append({
                "pdno": tkr,
                "prdt_name": r.get("prdt_name", ""),
                "hldg_qty": qty,
                "pchs_avg_prc": float(r.get("pchs_avg_prc", 0)),
                "prpr": float(r.get("prpr", 0)),
                "evlu_amt": float(r.get("evlu_amt", 0)),
                "evlu_pfls_amt": float(r.get("evlu_pfls_amt", 0)),
                "evlu_pfls_rt": float(r.get("evlu_pfls_rt", 0)),
            })
        return result

    def get_holding_cash(self) -> float:
        info = self._inquire_balance_raw(account_info=True)
        try:
            return float(info.get("prvs_rcdl_excc_amt", 0))
        except Exception:
            return 0.0

    # -------------------- 주문 --------------------
    def bid(self, ticker: str, price, quantity, quantity_scale: str):
        if price in {"market", "", 0}:
            ord_unpr = "0"  # 시장가
            ord_dvsn = "01"
            if str(quantity_scale).upper() == "CASH":
                if fdr is None:
                    raise ImportError("FinanceDataReader not installed")
                px = self.get_past_data(ticker).iloc[-1]["close"]
        else:
            px = price
            ord_unpr = str(price)
            ord_dvsn = "00"
        scale = str(quantity_scale).upper()
        if scale == "CASH":
            qty = int(float(quantity) / float(px))
        elif scale == "STOCK":
            qty = int(quantity)
        else:
            print("[ERROR] quantity_scale should be CASH or STOCK")
            return None, 0
        headers = self._header(self._tr("order-buy"))
        params = {
            "CANO": self._account_id,
            "ACNT_PRDT_CD": self._account_suffix,
            "PDNO": ticker,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": ord_unpr,
        }
        url = self._base_url + "/uapi/domestic-stock/v1/trading/order-cash"
        _, data = self._request(url, headers, params, method="post")
        if data.get("rt_cd") == "0":
            if self._env == "vps":
                self._append_transaction_log(ticker, "buy", price, qty)
            return data.get("output", {}).get("ODNO"), qty
        print(data.get("msg1"))
        return None, 0

    def ask(self, ticker: str, price, quantity, quantity_scale: str):
        if price in {"market", "", 0}:
            ord_unpr = "0"
            ord_dvsn = "01"
            if str(quantity_scale).upper() == "CASH":
                if fdr is None:
                    raise ImportError("FinanceDataReader not installed")
                px = self.get_past_data(ticker).iloc[-1]["close"]
        else:
            px = price
            ord_unpr = str(price)
            ord_dvsn = "00"
        scale = str(quantity_scale).upper()
        if scale == "CASH":
            qty = int(float(quantity) / float(px))
        elif scale == "STOCK":
            qty = int(quantity)
        else:
            print("[ERROR] quantity_scale should be CASH or STOCK")
            return None, 0
        headers = self._header(self._tr("order-sell"))
        params = {
            "CANO": self._account_id,
            "ACNT_PRDT_CD": self._account_suffix,
            "PDNO": ticker,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": ord_unpr,
        }
        url = self._base_url + "/uapi/domestic-stock/v1/trading/order-cash"
        _, data = self._request(url, headers, params, method="post")
        if data.get("rt_cd") == "0":
            od = data.get("output", {}).get("ODNO")
            if od is None:
                print("[ERROR] ask: ", data.get("msg1"))
                return None, 0
            if self._env == "vps":
                self._append_transaction_log(ticker, "sell", price, qty)
            return od, qty
        print(data.get("msg1"))
        return None, 0

    # -------------------- 호가 --------------------
    def get_asking_price(self, ticker: str) -> dict:
        """
        실시간 호가 리스트 + 체결 강도 (Web_06)

        Args:
            ticker: 종목코드 (6자리)

        Returns:
            {
                "symbol": "005930",
                "asks": [{"price": int, "quantity": int}, ...],  # 매도 호가 10단계
                "bids": [{"price": int, "quantity": int}, ...],  # 매수 호가 10단계
                "trade_strength": float,  # 체결 강도 (%)
            }
        """
        headers = self._header("FHKST01010200")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
        }
        # KIS OpenAPI 호가조회(inquire-asking-price-exp-ccn) 경로 오타 수정 (끝에 t 제거)
        url = self._base_url + "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
        _, res = self._request(url, headers, params)

        if res.get("rt_cd") != "0":
            return {"error": res.get("msg1", "호가 조회 실패")}

        output1 = res.get("output1", {})

        asks = []
        bids = []
        for i in range(1, 11):
            ask_price = int(output1.get(f"askp{i}", 0) or 0)
            ask_qty = int(output1.get(f"askp_rsqn{i}", 0) or 0)
            bid_price = int(output1.get(f"bidp{i}", 0) or 0)
            bid_qty = int(output1.get(f"bidp_rsqn{i}", 0) or 0)
            if ask_price > 0:
                asks.append({"price": ask_price, "quantity": ask_qty})
            if bid_price > 0:
                bids.append({"price": bid_price, "quantity": bid_qty})

        # 체결 강도: 총 매수 체결량 / (총 매수 + 총 매도) * 100
        sell_vol = int(output1.get("seln_cnqn_smtn", 0) or 0)
        buy_vol = int(output1.get("shnu_cnqn_smtn", 0) or 0)
        total = sell_vol + buy_vol
        trade_strength = round(buy_vol / total * 100, 1) if total > 0 else 50.0

        return {
            "symbol": ticker,
            "asks": asks,
            "bids": bids,
            "trade_strength": trade_strength,
        }

    # -------------------- 미체결 주문 --------------------
    def get_pending_orders(self, ticker: str | None = None) -> list:
        """
        미체결 주문 목록 조회 (Web_06)

        Args:
            ticker: 특정 종목만 필터링 (None이면 전체)

        Returns:
            list[dict]:
                - order_id:          주문번호
                - symbol:            종목코드
                - company_name:      종목명
                - side:              "buy" | "sell"
                - price:             주문 단가
                - pending_quantity:  미체결 잔여 수량
                - ordered_at:        주문 일시 (ISO 형식)
        """
        headers = self._header(self._tr("inquire-pending"))
        out = []
        cont = True
        fk100 = ""
        nk100 = ""

        while cont:
            params = {
                "CANO": self._account_id,
                "ACNT_PRDT_CD": self._account_suffix,
                "CTX_AREA_FK100": fk100,
                "CTX_AREA_NK100": nk100,
                "INQR_DVSN_3": "00",  # 00: 전체
                "INQR_DVSN_1": "",
            }
            url = self._base_url + "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
            hd, res = self._request(url, headers, params)

            if res.get("rt_cd") != "0":
                print(f"[ERROR] get_pending_orders: {res.get('msg1')}")
                break

            cont = hd.get("tr_cont") in {"F", "M"}
            headers["tr_cont"] = "N"
            fk100 = res.get("ctx_area_fk100", "")
            nk100 = res.get("ctx_area_nk100", "")
            out += res.get("output1", [])

        result = []
        for r in out:
            rmn_qty = int(r.get("rmn_qty", 0) or 0)
            if rmn_qty == 0:
                continue

            symbol = r.get("pdno", "")
            if ticker and symbol != ticker:
                continue

            # 주문 일시 파싱
            ord_dt = r.get("ord_dt", "")
            ord_tmd = r.get("ord_tmd", "")
            ordered_at = ""
            if ord_dt and ord_tmd:
                try:
                    ordered_at = (
                        f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}"
                        f"T{ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:6]}"
                    )
                except Exception:
                    ordered_at = ord_dt

            result.append({
                "order_id": r.get("odno", ""),
                "symbol": symbol,
                "company_name": r.get("prdt_name", ""),
                "side": "buy" if r.get("sll_buy_dvsn_cd") == "02" else "sell",
                "price": int(r.get("ord_unpr", 0) or 0),
                "pending_quantity": rmn_qty,
                "ordered_at": ordered_at,
            })

        return result

    def cancel_order(self, order_id: str, ticker: str, quantity: int) -> bool:
        """
        미체결 주문 취소 (Web_06)

        Args:
            order_id:  취소할 주문번호
            ticker:    종목코드
            quantity:  취소 수량 (미체결 잔여 수량 전달 권장)

        Returns:
            True (취소 성공) | False (실패)
        """
        headers = self._header(self._tr("order-cancel"))
        params = {
            "CANO": self._account_id,
            "ACNT_PRDT_CD": self._account_suffix,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_id,
            "PDNO": ticker,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 02: 취소
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        }
        url = self._base_url + "/uapi/domestic-stock/v1/trading/order-rvsecncl"
        _, data = self._request(url, headers, params, method="post")
        if data.get("rt_cd") != "0":
            print(f"[ERROR] cancel_order: {data.get('msg1')}")
            return False
        return True

    # -------------------- VPS 로컬 거래 로그 --------------------
    def _transaction_log_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "transaction_log.json")

    def _append_transaction_log(self, ticker: str, side: str, price, qty: int) -> None:
        """VPS 모드 주문 체결 시 로컬 로그 파일에 기록 (inquire-daily-ccld 대체)"""
        # 연속 주문 후 초당 거래 초과 방지
        time.sleep(0.5)
        price_info = self.get_stock_price(ticker)
        # name이 빈 문자열이거나 ticker와 같으면 1회 재시도
        prdt_name = price_info.get("name", "")
        if not prdt_name or prdt_name == ticker:
            time.sleep(1.0)
            price_info = self.get_stock_price(ticker)
            prdt_name = price_info.get("name", ticker)
        actual_price = (
            float(price_info.get("current_price", 0))
            if price in {"market", "", 0}
            else float(price)
        )
        record = {
            "ord_dt": datetime.now().strftime("%Y%m%d"),
            "pdno": ticker,
            "prdt_name": prdt_name,
            "sll_buy_dvsn_cd": "02" if side == "buy" else "01",
            "sll_buy_dvsn_cd_name": "매수" if side == "buy" else "매도",
            "ord_qty": qty,
            "tot_ccld_qty": qty,
            "avg_prvs": actual_price,
            "tot_ccld_amt": actual_price * qty,
        }
        log_path = self._transaction_log_path()
        try:
            logs = json.load(open(log_path, "r", encoding="utf-8")) if os.path.exists(log_path) else []
            logs.append(record)
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] _append_transaction_log: {e}")

    def _read_transaction_log(self, start_date: str, end_date: str, sll_buy_dvsn: str) -> list:
        """VPS 모드 로컬 거래 로그 날짜·구분 필터링 조회"""
        log_path = self._transaction_log_path()
        if not os.path.exists(log_path):
            return []
        try:
            logs = json.load(open(log_path, "r", encoding="utf-8"))
        except Exception:
            return []
        result = []
        for r in logs:
            ord_dt = r.get("ord_dt", "")
            if start_date and ord_dt < start_date:
                continue
            if end_date and ord_dt > end_date:
                continue
            if sll_buy_dvsn != "00" and r.get("sll_buy_dvsn_cd") != sll_buy_dvsn:
                continue
            result.append(r)
        return result

    def _calc_avg_from_log(self, symbol: str) -> float:
        """
        로컬 거래 로그 기반 평균매수가 계산 (VPS avg_price=0 fallback)

        한국 증권 가중평균 방식:
          - 매수 시마다 누적 평단 갱신
          - 매도는 평단에 영향 없음 (수량만 감소)

        Returns:
            계산된 평단가 (float). 로그 없거나 매수 기록 없으면 0.0
        """
        log_path = self._transaction_log_path()
        if not os.path.exists(log_path):
            return 0.0
        try:
            logs = json.load(open(log_path, "r", encoding="utf-8"))
        except Exception:
            return 0.0

        # 한국 증권 가중평균법:
        # - 매수: (기존평단 * 기존수량 + 매수단가 * 매수수량) / 신규총수량
        # - 매도: 평단 불변, 수량만 감소
        running_qty = 0
        running_avg = 0.0
        for r in logs:
            if r.get("pdno") != symbol:
                continue
            qty = int(r.get("tot_ccld_qty", 0))
            price = float(r.get("avg_prvs", 0))
            if r.get("sll_buy_dvsn_cd") == "02":  # 매수
                new_qty = running_qty + qty
                running_avg = (running_avg * running_qty + price * qty) / new_qty
                running_qty = new_qty
            else:  # 매도 — 평단 불변, 수량만 감소
                running_qty = max(0, running_qty - qty)

        if running_qty <= 0 or running_avg == 0:
            return 0.0
        return round(running_avg, 2)

    # -------------------- 거래내역 조회 --------------------
    def get_transaction_history(
        self,
        start_date: str = None,
        end_date: str = None,
        period: str = "1m",
        sll_buy_dvsn: str = "00"
    ) -> list:
        """
        주식일별주문체결조회 (기획 2-4: 거래 내역 리포트)

        Args:
            start_date: 조회시작일자 (YYYYMMDD), None이면 period로 계산
            end_date: 조회종료일자 (YYYYMMDD), None이면 오늘
            period: 기간 ("1m": 1개월, "3m": 3개월, "1y": 1년)
            sll_buy_dvsn: 매도매수구분 ("00":전체, "01":매도, "02":매수)

        Returns:
            list[dict]: 거래내역 리스트
                - ord_dt: 주문일자
                - pdno: 종목코드
                - prdt_name: 종목명
                - sll_buy_dvsn_cd: 매도매수구분 (01:매도, 02:매수)
                - sll_buy_dvsn_cd_name: 매도매수구분명
                - ord_qty: 주문수량
                - tot_ccld_qty: 총체결수량
                - avg_prvs: 체결평균가
                - tot_ccld_amt: 총체결금액
        """
        # 날짜 계산
        today = datetime.now()
        if end_date is None:
            end_date = today.strftime("%Y%m%d")

        if start_date is None:
            period_map = {
                "1w": relativedelta(weeks=1),
                "1m": relativedelta(months=1),
                "3m": relativedelta(months=3),
                "1y": relativedelta(years=1),
            }
            delta = period_map.get(period, relativedelta(months=1))
            start_date = (today - delta).strftime("%Y%m%d")

        # VPS(모의투자) 모드: KIS API output1 미지원 → 로컬 로그 사용
        if self._env == "vps":
            return self._read_transaction_log(start_date, end_date, sll_buy_dvsn)

        headers = self._header(self._tr("inquire-daily-ccld"))
        out = []
        cont = True
        fk100 = ""
        nk100 = ""

        while cont:
            params = {
                "CANO": self._account_id,
                "ACNT_PRDT_CD": self._account_suffix,
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "SLL_BUY_DVSN_CD": sll_buy_dvsn,
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": fk100,
                "CTX_AREA_NK100": nk100,
            }
            url = self._base_url + "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
            hd, res = self._request(url, headers, params)

            if res.get("rt_cd") != "0":
                print(f"[ERROR] get_transaction_history: {res.get('msg1')}")
                break

            cont = hd.get("tr_cont") in {"F", "M"}
            headers["tr_cont"] = "N"
            fk100 = res.get("ctx_area_fk100", "")
            nk100 = res.get("ctx_area_nk100", "")
            out += res.get("output1", [])

        # 필요한 필드만 추출하여 정리
        result = []
        for r in out:
            # 체결수량이 0인 건 제외 (미체결)
            ccld_qty = int(r.get("tot_ccld_qty", 0))
            if ccld_qty == 0:
                continue

            result.append({
                "ord_dt": r.get("ord_dt", ""),
                "pdno": r.get("pdno", ""),
                "prdt_name": r.get("prdt_name", ""),
                "sll_buy_dvsn_cd": r.get("sll_buy_dvsn_cd", ""),
                "sll_buy_dvsn_cd_name": r.get("sll_buy_dvsn_cd_name", ""),
                "ord_qty": int(r.get("ord_qty", 0)),
                "tot_ccld_qty": ccld_qty,
                "avg_prvs": float(r.get("avg_prvs", 0)),
                "tot_ccld_amt": float(r.get("tot_ccld_amt", 0)),
            })

        return result

    def _get_prod_token(self, prod_key: str, prod_secret: str) -> str:
        """실전 API 토큰 조회 (24시간 캐시, 프로세스 내 공유)"""
        global _prod_token, _prod_token_time
        if _prod_token and (time.time() - _prod_token_time) < 86400:
            return _prod_token
        url = _PROD_BASE_URL + "/oauth2/tokenP"  # 여기도 수정 (만약을 위해)
        body = {
            "grant_type": "client_credentials",
            "appkey": prod_key,
            "appsecret": prod_secret,
        }
        res = requests.post(
            url,
            headers={"content-type": "application/json"},
            data=json.dumps(body),
            timeout=30,
        )
        token = res.json().get("access_token", "")
        if token:
            _prod_token = token
            _prod_token_time = time.time()
        return token

    def get_market_ranking(
        self,
        category: str = "volume",
        market: str = "J",
        limit: int = 5,
    ) -> list:
        """
        국내주식 순위 조회 (거래량 / 등락률)

        모의(vps) 환경에서 KIS_APP_KEY_PROD / KIS_APP_SECRET_PROD 가 .env에
        설정되어 있으면 실전 API로 조회합니다 (랭킹 API는 실전만 지원).
        설정되지 않은 경우 현재 환경(vps)의 인증정보로 시도합니다.

        Args:
            category: "volume" (거래량 순위) | "return" (등락률 순위)
            market:   "J" (전체) | "0001" (코스피) | "1001" (코스닥)
            limit:    상위 N개 (기본 5)

        Returns:
            list[dict]:
                - symbol:        종목코드
                - company_name:  종목명
                - current_price: 현재가
                - change_rate:   전일대비율 (%)
                - volume:        누적거래량
        """
        if category == "volume":
            tr_id = "FHPST01710000"
            path = "/uapi/domestic-stock/v1/ranking/volume"
            scr_div = "20171"
            sort_cls = "0"
        else:
            tr_id = "FHPST01700000"
            path = "/uapi/domestic-stock/v1/ranking/fluctuation"
            scr_div = "20170"
            sort_cls = "0"

        # 모의 환경이고 실전 키가 별도로 있으면 실전 엔드포인트 사용
        prod_key = os.getenv("KIS_APP_KEY_PROD", "").strip()
        prod_secret = os.getenv("KIS_APP_SECRET_PROD", "").strip()

        if self._env == "vps" and prod_key and prod_secret:
            base_url = _PROD_BASE_URL
            token = self._get_prod_token(prod_key, prod_secret)
            headers = {
                "content-type": "application/json",
                "appkey": prod_key,
                "appsecret": prod_secret,
                "authorization": f"Bearer {token}",
                "tr_id": tr_id,
            }
        else:
            base_url = self._base_url
            headers = self._header(tr_id)

        params = {
            "FID_COND_MRKT_DIV_CODE":  market,
            "FID_COND_SCR_DIV_CODE":   scr_div,
            "FID_INPUT_ISCD":          "0000",
            "FID_RANK_SORT_CLS_CODE":  sort_cls,
            "FID_INPUT_CNT_1":         "0",
            "FID_PRC_CLS_CODE":        "0",
            "FID_INPUT_PRICE_1":       "",
            "FID_INPUT_PRICE_2":       "",
            "FID_VOL_CNT":             "",
            "FID_TRGT_CLS_CODE":       "0",
            "FID_TRGT_EXLS_CLS_CODE":  "0",
            "FID_DIV_CLS_CODE":        "0",
            "FID_RSFL_RATE1":          "",
            "FID_RSFL_RATE2":          "",
        }

        _, res = self._request(base_url + path, headers, params)
        if res.get("rt_cd") != "0":
            print(f"[WARN] get_market_ranking: {res.get('msg1')}")
            return []

        result = []
        for item in res.get("output", [])[:limit]:
            result.append({
                "symbol":        item.get("stck_shrn_iscd", ""),
                "company_name":  item.get("hts_kor_isnm", ""),
                "current_price": int(item.get("stck_prpr", 0)),
                "change_rate":   float(item.get("prdy_ctrt", 0)),
                "volume":        int(item.get("acml_vol", 0)),
            })
        return result

    def get_transaction_summary(self, period: str = "1m") -> dict:
        """
        거래내역 요약 (기획 2-4, 2-6 지원)

        Args:
            period: 기간 ("1w": 1주일, "1m": 1개월, "3m": 3개월, "1y": 1년)

        Returns:
            dict: 거래 요약 정보
                - period: 조회기간
                - total_buy_amount: 총 매수금액
                - total_sell_amount: 총 매도금액
                - total_trades: 총 거래건수
                - buy_trades: 매수 거래건수
                - sell_trades: 매도 거래건수
                - by_stock: 종목별 거래 요약
        """
        transactions = self.get_transaction_history(period=period)

        total_buy = 0
        total_sell = 0
        buy_count = 0
        sell_count = 0
        by_stock = {}

        for t in transactions:
            pdno = t["pdno"]
            prdt_name = t["prdt_name"]
            amt = t["tot_ccld_amt"]
            qty = t["tot_ccld_qty"]
            is_buy = t["sll_buy_dvsn_cd"] == "02"

            if is_buy:
                total_buy += amt
                buy_count += 1
            else:
                total_sell += amt
                sell_count += 1

            # 종목별 집계
            if pdno not in by_stock:
                by_stock[pdno] = {
                    "prdt_name": prdt_name,
                    "buy_amount": 0,
                    "sell_amount": 0,
                    "buy_qty": 0,
                    "sell_qty": 0,
                    "trades": 0,
                }

            by_stock[pdno]["trades"] += 1
            if is_buy:
                by_stock[pdno]["buy_amount"] += amt
                by_stock[pdno]["buy_qty"] += qty
            else:
                by_stock[pdno]["sell_amount"] += amt
                by_stock[pdno]["sell_qty"] += qty

        # 종목별 수익률 계산 (매도금액 - 매수금액)
        for pdno, data in by_stock.items():
            if data["buy_amount"] > 0:
                profit = data["sell_amount"] - data["buy_amount"]
                data["realized_profit"] = profit
                data["profit_rate"] = round((profit / data["buy_amount"]) * 100, 2) if data["buy_amount"] > 0 else 0
            else:
                data["realized_profit"] = data["sell_amount"]
                data["profit_rate"] = 0

        return {
            "period": period,
            "total_buy_amount": total_buy,
            "total_sell_amount": total_sell,
            "net_amount": total_sell - total_buy,
            "total_trades": len(transactions),
            "buy_trades": buy_count,
            "sell_trades": sell_count,
            "by_stock": by_stock,
        }


if __name__ == "__main__":
    # .env 기반 기본 실행 (모의: KIS_ENV=vps, 실전: KIS_ENV=prod)
    try:
        h = HantuStock()
        print("현금:", h.get_holding_cash())
        print("보유종목:", h.get_holding_stock())
        # 간단 주문 테스트 (시장가, 1주)
        # od_buy, q1 = h.bid("005930", "market", 1, "STOCK")
        # print("매수주문:", od_buy, q1)
        # od_sell, q2 = h.ask("005930", "market", 1, "STOCK")
        # print("매도주문:", od_sell, q2)
    except Exception as e:
        print("[MAIN]", e)
