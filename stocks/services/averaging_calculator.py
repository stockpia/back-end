"""
물타기 평단가 계산기
Averaging Down Calculator for Stock Investments

Web_03 물타기 계산기 전용 모듈
web_service_specification.md 기준 구현
"""

from typing import Dict


class AveragingCalculator:
    """물타기 평단가 계산 클래스"""

    def calculate(
        self,
        avg_price: float,      # 현재 평단가
        quantity: int,         # 보유 수량
        current_price: float,  # 현재가 (추가 매수 가격)
        add_quantity: int      # 추가 매수량
    ) -> Dict:
        """
        물타기 시뮬레이션 - 추가 매수 후 새로운 평단가 계산

        Args:
            avg_price: 현재 보유 주식의 평균 매수가
            quantity: 현재 보유 수량
            current_price: 현재 주가 (추가 매수할 가격)
            add_quantity: 추가로 매수할 수량

        Returns:
            Dict containing:
                - new_avg: 새로운 평단가
                - change: 평단가 변화 금액
                - change_pct: 평단가 변화율 (%)
                - total_qty: 총 보유 수량
                - total_cost: 총 투자 금액
                - breakeven_price: 손익분기점 (= new_avg)
                - profit_if_sell_now: 현재가에 전량 매도 시 손익
        """

        # 기존 투자금
        existing_cost = avg_price * quantity

        # 추가 투자금
        additional_cost = current_price * add_quantity

        # 총 투자금
        total_cost = existing_cost + additional_cost

        # 총 보유 수량
        total_qty = quantity + add_quantity

        # 새로운 평단가
        new_avg = total_cost / total_qty if total_qty > 0 else 0

        # 평단가 변화
        change = new_avg - avg_price
        change_pct = (change / avg_price * 100) if avg_price > 0 else 0

        # 현재가에 전량 매도 시 손익
        current_value = current_price * total_qty
        profit_if_sell_now = current_value - total_cost
        profit_pct = (profit_if_sell_now / total_cost * 100) if total_cost > 0 else 0

        return {
            'new_avg': int(new_avg),
            'change': int(change),
            'change_pct': round(change_pct, 2),
            'total_qty': total_qty,
            'total_cost': int(total_cost),
            'breakeven_price': int(new_avg),
            'profit_if_sell_now': int(profit_if_sell_now),
            'profit_pct': round(profit_pct, 2)
        }
