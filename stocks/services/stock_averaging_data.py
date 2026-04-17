"""
Web_03 물타기 계산기 데이터 제공 모듈
Stock Averaging Calculator Data Provider for Web_03

Web 백엔드에서 호출하는 물타기 계산 API
- 보유 종목 정보 조회
- 수량/금액 기준 물타기 계산
- 계산 결과 저장 및 히스토리 관리
"""

from __future__ import annotations

import os
import json
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path

from .HantuStock import HantuStock
from .averaging_calculator import AveragingCalculator


class StockAveragingDataProvider:
    """물타기 계산기 데이터 제공 클래스"""

    def __init__(self):
        """초기화"""
        # 한국투자증권 API (lazy initialization)
        self._hantu = None

        # 물타기 계산기 초기화
        self.calculator = AveragingCalculator()

        # 히스토리 저장 경로
        self.history_base_path = Path("./averaging_history")
        self.history_base_path.mkdir(exist_ok=True)

    @property
    def hantu(self):
        """HantuStock 인스턴스 (lazy initialization)"""
        if self._hantu is None:
            self._hantu = HantuStock()
        return self._hantu

    def get_holding_info(self, symbol: str) -> Dict:
        """
        보유 종목 정보 조회

        Args:
            symbol: 종목 코드 (예: "005930")

        Returns:
            Dict containing:
                - symbol: 종목 코드
                - company_name: 종목명
                - is_holding: 보유 여부
                - holding_info: 보유 정보 (보유 시)
                    - quantity: 보유 수량
                    - avg_price: 평균 매수가
                    - current_price: 현재가
                    - total_cost: 총 투자금
                    - current_value: 현재 평가액
                    - profit_loss: 평가 손익
                    - profit_loss_pct: 수익률 (%)
                    - fetched_at: 조회 시각
                - message: 안내 메시지 (미보유 시)
        """
        try:
            # 보유 종목 조회
            holdings = self.hantu.get_holdings()

            # 해당 종목 찾기
            holding = None
            for item in holdings:
                if item.get('symbol') == symbol:
                    holding = item
                    break

            # 종목명 조회 (간단하게 처리)
            company_name = holding.get('name', '') if holding else self._get_company_name(symbol)

            if not holding:
                return {
                    "symbol": symbol,
                    "company_name": company_name,
                    "is_holding": False,
                    "message": f"현재 {company_name}을(를) 보유하고 있지 않습니다"
                }

            # 보유 정보 구성
            quantity = int(holding.get('quantity', 0))
            avg_price = float(holding.get('avg_price', 0))
            current_price = float(holding.get('current_price', 0))

            total_cost = avg_price * quantity
            current_value = current_price * quantity
            profit_loss = current_value - total_cost
            profit_loss_pct = (profit_loss / total_cost * 100) if total_cost > 0 else 0

            return {
                "symbol": symbol,
                "company_name": company_name,
                "is_holding": True,
                "holding_info": {
                    "quantity": quantity,
                    "avg_price": int(avg_price),
                    "current_price": int(current_price),
                    "total_cost": int(total_cost),
                    "current_value": int(current_value),
                    "profit_loss": int(profit_loss),
                    "profit_loss_pct": round(profit_loss_pct, 2),
                    "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            }

        except Exception as e:
            return {
                "error": "API_ERROR",
                "message": f"보유 종목 조회 중 오류가 발생했습니다: {str(e)}",
                "symbol": symbol
            }

    def calculate_by_quantity(
        self,
        symbol: str,
        additional_price: float,
        additional_quantity: int
    ) -> Dict:
        """
        수량 기준 물타기 계산

        Args:
            symbol: 종목 코드
            additional_price: 추가 매수 단가
            additional_quantity: 추가 매수 수량

        Returns:
            Dict containing:
                - symbol: 종목 코드
                - company_name: 종목명
                - calculation_mode: "quantity"
                - input: 입력 정보
                - result: 계산 결과
                - fetched_at: 계산 시각
        """
        # 보유 정보 조회
        holding_info = self.get_holding_info(symbol)

        if not holding_info.get("is_holding"):
            return {
                "error": "NOT_HOLDING",
                "message": holding_info.get("message", "보유 종목이 아닙니다"),
                "symbol": symbol,
                "company_name": holding_info.get("company_name", "")
            }

        # 계산
        holding = holding_info["holding_info"]
        result = self.calculator.calculate(
            avg_price=holding["avg_price"],
            quantity=holding["quantity"],
            current_price=additional_price,
            add_quantity=additional_quantity
        )

        return {
            "symbol": symbol,
            "company_name": holding_info["company_name"],
            "calculation_mode": "quantity",
            "input": {
                "current_avg_price": holding["avg_price"],
                "current_quantity": holding["quantity"],
                "current_price": holding["current_price"],
                "additional_price": int(additional_price),
                "additional_quantity": additional_quantity
            },
            "result": {
                "new_avg_price": result["new_avg"],
                "avg_price_change": result["change"],
                "avg_price_change_pct": result["change_pct"],
                "total_quantity": result["total_qty"],
                "total_cost": result["total_cost"],
                "additional_cost": int(additional_price * additional_quantity),
                "breakeven_price": result["breakeven_price"],
                "profit_if_sell_now": result["profit_if_sell_now"],
                "profit_pct": result["profit_pct"]
            },
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    def calculate_by_amount(
        self,
        symbol: str,
        investment_amount: float,
        purchase_price: float
    ) -> Dict:
        """
        금액 기준 물타기 계산

        Args:
            symbol: 종목 코드
            investment_amount: 투자 금액
            purchase_price: 매수 단가

        Returns:
            Dict containing:
                - symbol: 종목 코드
                - company_name: 종목명
                - calculation_mode: "amount"
                - input: 입력 정보 (calculated_quantity 포함)
                - result: 계산 결과
                - fetched_at: 계산 시각
        """
        # 보유 정보 조회
        holding_info = self.get_holding_info(symbol)

        if not holding_info.get("is_holding"):
            return {
                "error": "NOT_HOLDING",
                "message": holding_info.get("message", "보유 종목이 아닙니다"),
                "symbol": symbol,
                "company_name": holding_info.get("company_name", "")
            }

        # 수량 계산 (소수점 버림)
        calculated_quantity = int(investment_amount / purchase_price)
        actual_investment = purchase_price * calculated_quantity

        # 계산
        holding = holding_info["holding_info"]
        result = self.calculator.calculate(
            avg_price=holding["avg_price"],
            quantity=holding["quantity"],
            current_price=purchase_price,
            add_quantity=calculated_quantity
        )

        return {
            "symbol": symbol,
            "company_name": holding_info["company_name"],
            "calculation_mode": "amount",
            "input": {
                "current_avg_price": holding["avg_price"],
                "current_quantity": holding["quantity"],
                "current_price": holding["current_price"],
                "investment_amount": int(investment_amount),
                "purchase_price": int(purchase_price),
                "calculated_quantity": calculated_quantity
            },
            "result": {
                "new_avg_price": result["new_avg"],
                "avg_price_change": result["change"],
                "avg_price_change_pct": result["change_pct"],
                "total_quantity": result["total_qty"],
                "total_cost": result["total_cost"],
                "additional_cost": int(actual_investment),
                "breakeven_price": result["breakeven_price"],
                "profit_if_sell_now": result["profit_if_sell_now"],
                "profit_pct": result["profit_pct"]
            },
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    def save_calculation(
        self,
        symbol: str,
        calculation_result: Dict,
        input_mode: str = "quantity"
    ) -> Dict:
        """
        계산 결과 저장

        Args:
            symbol: 종목 코드
            calculation_result: calculate_by_* 메서드의 반환값
            input_mode: "quantity" 또는 "amount"

        Returns:
            Dict containing:
                - calculation_id: 계산 ID
                - symbol: 종목 코드
                - saved_at: 저장 시각
                - message: 저장 완료 메시지
        """
        try:
            # 종목별 디렉토리 생성
            symbol_dir = self.history_base_path / symbol
            symbol_dir.mkdir(exist_ok=True)

            # 계산 ID 생성
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            calculation_id = f"calc_{timestamp}_{symbol}"

            # 저장할 데이터 구성
            save_data = {
                "calculation_id": calculation_id,
                "symbol": symbol,
                "company_name": calculation_result.get("company_name", ""),
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "calculation_mode": input_mode,
                "snapshot": {
                    "current_avg_price": calculation_result["input"]["current_avg_price"],
                    "current_quantity": calculation_result["input"]["current_quantity"],
                    "current_price": calculation_result["input"]["current_price"]
                },
                "input": {},
                "result": calculation_result["result"]
            }

            # 입력 모드별 입력값 저장
            if input_mode == "quantity":
                save_data["input"] = {
                    "additional_price": calculation_result["input"]["additional_price"],
                    "additional_quantity": calculation_result["input"]["additional_quantity"]
                }
            else:  # amount
                save_data["input"] = {
                    "investment_amount": calculation_result["input"]["investment_amount"],
                    "purchase_price": calculation_result["input"]["purchase_price"],
                    "calculated_quantity": calculation_result["input"]["calculated_quantity"]
                }

            # 파일 저장
            file_path = symbol_dir / f"{calculation_id}.json"
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)

            return {
                "calculation_id": calculation_id,
                "symbol": symbol,
                "saved_at": save_data["saved_at"],
                "message": "계산 결과가 저장되었습니다"
            }

        except Exception as e:
            return {
                "error": "SAVE_FAILED",
                "message": f"저장 중 오류가 발생했습니다: {str(e)}",
                "symbol": symbol
            }

    def get_calculation_history(
        self,
        symbol: str,
        limit: int = 10
    ) -> Dict:
        """
        계산 히스토리 조회

        Args:
            symbol: 종목 코드
            limit: 조회 개수 (기본 10개)

        Returns:
            Dict containing:
                - symbol: 종목 코드
                - company_name: 종목명
                - total_count: 전체 개수
                - calculations: 계산 히스토리 리스트
        """
        try:
            symbol_dir = self.history_base_path / symbol

            if not symbol_dir.exists():
                return {
                    "symbol": symbol,
                    "company_name": self._get_company_name(symbol),
                    "total_count": 0,
                    "calculations": []
                }

            # JSON 파일 목록 조회 (최신순)
            json_files = sorted(
                symbol_dir.glob("calc_*.json"),
                key=lambda x: x.stat().st_mtime,
                reverse=True
            )

            # 제한 개수만큼 로드
            calculations = []
            for file_path in json_files[:limit]:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)

                    # 요약 정보만 추출
                    calculations.append({
                        "calculation_id": data["calculation_id"],
                        "saved_at": data["saved_at"],
                        "calculation_mode": data["calculation_mode"],
                        "input": data["input"],
                        "result_summary": {
                            "new_avg_price": data["result"]["new_avg_price"],
                            "total_quantity": data["result"]["total_quantity"],
                            "total_cost": data["result"]["total_cost"]
                        }
                    })
                except Exception:
                    continue

            return {
                "symbol": symbol,
                "company_name": self._get_company_name(symbol),
                "total_count": len(calculations),
                "calculations": calculations
            }

        except Exception as e:
            return {
                "error": "HISTORY_ERROR",
                "message": f"히스토리 조회 중 오류가 발생했습니다: {str(e)}",
                "symbol": symbol,
                "calculations": []
            }

    def delete_calculation(self, calculation_id: str) -> Dict:
        """
        계산 결과 삭제

        Args:
            calculation_id: 삭제할 계산 ID

        Returns:
            Dict containing:
                - success: 성공 여부
                - message: 결과 메시지
        """
        try:
            # calculation_id에서 종목 코드 추출
            # 형식: calc_20260128_103000_005930
            symbol = calculation_id.split('_')[-1]

            symbol_dir = self.history_base_path / symbol
            file_path = symbol_dir / f"{calculation_id}.json"

            if not file_path.exists():
                return {
                    "success": False,
                    "message": "삭제할 계산 결과를 찾을 수 없습니다"
                }

            file_path.unlink()

            return {
                "success": True,
                "message": "계산 결과가 삭제되었습니다"
            }

        except Exception as e:
            return {
                "success": False,
                "message": f"삭제 중 오류가 발생했습니다: {str(e)}"
            }

    def _get_company_name(self, symbol: str) -> str:
        """
        종목명 조회 (HantuStock API 활용)

        Args:
            symbol: 종목 코드

        Returns:
            종목명
        """
        try:
            price_info = self.hantu.get_stock_price(symbol)
            if "error" in price_info:
                return symbol
            return price_info.get("name", symbol)
        except Exception as e:
            print(f"종목명 조회 실패 ({symbol}): {e}")
            return symbol
