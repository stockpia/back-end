"""
Web_06 매수/매도 주문 API
웹 기획에 맞춘 호가 조회, 주문 실행, 미체결 관리

기획:
- 좌측 패널: 실시간 호가 리스트 + 체결 강도
- 우측 패널: 매수/매도 주문 폼 (지정가/현재가/시장가)
- 우측 패널 전환: 체결 대기 목록 (미체결 주문 관리)
- 계좌 연동 필수
"""

from typing import Dict, List, Optional
from dotenv import load_dotenv

from .HantuStock import HantuStock

load_dotenv()


class WebOrder:
    """
    Web_06 매수/매도 주문 데이터 프로바이더

    기능:
    - get_order_book(symbol): 호가 리스트 + 체결 강도
    - get_account_info(symbol): 예수금 + 보유 수량 (주문 폼 초기값)
    - place_order(symbol, side, order_type, price, quantity): 매수/매도 주문
    - get_pending_orders(symbol): 미체결 주문 목록
    - cancel_order(order_id, symbol, quantity): 주문 취소
    """

    def __init__(self):
        """Initialize"""
        self.hantu = HantuStock()

    # ========================================
    # 호가 리스트
    # ========================================

    def get_order_book(self, symbol: str) -> Dict:
        """
        실시간 호가 리스트 + 체결 강도

        Args:
            symbol: 종목코드 (예: "005930")

        Returns:
            {
                "symbol": "005930",
                "asks": [{"price": int, "quantity": int}, ...],  # 매도 호가 (내림차순)
                "bids": [{"price": int, "quantity": int}, ...],  # 매수 호가 (내림차순)
                "trade_strength": float,  # 체결 강도 (%)
            }
        """
        result = self.hantu.get_asking_price(symbol)

        if "error" in result:
            return self._error_response(result["error"])

        return result

    # ========================================
    # 계좌 정보 (주문 폼 초기값)
    # ========================================

    def get_account_info(self, symbol: str) -> Dict:
        """
        주문 폼에 필요한 계좌 정보 제공

        매수 폼: 예수금 (퍼센트 수량 계산용)
        매도 폼: 보유 수량 + 평단가 (예상 손익 계산용)

        Args:
            symbol: 종목코드

        Returns:
            {
                "available_cash": int,         # 예수금
                "holding_quantity": int,       # 보유 수량
                "average_price": float,        # 평단가
                "current_price": int,          # 현재가
            }
        """
        cash = self.hantu.get_holding_cash()
        current_price_info = self.hantu.get_stock_price(symbol)
        current_price = current_price_info.get("current_price", 0)

        # 보유 종목 상세 (평단가 포함)
        holdings = self.hantu.get_holding_stock_detail()
        holding_qty = 0
        average_price = 0.0
        for h in holdings:
            if h["pdno"] == symbol:
                holding_qty = h["hldg_qty"]
                average_price = float(h["pchs_avg_prc"])
                if average_price == 0:   # VPS: pchs_avg_prc 항상 0.0 → 로컬 로그 기반 계산
                    average_price = self.hantu._calc_avg_from_log(symbol)
                break

        return {
            "available_cash": int(cash),
            "holding_quantity": holding_qty,
            "average_price": average_price,
            "current_price": current_price,
        }

    # ========================================
    # 주문 실행
    # ========================================

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        price: int,
        quantity: int,
    ) -> Dict:
        """
        매수/매도 주문 실행

        Args:
            symbol:     종목코드 (예: "005930")
            side:       "buy" (매수) | "sell" (매도)
            order_type: "limit" (지정가) | "market" (시장가) | "current" (현재가)
            price:      주문 단가 (시장가일 경우 무시)
            quantity:   주문 수량

        Returns:
            {
                "success": bool,
                "order_id": str,
                "symbol": str,
                "side": str,
                "order_type": str,
                "price": int,
                "quantity": int,
                "message": str,
            }
        """
        # 현재가 탭 = 지정가 처리
        if order_type == "current":
            order_type = "limit"

        # 시장가 처리
        if order_type == "market":
            api_price = "market"
        else:
            api_price = price

        if side == "buy":
            order_id, qty = self.hantu.bid(symbol, api_price, quantity, "STOCK")
        else:
            order_id, qty = self.hantu.ask(symbol, api_price, quantity, "STOCK")

        if order_id:
            side_label = "매수" if side == "buy" else "매도"
            return {
                "success": True,
                "order_id": order_id,
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "price": price,
                "quantity": qty,
                "message": f"{side_label} 주문이 접수되었습니다",
            }
        else:
            return {
                "success": False,
                "message": "주문 접수에 실패했습니다. 잔고 또는 보유 수량을 확인해주세요.",
            }

    # ========================================
    # 미체결 주문 관리
    # ========================================

    def get_pending_orders(self, symbol: Optional[str] = None) -> Dict:
        """
        미체결 주문 목록 조회

        Args:
            symbol: 특정 종목만 필터링 (None이면 전체)

        Returns:
            {
                "pending_orders": [
                    {
                        "order_id": str,
                        "symbol": str,
                        "company_name": str,
                        "side": "buy" | "sell",
                        "price": int,
                        "pending_quantity": int,
                        "ordered_at": str,
                    },
                    ...
                ],
                "total_count": int,
            }
        """
        orders = self.hantu.get_pending_orders(ticker=symbol)
        return {
            "pending_orders": orders,
            "total_count": len(orders),
        }

    def cancel_order(self, order_id: str, symbol: str, quantity: int) -> Dict:
        """
        미체결 주문 취소

        Args:
            order_id:  취소할 주문번호
            symbol:    종목코드
            quantity:  취소 수량

        Returns:
            {
                "success": bool,
                "order_id": str,
                "message": str,
            }
        """
        success = self.hantu.cancel_order(order_id, symbol, quantity)
        if success:
            return {
                "success": True,
                "order_id": order_id,
                "message": "주문이 취소되었습니다",
            }
        else:
            return {
                "success": False,
                "order_id": order_id,
                "message": "주문 취소에 실패했습니다",
            }

    # ========================================
    # 내부 헬퍼
    # ========================================

    def _error_response(self, reason: str) -> Dict:
        return {"error": reason}


