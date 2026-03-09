"""
종목 리스트 데이터 조회 모듈
웹 기획서 Web_01 - 종목 탐색 메인 화면용
"""

import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional

try:
    from pykrx import stock as pykrx_stock
except ImportError:
    pykrx_stock = None

try:
    from HantuStock import HantuStock
except ImportError:
    HantuStock = None


class StockListDataProvider:
    """종목 리스트 데이터 제공 클래스"""

    def __init__(self, hantu_stock: Optional[HantuStock] = None):
        """
        Args:
            hantu_stock: HantuStock 인스턴스 (선택). 보유 종목 조회용
        """
        self._hantu = hantu_stock
        if hantu_stock is None and HantuStock is not None:
            try:
                self._hantu = HantuStock()
            except Exception as e:
                print(f"[WARN] HantuStock 초기화 실패: {e}. 보유 종목 조회 제한됨.")

    # ==================== 전체 종목 리스트 ====================

    def get_market_stocks(self, market: str = "ALL", limit: int = 100) -> Dict:
        """
        시장 전체 종목 리스트 조회

        Args:
            market: 시장 구분 (KOSPI, KOSDAQ, ALL)
            limit: 최대 조회 개수

        Returns:
            dict: 종목 리스트 (ticker, name, current_price, change_rate, volume)
        """
        if pykrx_stock is None:
            return {"error": "pykrx가 설치되지 않았습니다. pip install pykrx"}

        try:
            # 최근 거래일 찾기
            today = datetime.now().strftime("%Y%m%d")
            recent_dates = pykrx_stock.get_previous_business_days(year=datetime.now().year)

            if not recent_dates.empty:
                date_str = recent_dates.iloc[-1].strftime("%Y%m%d")
            else:
                date_str = today

            # 시장별 OHLCV 조회
            if market == "ALL":
                df_kospi = pykrx_stock.get_market_ohlcv(date_str, market="KOSPI")
                df_kosdaq = pykrx_stock.get_market_ohlcv(date_str, market="KOSDAQ")
                df = pd.concat([df_kospi, df_kosdaq])
            else:
                df = pykrx_stock.get_market_ohlcv(date_str, market=market)

            if df.empty:
                return {"error": "데이터가 없습니다", "date": date_str}

            # 등락률 계산
            df['change_rate'] = df['등락률']
            df['volume'] = df['거래량']
            df['current_price'] = df['종가']

            # 티커별 종목명 조회
            tickers = df.index.tolist()
            names = {}
            for ticker in tickers[:limit]:
                try:
                    name = pykrx_stock.get_market_ticker_name(ticker)
                    names[ticker] = name
                except:
                    names[ticker] = ticker

            # 결과 생성
            stocks = []
            for ticker in tickers[:limit]:
                row = df.loc[ticker]
                stocks.append({
                    "ticker": ticker,
                    "name": names.get(ticker, ticker),
                    "current_price": int(row['current_price']),
                    "change_rate": round(float(row['change_rate']), 2),
                    "volume": int(row['volume'])
                })

            return {
                "date": date_str,
                "market": market,
                "count": len(stocks),
                "stocks": stocks
            }

        except Exception as e:
            return {"error": str(e)}

    # ==================== 정렬 기능 ====================

    def sort_stocks(self, stocks: List[Dict], sort_by: str = "price", order: str = "desc") -> List[Dict]:
        """
        종목 리스트 정렬

        Args:
            stocks: 종목 리스트
            sort_by: 정렬 기준 (price, change_rate, volume, name)
            order: 정렬 순서 (asc, desc)

        Returns:
            list: 정렬된 종목 리스트
        """
        sort_keys = {
            "price": "current_price",
            "change_rate": "change_rate",
            "volume": "volume",
            "name": "name"
        }

        key = sort_keys.get(sort_by, "current_price")
        reverse = (order == "desc")

        return sorted(stocks, key=lambda x: x.get(key, 0), reverse=reverse)

    def get_sorted_market_stocks(
        self,
        market: str = "ALL",
        sort_by: str = "price",
        order: str = "desc",
        limit: int = 100
    ) -> Dict:
        """
        정렬된 시장 종목 리스트 조회

        Args:
            market: 시장 구분 (KOSPI, KOSDAQ, ALL)
            sort_by: 정렬 기준 (price, change_rate, volume)
            order: 정렬 순서 (asc, desc)
            limit: 최대 조회 개수

        Returns:
            dict: 정렬된 종목 리스트
        """
        result = self.get_market_stocks(market, limit=500)  # 정렬 전 충분히 가져옴

        if "error" in result:
            return result

        stocks = result["stocks"]
        sorted_stocks = self.sort_stocks(stocks, sort_by, order)

        return {
            "date": result["date"],
            "market": market,
            "sort_by": sort_by,
            "order": order,
            "count": min(len(sorted_stocks), limit),
            "stocks": sorted_stocks[:limit]
        }

    # ==================== 보유 종목 리스트 ====================

    def get_holding_stocks(self, sort_by: str = "eval_amount", order: str = "desc") -> Dict:
        """
        보유 종목 리스트 조회 (계좌 연동 필요)

        Args:
            sort_by: 정렬 기준 (eval_amount, profit_rate, quantity, name)
            order: 정렬 순서 (asc, desc)

        Returns:
            dict: 보유 종목 리스트
        """
        if not self._hantu:
            return {"error": "계좌 연동이 필요합니다. HantuStock 초기화 필요."}

        try:
            holdings = self._hantu.get_holding_stock_detail()

            if not holdings:
                return {
                    "count": 0,
                    "stocks": [],
                    "message": "보유 종목이 없습니다"
                }

            # 필드명 변환
            stocks = []
            for h in holdings:
                stocks.append({
                    "ticker": h.get("pdno", ""),
                    "name": h.get("prdt_name", ""),
                    "quantity": h.get("hldg_qty", 0),
                    "avg_price": h.get("pchs_avg_prc", 0),
                    "current_price": h.get("prpr", 0),
                    "eval_amount": h.get("evlu_amt", 0),
                    "profit_amount": h.get("evlu_pfls_amt", 0),
                    "profit_rate": h.get("evlu_pfls_rt", 0)
                })

            # 정렬
            sort_keys = {
                "eval_amount": "eval_amount",
                "profit_rate": "profit_rate",
                "quantity": "quantity",
                "name": "name"
            }
            key = sort_keys.get(sort_by, "eval_amount")
            reverse = (order == "desc")
            stocks = sorted(stocks, key=lambda x: x.get(key, 0), reverse=reverse)

            # 총 평가금액 계산
            total_eval = sum(s["eval_amount"] for s in stocks)
            total_profit = sum(s["profit_amount"] for s in stocks)

            return {
                "count": len(stocks),
                "total_eval_amount": total_eval,
                "total_profit_amount": total_profit,
                "sort_by": sort_by,
                "order": order,
                "stocks": stocks
            }

        except Exception as e:
            return {"error": str(e)}

    # ==================== 관심 종목 (DB 연동 필요) ====================

    def get_watchlist_stocks(self, user_id: str, tickers: List[str]) -> Dict:
        """
        관심 종목 리스트 조회

        Args:
            user_id: 사용자 ID
            tickers: 관심 종목 티커 리스트 (DB에서 조회한 값)

        Returns:
            dict: 관심 종목 리스트 (현재가 정보 포함)
        """
        if not tickers:
            return {
                "count": 0,
                "stocks": [],
                "message": "관심 종목이 없습니다"
            }

        stocks = []
        for ticker in tickers:
            try:
                if self._hantu:
                    data = self._hantu.get_stock_price(ticker)
                    if "error" not in data:
                        stocks.append({
                            "ticker": ticker,
                            "name": data.get("name", ticker),
                            "current_price": data.get("current_price", 0),
                            "change_rate": data.get("change_rate", 0),
                            "volume": data.get("volume", 0)
                        })
            except:
                continue

        return {
            "user_id": user_id,
            "count": len(stocks),
            "stocks": stocks
        }


