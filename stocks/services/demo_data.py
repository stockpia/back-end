"""
시연용 demo 데이터 생성기.

WebDetailReport 의 리포트는 실 KIS 거래내역에 의존하는데, 모의계좌엔 거래기록이
없는 경우가 많아 빈 응답이 나옴. demo 모드 (`?demo=1`) 일 때만 사용자의 실제 보유
종목 기준으로 그럴듯한 가상 매수/매도 transactions 를 만들어 풀 리포트 시연 가능.

운영 코드 영향 없음 — endpoint 의 ?demo=1 query 일 때만 호출.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional


def build_demo_transactions(
    holdings: List[Dict],
    *,
    now: Optional[datetime] = None,
) -> List[Dict]:
    """
    보유 종목 리스트 → 가상 거래내역 리스트.

    Args:
        holdings: HantuStock.get_holding_stock_detail() 결과 (pdno/prdt_name/
            hldg_qty/prpr 키 사용)
        now: 기준 시각 (테스트용). 기본은 datetime.now().

    Returns:
        get_transaction_history 와 동일한 dict 스키마 (ord_dt/pdno/prdt_name/
        sll_buy_dvsn_cd/sll_buy_dvsn_cd_name/ord_qty/tot_ccld_qty/avg_prvs/
        tot_ccld_amt) 의 list.

    Returns 빈 list 가능 (holdings 가 비었거나 모두 invalid 인 경우).
    """
    if not holdings:
        return []

    today = now or datetime.now()
    fake_tx: List[Dict] = []

    for i, h in enumerate(holdings):
        ticker = h.get("pdno", "")
        name = h.get("prdt_name", "")
        qty = int(h.get("hldg_qty", 0) or 0)
        cur = float(h.get("prpr", 0) or 0)
        if not ticker or qty <= 0 or cur <= 0:
            continue

        # 가상 매수: 현재가의 92% (8% 평가익), 20+i*4 일 전
        buy_price = max(1, int(cur * 0.92))
        buy_date = (today - timedelta(days=20 + i * 4)).strftime("%Y%m%d")
        fake_tx.append({
            "ord_dt": buy_date,
            "pdno": ticker, "prdt_name": name,
            "sll_buy_dvsn_cd": "02", "sll_buy_dvsn_cd_name": "매수",
            "ord_qty": qty, "tot_ccld_qty": qty,
            "avg_prvs": buy_price, "tot_ccld_amt": buy_price * qty,
        })

        # 일부 종목은 8일 전 추가매수 — 물타기 패턴 시연용
        if ticker in ("035720", "035420"):
            extra_qty = max(1, qty // 2)
            extra_price = max(1, int(cur * 0.86))
            fake_tx.append({
                "ord_dt": (today - timedelta(days=8)).strftime("%Y%m%d"),
                "pdno": ticker, "prdt_name": name,
                "sll_buy_dvsn_cd": "02", "sll_buy_dvsn_cd_name": "매수",
                "ord_qty": extra_qty, "tot_ccld_qty": extra_qty,
                "avg_prvs": extra_price, "tot_ccld_amt": extra_price * extra_qty,
            })

    # 첫 종목 일부 매도 (수익 실현 시연용) — realized_profit 메트릭 채움
    if fake_tx:
        first_buy = fake_tx[0]
        sell_qty = max(1, first_buy["tot_ccld_qty"] // 3)
        sell_price = max(1, int(first_buy["avg_prvs"] * 1.10))  # +10% 수익 매도
        fake_tx.append({
            "ord_dt": (today - timedelta(days=3)).strftime("%Y%m%d"),
            "pdno": first_buy["pdno"], "prdt_name": first_buy["prdt_name"],
            "sll_buy_dvsn_cd": "01", "sll_buy_dvsn_cd_name": "매도",
            "ord_qty": sell_qty, "tot_ccld_qty": sell_qty,
            "avg_prvs": sell_price, "tot_ccld_amt": sell_price * sell_qty,
        })

    return fake_tx
