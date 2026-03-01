from __future__ import annotations

import os
import pandas as pd
import time
import requests
import json
from datetime import datetime

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
        self._api_key = api_key or os.getenv("KIS_APP_KEY", "").strip()
        self._secret_key = secret_key or os.getenv("KIS_APP_SECRET", "").strip()
        self._account_id = account_id or os.getenv("KIS_ACCOUNT_ID", "").strip()
        self._account_suffix = os.getenv("KIS_ACCOUNT_SUFFIX", "01").strip() or "01"

        _env = (env or os.getenv("KIS_ENV", "prod")).strip().lower()
        if _env not in {"prod", "vps", "paper", "demo", "sandbox", "vts"}:
            raise ValueError("env must be one of {'prod','vps'}; alias {'paper','demo','sandbox'} allowed")
        # alias 처리
        self._env = "vps" if _env in {"vps", "paper", "demo", "sandbox", "vts"} else "prod"

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
        }
        return prefix + codes[key]

    def _get_access_token(self) -> str:
        token_path = "/oauth2/token" if self._env == "prod" else "/oauth2/tokenP"
        url = self._base_url + token_path
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self._api_key,
            "appsecret": self._secret_key,
        }
        backoff = 0.5
        for attempt in range(6):
            try:
                res = requests.post(url, headers=headers, data=json.dumps(body), timeout=30)
                data = res.json()
                if "access_token" in data:
                    return data["access_token"]
                # error message
                print(f"[WARN] token error: {data}")
            except Exception as e:
                print(f"[ERROR] get_access_token: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
        raise RuntimeError("Failed to get access token after retries")

    def _header(self, tr_id: str) -> dict:
        return {
            "content-type": "application/json",
            "appkey": self._api_key,
            "appsecret": self._secret_key,
            "authorization": f"Bearer {self._access_token}",
            "tr_id": tr_id,
        }


    def _request(self, url: str, headers: dict, params: dict, *, method: str = "get"):
        backoff = 0.5
        for attempt in range(6):
            try:
                if method == "get":
                    resp = requests.get(url, headers=headers, params=params, timeout=30)
                else:
                    resp = requests.post(url, headers=headers, data=json.dumps(params), timeout=30)
                r_headers = resp.headers
                data = resp.json()
                if data.get("rt_cd") != "0":
                    # 과호출 제한 등 재시도 케이스
                    if data.get("msg_cd") in {"EGW00201", "EGW00123"}:  # throttling 등
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 5.0)
                        continue
                return r_headers, data
            except requests.exceptions.ConnectTimeout:
                print(f"[WARN] connect timeout, retry {attempt+1}")
            except requests.exceptions.ReadTimeout:
                print(f"[WARN] read timeout, retry {attempt+1}")
            except Exception as e:
                print(f"[WARN] request error: {e}, retry {attempt+1}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)
        return {}, {"rt_cd": "1", "msg1": "request failed after retries"}

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
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker,
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
            "fid_etc_cls_code": "",
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker,
            "fid_input_hour_1": end_time,
            "fid_pw_data_incu_yn": "N",
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
            params["fid_input_hour_1"] = last_time

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
            return od, qty
        print(data.get("msg1"))
        return None, 0

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
                "1m": relativedelta(months=1),
                "3m": relativedelta(months=3),
                "1y": relativedelta(years=1),
            }
            delta = period_map.get(period, relativedelta(months=1))
            start_date = (today - delta).strftime("%Y%m%d")

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

    def get_transaction_summary(self, period: str = "1m") -> dict:
        """
        거래내역 요약 (기획 2-4, 2-6 지원)

        Args:
            period: 기간 ("1m": 1개월, "3m": 3개월, "1y": 1년)

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
