"""
종목 리스트 데이터 조회 모듈
웹 기획서 Web_01 - 종목 탐색 메인 화면용
"""

from datetime import datetime
from typing import Dict, List, Optional

try:
    # Django 환경을 위한 상대 경로 임포트
    from .HantuStock import HantuStock
except ImportError:
    # 직접 스크립트로 실행할 때를 위한 절대 경로 임포트
    try:
        from .HantuStock import HantuStock
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

    # ==================== KIS 랭킹 API ====================

    def _get_ranking_stocks(self, category: str, limit: int) -> Dict:
        """
        KIS 랭킹 API로 거래량/등락률 순위 조회

        Args:
            category: "volume" (거래량) | "return" (등락률)
            limit: 조회 개수

        Returns:
            get_market_stocks()와 동일한 스키마
        """
        if not self._hantu:
            return {"error": "HantuStock 초기화 필요"}
        try:
            today = datetime.now().strftime("%Y%m%d")
            rankings = self._hantu.get_market_ranking(category=category, limit=limit)
            
            # rankings가 에러 딕셔너리인 경우 그대로 반환
            if isinstance(rankings, dict) and "error" in rankings:
                return rankings
                
            if not rankings:
                return {"error": "랭킹 데이터 없음 (장외 시간 또는 API 오류)"}

            stocks = [
                {
                    "ticker": item.get("symbol", ""),
                    "name": item.get("company_name", ""),
                    "current_price": item.get("current_price", 0),
                    "change_rate": item.get("change_rate", 0),
                    "volume": item.get("volume", 0),
                }
                for item in rankings
            ]
            return {"date": today, "market": "ALL", "count": len(stocks), "stocks": stocks}
        except Exception as e:
            return {"error": str(e)}

    # ==================== 정렬 기능 ====================

    @staticmethod
    def sort_stocks(stocks: List[Dict], sort_by: str = "price", order: str = "desc") -> List[Dict]:
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
        sort_by: str = "volume",
        order: str = "desc",
        limit: int = 100
    ) -> Dict:
        """
        정렬된 시장 종목 리스트 조회

        Args:
            market: 시장 구분 (KOSPI, KOSDAQ, ALL)
            sort_by: 정렬 기준 (change_rate, volume)
            order: 정렬 순서 (asc, desc)
            limit: 최대 조회 개수

        Returns:
            dict: 정렬된 종목 리스트
        """
        if sort_by == "volume":
            category = "volume"
        elif sort_by == "change_rate":
            category = "return"
        else:
            return {"error": f"지원하지 않는 정렬 기준입니다: {sort_by} (volume, change_rate만 지원)"}

        ranking_result = self._get_ranking_stocks(category=category, limit=limit)
        used_fallback = False

        # 거래량 ranking endpoint 는 장외 / 주말 / 공휴일에 빈 응답이 옴 (KIS 정책).
        # 등락률 ranking 은 그 시점에도 마지막 평일 스냅샷이 살아있음 → fluctuation
        # 결과를 받아 volume 으로 재정렬하면 같은 종목 pool 에서 의미있는 응답 가능.
        if sort_by == "volume" and (
            ("error" in ranking_result)
            or not ranking_result.get("stocks")
        ):
            fallback = self._get_ranking_stocks(category="return", limit=limit)
            if "error" not in fallback and fallback.get("stocks"):
                ranking_result = fallback
                used_fallback = True
            else:
                # fluctuation 도 비어있으면 원래 에러 그대로
                return ranking_result if "error" in ranking_result else {"error": "ranking 데이터 없음"}
        elif "error" in ranking_result:
            return ranking_result

        # 안전망 post-filter — KIS FID 필터로도 못 거르는 비정상 종목 차단:
        # 정상 일일 등락률 한계 ±30% 보다 큰 절댓값 → 정지해제 폭락/폭등 분명.
        # 거래량 1만주 미만도 정지/저유동 의심 → 컷.
        filtered = [
            s for s in ranking_result.get("stocks", [])
            if abs(s.get("change_rate", 0) or 0) <= 30.5
            and (s.get("volume", 0) or 0) >= 10000
        ]
        ranking_result["stocks"] = filtered

        # KIS 랭킹 API 가 카테고리 (return/volume) "순위" 라고 주지만 실제 단조 정렬은
        # 보장되지 않음 → 우리가 직접 해당 필드로 재정렬해 응답 정합성 확보.
        ranking_result["stocks"] = self.sort_stocks(
            ranking_result["stocks"], sort_by=sort_by, order=order
        )
        ranking_result["count"] = len(ranking_result["stocks"])
        ranking_result["sort_by"] = sort_by
        ranking_result["order"] = order
        if used_fallback:
            # 프론트가 "장외 시간 — 등락률 기준 종목 풀에서 거래량 순" 같은 라벨을 표시 가능
            ranking_result["data_source"] = "fluctuation_fallback"
        return ranking_result

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
            except Exception:
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
