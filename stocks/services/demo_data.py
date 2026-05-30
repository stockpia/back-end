"""
시연용 demo 데이터 생성기.

WebDetailReport 는 실 KIS 거래내역에 의존하는데, 모의계좌엔 거래기록이
없는 경우가 많아 빈 응답이 옴. demo 모드 (`?demo=1`) 일 때만 사용자의 실제 보유
종목 + 평가손익 (pl) 기준으로 그럴듯한 가상 매수/매도 transactions 를 만들어
풀 리포트 시연 가능.

원칙:
- buy_price 는 실 holding 의 evlu_pfls_amt (pl) / qty 로 역산 → 매도 시 realized
  손익이 사용자가 계좌에서 보던 평가손익과 일치 (수익률도 자연스럽게 맞음)
- 매수 날짜를 period (1m/3m/1y) 안에 잘 분포시켜 period 별 결과가 달라지도록
- 일부 종목은 추가 매수 (물타기 패턴) + 1건 부분 매도 (수익 실현) 시연용

운영 코드 영향 없음 — endpoint 의 ?demo=1 query 일 때만 호출.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

# period → (정수 일수, 매수 date 분산용 step)
_PERIOD_DAYS = {"1m": 30, "3m": 90, "1y": 365}


def _safe_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_demo_transactions(
    holdings: List[Dict],
    *,
    period: str = "1m",
    now: Optional[datetime] = None,
) -> List[Dict]:
    """
    실 보유 종목 + 평가손익 기반 가상 거래내역 생성.

    Args:
        holdings: HantuStock.get_holding_stock_detail() 결과
            기대 필드: pdno / prdt_name / hldg_qty / prpr / evlu_pfls_amt
        period: '1m' | '3m' | '1y' — 매수 날짜 분포 범위 결정
        now: 기준 시각 (테스트용)

    Returns:
        get_transaction_history 와 동일한 dict 스키마 list. 빈 holdings → [].

    설계:
      - buy_price = current_price - (pl / qty) → 매도 완료 시 realized = pl (실 계좌 평가손익과 일치)
      - 매수 날짜는 period 안에 균등 분산 (1m 이면 25~5일 전, 1y 이면 350~30일 전)
      - NAVER / 카카오는 8일 전 추가매수 → 물타기 패턴 시연
      - 첫 종목 일부 매도 (3일 전, 매수가의 +5% 가격) → 수익 실현 시연
    """
    if not holdings:
        return []

    today = now or datetime.now()
    period_days = _PERIOD_DAYS.get(period, 30)

    # 매수 날짜 분산: oldest=period_days-5, newest=5 사이 균등 분포 (지나치게 옛날/오늘 방지)
    n = max(1, len(holdings))
    oldest = max(5, period_days - 5)
    newest = 5
    step = (oldest - newest) / max(1, n - 1) if n > 1 else 0

    fake_tx: List[Dict] = []

    for i, h in enumerate(holdings):
        ticker = str(h.get("pdno") or "")
        name = str(h.get("prdt_name") or ticker)
        qty = _safe_int(h.get("hldg_qty"))
        cur = _safe_float(h.get("prpr"))
        pl = _safe_float(h.get("evlu_pfls_amt"))
        if not ticker or qty <= 0 or cur <= 0:
            continue

        # buy_price 역산 — 매도 시 realized = pl 가 되도록
        per_share_profit = pl / qty if qty > 0 else 0
        buy_price = int(round(max(1, cur - per_share_profit)))
        days_ago = int(round(oldest - step * i))
        buy_date = (today - timedelta(days=days_ago)).strftime("%Y%m%d")
        fake_tx.append({
            "ord_dt": buy_date,
            "pdno": ticker, "prdt_name": name,
            "sll_buy_dvsn_cd": "02", "sll_buy_dvsn_cd_name": "매수",
            "ord_qty": qty, "tot_ccld_qty": qty,
            "avg_prvs": buy_price, "tot_ccld_amt": buy_price * qty,
        })

        # NAVER/카카오는 추가매수 — 물타기 패턴
        if ticker in ("035720", "035420"):
            extra_qty = max(1, qty // 2)
            extra_price = max(1, int(round(buy_price * 0.93)))  # 7% 더 낮게 추가매수
            extra_days = max(2, min(days_ago - 3, period_days - 3))
            fake_tx.append({
                "ord_dt": (today - timedelta(days=extra_days)).strftime("%Y%m%d"),
                "pdno": ticker, "prdt_name": name,
                "sll_buy_dvsn_cd": "02", "sll_buy_dvsn_cd_name": "매수",
                "ord_qty": extra_qty, "tot_ccld_qty": extra_qty,
                "avg_prvs": extra_price, "tot_ccld_amt": extra_price * extra_qty,
            })

    # 첫 종목 부분 매도 — 수익실현 시연용
    if fake_tx:
        first = fake_tx[0]
        sell_qty = max(1, first["tot_ccld_qty"] // 3)
        # 매수가의 +5% 가격에 매도 (안정적 수익) — realized 가 양수가 되도록
        sell_price = max(1, int(round(first["avg_prvs"] * 1.05)))
        sell_days_ago = max(1, min(3, period_days // 2))
        fake_tx.append({
            "ord_dt": (today - timedelta(days=sell_days_ago)).strftime("%Y%m%d"),
            "pdno": first["pdno"], "prdt_name": first["prdt_name"],
            "sll_buy_dvsn_cd": "01", "sll_buy_dvsn_cd_name": "매도",
            "ord_qty": sell_qty, "tot_ccld_qty": sell_qty,
            "avg_prvs": sell_price, "tot_ccld_amt": sell_price * sell_qty,
        })

    return fake_tx