# ========================================
# 테스트
# ========================================

if __name__ == "__main__":
    import json

    print("=" * 60)
    print("Web_06 매수/매도 주문 테스트")
    print("=" * 60)
    print()

    web = WebOrder()
    symbol = "005930"

    # 1. 호가 리스트
    print("[1단계] 호가 리스트")
    print("-" * 40)
    book = web.get_order_book(symbol)
    if "error" not in book:
        print(f"  매도 호가 (상위 3): {book['asks'][:3]}")
        print(f"  매수 호가 (상위 3): {book['bids'][:3]}")
        print(f"  체결 강도: {book['trade_strength']}%")
    else:
        print(f"  오류: {book['error']}")
    print()

    # 2. 계좌 정보
    print("[2단계] 계좌 정보")
    print("-" * 40)
    account = web.get_account_info(symbol)
    print(f"  예수금: {account['available_cash']:,.0f}원")
    print(f"  보유 수량: {account['holding_quantity']}주")
    print(f"  평단가: {account['average_price']:,.0f}원")
    print(f"  현재가: {account['current_price']:,.0f}원")
    print()

    # 3. 미체결 주문 목록
    print("[3단계] 미체결 주문 목록")
    print("-" * 40)
    pending = web.get_pending_orders(symbol)
    print(f"  미체결 건수: {pending['total_count']}건")
    for o in pending["pending_orders"]:
        side_label = "매수" if o["side"] == "buy" else "매도"
        print(f"  [{side_label}] {o['company_name']} {o['pending_quantity']}주 @ {o['price']:,}원 (주문번호: {o['order_id']})")
    print()

    # 4. 테스트 매수 (지정가, 현재가 기준 -1호가)
    if not book.get("error") and book.get("bids"):
        test_price = book["bids"][0]["price"] - 100
        print(f"[4단계] 지정가 매수 테스트 ({symbol} {test_price:,}원 1주)")
        print("-" * 40)
        result = web.place_order(symbol, "buy", "limit", test_price, 1)
        print(f"  성공: {result['success']}")
        print(f"  메시지: {result['message']}")
        if result.get("order_id"):
            print(f"  주문번호: {result['order_id']}")

            # 5. 방금 넣은 주문 취소
            import time
            time.sleep(0.5)
            print()
            print(f"[5단계] 방금 주문 취소 ({result['order_id']})")
            print("-" * 40)
            cancel_result = web.cancel_order(result["order_id"], symbol, 1)
            print(f"  성공: {cancel_result['success']}")
            print(f"  메시지: {cancel_result['message']}")

    print()
    print("=" * 60)
    print("테스트 완료")
    print("=" * 60)
