"""
시연용 demo 데이터 생성기.

WebDetailReport 는 실 KIS 거래내역에 의존하는데, 모의계좌엔 거래기록이
없는 경우가 많아 빈 응답이 옴. 이 모듈은 사용자의 실제 보유 종목 + 평가손익
기준으로 그럴듯한 가상 매수/매도 transactions 를 만들어 풀 리포트 시연 가능.

설계 원칙:
- **deterministic** — 종목코드 + period 기반 seed 로 random.Random 사용 →
  같은 사용자는 매번 같은 거래 패턴을 봄 (UX 일관성)
- **장기적·풍부한 기록** — period 별 다른 거래 횟수 (1m: 3-5건/종목,
  3m: 5-9건, 1y: 8-14건). 시간상 잘 분산.
- **현실적 수치** — 가격/수량에 노이즈를 더해 깔끔하게 떨어지지 않도록
  (예: +5% 매도가 → +4.7% / +6.3% 같이 다양).
- **평가손익 일치** — 평균 매수가가 실 보유 buy_price (cur - pl/qty) 와
  일치하도록 가격 정규화 → 사용자가 계좌에서 본 평가손익과 모순 없음
- **다양한 패턴** — 단순 매수만이 아니라 부분 매도 / 추가 매수 (물타기) /
  손실 매도 등 섞임

운영 코드 영향 없음 — endpoint 의 auto fallback (실 거래 0건) 일 때만 호출.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# period → 기간 일수 + (종목당 매수 횟수 min, max)
_PERIOD_CONFIG = {
    "1m": {"days": 30, "buys_range": (3, 5), "sells_max": 2},
    "3m": {"days": 90, "buys_range": (5, 9), "sells_max": 3},
    "1y": {"days": 365, "buys_range": (8, 14), "sells_max": 5},
}


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


def _seeded_rng(ticker: str, period: str) -> random.Random:
    """종목+period deterministic seed → 매번 같은 패턴 (UX 일관성)."""
    h = hashlib.sha256(f"{ticker}|{period}".encode("utf-8")).hexdigest()
    return random.Random(int(h[:12], 16))


def _distribute_qty(total: int, n: int, rng: random.Random) -> List[int]:
    """total 수량을 n개로 분배 (±30% 변동, 합 = total 보장)."""
    if n <= 0 or total <= 0:
        return []
    if n == 1:
        return [total]
    base = total / n
    raw = [max(0.4, rng.uniform(0.7, 1.3)) * base for _ in range(n)]
    s = sum(raw)
    # 정수로 떨어뜨리되 마지막 항으로 합 보정
    out = [max(1, int(round(r * total / s))) for r in raw]
    diff = total - sum(out)
    out[-1] = max(1, out[-1] + diff)
    return out


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
        period: '1m' | '3m' | '1y' — 매수 날짜 분포 범위 + 거래 횟수 결정
        now: 기준 시각 (테스트용)

    Returns:
        get_transaction_history 와 동일한 dict 스키마 list. 빈 holdings → [].
    """
    if not holdings:
        return []

    today = now or datetime.now()
    cfg = _PERIOD_CONFIG.get(period, _PERIOD_CONFIG["1m"])
    period_days = cfg["days"]
    buys_min, buys_max = cfg["buys_range"]
    sells_max = cfg["sells_max"]

    fake_tx: List[Dict] = []

    for h in holdings:
        ticker = str(h.get("pdno") or "")
        name = str(h.get("prdt_name") or ticker)
        qty = _safe_int(h.get("hldg_qty"))
        cur = _safe_float(h.get("prpr"))
        pl = _safe_float(h.get("evlu_pfls_amt"))
        if not ticker or qty <= 0 or cur <= 0:
            continue

        rng = _seeded_rng(ticker, period)

        # 평균 매수가 — 매도 완료 시 realized = pl 가 되도록 역산
        per_share_profit = pl / qty if qty > 0 else 0
        avg_buy = max(1, cur - per_share_profit)

        # ─── 매수 시퀀스 (3-14건 / 종목, period 따라) ───
        n_buys = rng.randint(buys_min, buys_max)

        # 추가로 매도할 수량 — 매수 총량 = qty + sell_total
        sell_total = (
            rng.randint(0, qty // 2)
            if rng.random() < 0.7  # 70% 종목은 매도 경험 있음
            else 0
        )
        total_buy_qty = qty + sell_total
        buy_qtys = _distribute_qty(total_buy_qty, n_buys, rng)

        # 가격 노이즈 ±3% — 깔끔하게 떨어지지 않도록
        # 정규화로 평균 매수가가 avg_buy 와 일치
        raw_prices = [avg_buy * rng.uniform(0.97, 1.03) for _ in range(n_buys)]
        # 가중평균 정규화 (수량 가중)
        weighted_sum = sum(p * q for p, q in zip(raw_prices, buy_qtys))
        weighted_avg = weighted_sum / total_buy_qty
        norm = avg_buy / weighted_avg if weighted_avg > 0 else 1.0
        buy_prices = [max(1, int(round(p * norm))) for p in raw_prices]

        # 매수 날짜 — period 안에 잘 분산. n_buys 개를 정렬 (옛 → 최신)
        # 최옛 = period_days * 0.95, 최최신 = 3일 전 정도
        oldest = max(5, int(period_days * 0.9))
        newest = 3
        if n_buys == 1:
            buy_days = [int((oldest + newest) / 2)]
        else:
            # 균등 간격 + ±25% jitter
            base_gap = (oldest - newest) / (n_buys - 1)
            buy_days_raw = [
                oldest - base_gap * i + rng.uniform(-base_gap * 0.25, base_gap * 0.25)
                for i in range(n_buys)
            ]
            buy_days = sorted({max(2, int(round(d))) for d in buy_days_raw}, reverse=True)
            # 중복 제거로 줄어들 수 있어 buy_prices/qtys 도 길이 맞춤
            if len(buy_days) < n_buys:
                n_buys = len(buy_days)
                buy_prices = buy_prices[:n_buys]
                buy_qtys = buy_qtys[:n_buys]

        # ─── 매도 시퀀스 (0~5건, sell_total 분배) ───
        sell_records = []
        if sell_total > 0:
            n_sells = rng.randint(1, max(1, min(sells_max, sell_total)))
            sell_qtys = _distribute_qty(sell_total, n_sells, rng)
            # 가격 — avg_buy 의 -5% ~ +12% 사이 (이익/손실 섞임)
            # 종목별로 한쪽 편향 (수익률 양수면 매도가 평균 > avg_buy)
            base_return = (pl / (avg_buy * qty)) if avg_buy > 0 and qty > 0 else 0
            for sq in sell_qtys:
                # 변동률 = base_return ± noise
                ret = base_return + rng.uniform(-0.04, 0.06)
                sell_price = max(1, int(round(avg_buy * (1 + ret))))
                # 매도 날짜는 매수 중간 ~ 최근 사이
                sell_days_ago = rng.randint(2, max(3, int(period_days * 0.7)))
                sell_records.append((sell_days_ago, sq, sell_price))

        # ─── transactions append (매수 + 매도 시간순 섞임) ───
        events: List[tuple] = []
        for d_ago, q_, p_ in zip(buy_days, buy_qtys, buy_prices):
            events.append((d_ago, "buy", q_, p_))
        for d_ago, q_, p_ in sell_records:
            events.append((d_ago, "sell", q_, p_))
        events.sort(key=lambda e: -e[0])  # 옛것부터

        for d_ago, kind, q_, p_ in events:
            date_str = (today - timedelta(days=d_ago)).strftime("%Y%m%d")
            if kind == "buy":
                fake_tx.append({
                    "ord_dt": date_str,
                    "pdno": ticker, "prdt_name": name,
                    "sll_buy_dvsn_cd": "02", "sll_buy_dvsn_cd_name": "매수",
                    "ord_qty": q_, "tot_ccld_qty": q_,
                    "avg_prvs": p_, "tot_ccld_amt": p_ * q_,
                })
            else:
                fake_tx.append({
                    "ord_dt": date_str,
                    "pdno": ticker, "prdt_name": name,
                    "sll_buy_dvsn_cd": "01", "sll_buy_dvsn_cd_name": "매도",
                    "ord_qty": q_, "tot_ccld_qty": q_,
                    "avg_prvs": p_, "tot_ccld_amt": p_ * q_,
                })

    return fake_tx