# 테스트
if __name__ == "__main__":
    provider = StockListDataProvider()

    print("=" * 50)
    print("종목 리스트 테스트")
    print("=" * 50)

    # 전체 종목 리스트 (상승률순)
    print("\n[1] 상승률 상위 종목:")
    result = provider.get_sorted_market_stocks(
        market="KOSPI",
        sort_by="change_rate",
        order="desc",
        limit=10
    )
    if "error" not in result:
        for i, stock in enumerate(result["stocks"][:5], 1):
            print(f"  {i}. {stock['name']} ({stock['ticker']}): {stock['current_price']:,}원 ({stock['change_rate']:+.2f}%)")
    else:
        print(f"  에러: {result['error']}")

    # 거래량 상위
    print("\n[2] 거래량 상위 종목:")
    result = provider.get_sorted_market_stocks(
        market="KOSPI",
        sort_by="volume",
        order="desc",
        limit=10
    )
    if "error" not in result:
        for i, stock in enumerate(result["stocks"][:5], 1):
            print(f"  {i}. {stock['name']} ({stock['ticker']}): 거래량 {stock['volume']:,}")
    else:
        print(f"  에러: {result['error']}")

    # 보유 종목
    print("\n[3] 보유 종목:")
    result = provider.get_holding_stocks()
    if "error" not in result:
        print(f"  총 평가금액: {result.get('total_eval_amount', 0):,}원")
        for stock in result["stocks"][:3]:
            print(f"  - {stock['name']}: {stock['quantity']}주, 수익률 {stock['profit_rate']:+.2f}%")
    else:
        print(f"  {result.get('error', result.get('message', ''))}")
