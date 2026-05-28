"""
MATE Agent 가 자율 호출하는 도구 카탈로그 (12개).

설계 원칙 (MATE_Agentic_설계_가이드.md §4-2):
1. user_id 입력 → 내부에서 KisAccount 조회 (사용자가 봇에 자기 키 입력 X)
2. 예외 raise 금지 → {"error": "..."} 구조화 응답
3. idempotent — 같은 입력 같은 출력 (캐시 가능)
4. docstring 한국어 + 상세히 — LLM 이 함수 시그니처와 docstring 만 보고 선택

각 도구는 LangChain @tool 데코레이터로 래핑되며 stocks/agents/tools_registry.py
의 ALL_TOOLS 리스트로 export → LangGraph 가 바인딩.

⚠️ v1 한계 (PR 사이즈 절약):
- KIS 의존 도구 (holdings/place_order/...) 는 시스템 단일 계좌 사용.
  per-user KisAccount → HantuStock(app_key, secret) 분기는 _resolve_user_account()
  훅에 TODO 마크. ReplyAgent 도입 (W2) 시 실제 사용자 키 라우팅 필요.
- get_user_favorites: Favorite 모델 없음 → 빈 리스트 stub.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from django.utils import timezone
from langchain_core.tools import tool

from ..models import KisAccount, NotificationLog, TelegramLink, User

logger = logging.getLogger(__name__)


# ─── 사용자 컨텍스트 헬퍼 ──────────────────────────────────

def _get_user(user_id: str) -> Optional[User]:
    try:
        return User.objects.filter(user_id=user_id).first()
    except Exception as e:
        logger.error("[TOOLS] _get_user(%s) failed: %s", user_id, e)
        return None


def _resolve_user_account(user_id: str) -> Dict[str, Any]:
    """
    user_id → KIS 자격증명 lookup.

    TODO(W2): 현재 KisAccount 가 있어도 시스템 단일 계좌 (env) 로 fallback.
    HantuStock 이 app_key/secret 을 인자로 받도록 리팩터한 뒤 per-user 분기.
    """
    user = _get_user(user_id)
    if not user:
        return {"ok": False, "error": f"user {user_id} not found"}
    has_kis = KisAccount.objects.filter(user=user).exists()
    return {"ok": True, "user": user, "has_kis": has_kis}


def _ok_or_error(payload: Dict[str, Any]) -> Dict[str, Any]:
    """기존 서비스 모듈이 {'error': ...} 컨벤션을 그대로 통과시키는 헬퍼."""
    return payload


# ─── 1. get_user_holdings ────────────────────────────────

@tool
def get_user_holdings(user_id: str) -> Dict[str, Any]:
    """
    사용자의 보유 종목 리스트를 조회합니다.

    각 종목별 평가금액 / 매입가 / 현재가 / 손익까지 한 번에 가져옵니다.

    Args:
        user_id: 사용자 ID (UUID 또는 식별자)

    Returns:
        {
            "holdings": [
                {
                    "ticker": "005930",
                    "name": "삼성전자",
                    "quantity": int,
                    "avg_price": float,
                    "current_price": float,
                    "eval_amount": float,
                    "profit_amount": float,
                    "profit_rate": float
                },
                ...
            ],
            "count": int
        }
        또는 {"error": "..."}

    사용자가 "내 보유 종목", "포트폴리오", "내 주식" 등을 물을 때 호출하세요.
    """
    ctx = _resolve_user_account(user_id)
    if not ctx.get("ok"):
        return {"error": ctx.get("error", "unknown user")}
    if not ctx.get("has_kis"):
        return {"error": "KIS 계좌가 연동되지 않았습니다"}

    try:
        from ..services.stock_list_data import StockListDataProvider
        provider = StockListDataProvider()
        result = provider.get_holding_stocks(sort_by="eval_amount", order="desc")
    except Exception as e:
        logger.error("[TOOLS] get_user_holdings failed: %s", e)
        return {"error": str(e)}

    if "error" in result:
        return _ok_or_error(result)
    return {"holdings": result.get("stocks", []), "count": result.get("count", 0)}


# ─── 2. get_user_favorites ───────────────────────────────

@tool
def get_user_favorites(user_id: str) -> Dict[str, Any]:
    """
    사용자가 즐겨찾기 한 관심 종목 리스트를 조회합니다.

    Args:
        user_id: 사용자 ID

    Returns:
        {"favorites": ["005930", "035720", ...], "count": int}
        또는 {"error": "..."}

    사용자가 "관심 종목", "즐겨찾기", "favorites" 등을 물을 때 호출하세요.
    """
    # TODO(W2): Favorite 모델이 아직 없어 빈 리스트 stub. 모델 도입 후 실제 쿼리로 교체.
    ctx = _resolve_user_account(user_id)
    if not ctx.get("ok"):
        return {"error": ctx.get("error", "unknown user")}
    return {"favorites": [], "count": 0, "note": "Favorite 모델 미구현 (W2 예정)"}


# ─── 3. get_current_price ────────────────────────────────

@tool
def get_current_price(symbol: str) -> Dict[str, Any]:
    """
    종목의 현재가 + 등락률 + 거래량을 조회합니다.

    Args:
        symbol: 6자리 종목코드 (예: "005930") 또는 한글 종목명 (예: "삼성전자")

    Returns:
        {
            "ticker": "005930",
            "company_name": "삼성전자",
            "current_price": float,
            "price_change": float,
            "change_rate": float,
            "volume": int
        }
        또는 {"error": "..."}

    "지금 얼마야?", "현재가", "시세" 등 실시간 가격 질문에 호출하세요.
    """
    try:
        from ..services.stock_chart_data import StockChartDataProvider
        provider = StockChartDataProvider()
        info = provider.get_stock_info(symbol)
        return _ok_or_error(info)
    except Exception as e:
        logger.error("[TOOLS] get_current_price(%s) failed: %s", symbol, e)
        return {"error": str(e)}


# ─── 4. get_stock_news ───────────────────────────────────

@tool
def get_stock_news(symbol: str, days: int = 7) -> Dict[str, Any]:
    """
    종목 관련 최근 뉴스를 검색합니다.

    Args:
        symbol: 6자리 종목코드 또는 회사명
        days: 며칠 전까지 검색할지 (기본 7일). 현재 Tavily API 한계상 7일 단위.

    Returns:
        {
            "articles": [
                {"title": str, "url": str, "summary": str, "published_at": str},
                ...
            ],
            "count": int
        }
        또는 {"error": "..."}

    "뉴스 알려줘", "최근 이슈", "왜 떨어졌어?" 등 시장 반응이 필요할 때 호출하세요.
    """
    try:
        from ..services.stock_search import lookup_company_name
        from ..services.tavily_search import TavilySearchClient

        # symbol 이 코드면 회사명 lookup, 아니면 그대로
        if symbol.isdigit() or len(symbol) <= 8:
            company = lookup_company_name(symbol) if symbol.isdigit() else symbol
        else:
            company = symbol

        client = TavilySearchClient()
        result = client.search_stock_news(company, symbol, max_results=5)
        if "error" in result:
            return _ok_or_error(result)
        return {
            "articles": [
                {
                    "title": r["title"],
                    "url": r["url"],
                    "summary": r["content"],
                    "published_at": r.get("published_date", ""),
                }
                for r in result.get("results", [])
            ],
            "count": len(result.get("results", [])),
        }
    except Exception as e:
        logger.error("[TOOLS] get_stock_news(%s) failed: %s", symbol, e)
        return {"error": str(e)}


# ─── 5. get_financial_summary ────────────────────────────

@tool
def get_financial_summary(symbol: str) -> Dict[str, Any]:
    """
    종목의 재무 요약 (매출/영업이익/부채비율 등) 을 DART 공시에서 가져옵니다.

    Args:
        symbol: 6자리 종목코드 (예: "005930")

    Returns:
        {
            "revenue": float, "operating_income": float, "net_income": float,
            "debt_ratio": float, "total_liabilities": float, "total_equity": float,
            "operating_cf": float, "operating_margin": float,
            "report_year": int, "report_type": str, "report_label": str
        }
        또는 {"error": "..."}

    "재무는 어때?", "부채 많아?", "영업이익" 등 펀더멘털 질문에 호출하세요.
    """
    try:
        from ..services.dart_client import DartClient
        client = DartClient()
        result = client.get_financial_summary(symbol)
        return _ok_or_error(result)
    except Exception as e:
        logger.error("[TOOLS] get_financial_summary(%s) failed: %s", symbol, e)
        return {"error": str(e)}


# ─── 6. calculate_averaging ──────────────────────────────

@tool
def calculate_averaging(
    avg_price: float,
    quantity: int,
    current_price: float,
    add_quantity: int,
) -> Dict[str, Any]:
    """
    평단가 물타기 계산 — 추가 매수 시 새 평단가 / 손익분기점 / 예상 손익 산출.

    Args:
        avg_price: 현재 보유 종목의 평균 매입가 (원)
        quantity: 현재 보유 수량
        current_price: 현재가 (추가 매수 가격)
        add_quantity: 추가 매수 수량

    Returns:
        {
            "new_avg": int,           # 추가 매수 후 새 평단가
            "change": int,            # 평단 변동 (음수면 낮아짐)
            "change_pct": float,
            "total_qty": int,         # 추가 후 총 수량
            "total_cost": int,        # 총 매입원가
            "breakeven_price": int,   # 손익분기점
            "profit_if_sell_now": int,
            "profit_pct": float
        }

    "평단 낮추려면?", "물타기 효과", "추가 매수하면" 류 시뮬레이션에 호출하세요.
    """
    try:
        from ..services.averaging_calculator import AveragingCalculator
        calc = AveragingCalculator()
        return calc.calculate(
            avg_price=avg_price,
            quantity=quantity,
            current_price=current_price,
            add_quantity=add_quantity,
        )
    except Exception as e:
        logger.error("[TOOLS] calculate_averaging failed: %s", e)
        return {"error": str(e)}


# ─── 7. get_pending_orders ───────────────────────────────

@tool
def get_pending_orders(user_id: str) -> Dict[str, Any]:
    """
    사용자의 미체결 (대기 중인) 주문 리스트를 조회합니다.

    Args:
        user_id: 사용자 ID

    Returns:
        {
            "pending_orders": [
                {"order_id": str, "symbol": str, "company_name": str,
                 "side": "buy"|"sell", "price": int, "pending_quantity": int,
                 "ordered_at": str},
                ...
            ],
            "total_count": int
        }
        또는 {"error": "..."}

    "내 주문", "대기 주문", "체결됐어?" 류 질문에 호출하세요.
    """
    ctx = _resolve_user_account(user_id)
    if not ctx.get("ok"):
        return {"error": ctx.get("error", "unknown user")}
    if not ctx.get("has_kis"):
        return {"error": "KIS 계좌가 연동되지 않았습니다"}

    try:
        from ..services.web_order import WebOrder
        order = WebOrder()
        return _ok_or_error(order.get_pending_orders())
    except Exception as e:
        logger.error("[TOOLS] get_pending_orders failed: %s", e)
        return {"error": str(e)}


# ─── 8. place_order ──────────────────────────────────────

@tool
def place_order(
    user_id: str,
    symbol: str,
    side: str,
    quantity: int,
    price: int,
    order_type: str = "limit",
) -> Dict[str, Any]:
    """
    매수/매도 주문을 접수합니다.

    Args:
        user_id: 사용자 ID
        symbol: 6자리 종목코드
        side: "buy" (매수) 또는 "sell" (매도)
        quantity: 주문 수량
        price: 주문 가격 (시장가 주문이면 0)
        order_type: "limit" (지정가) 또는 "market" (시장가). 기본 limit.

    Returns:
        {"success": bool, "order_id": str|None, "message": str, ...}
        또는 {"error": "..."}

    ⚠️ 사용자가 명시적으로 매매를 요청했을 때만 호출. 분석 단계에서 절대 호출 X.
    """
    ctx = _resolve_user_account(user_id)
    if not ctx.get("ok"):
        return {"error": ctx.get("error", "unknown user")}
    if not ctx.get("has_kis"):
        return {"error": "KIS 계좌가 연동되지 않았습니다"}

    try:
        from ..services.web_order import WebOrder
        order = WebOrder()
        return _ok_or_error(order.place_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            price=price,
            quantity=quantity,
        ))
    except Exception as e:
        logger.error("[TOOLS] place_order failed: %s", e)
        return {"error": str(e)}


# ─── 9. cancel_order ─────────────────────────────────────

@tool
def cancel_order(user_id: str, order_id: str, symbol: str, quantity: int) -> Dict[str, Any]:
    """
    미체결 주문을 취소합니다.

    Args:
        user_id: 사용자 ID
        order_id: 취소할 주문의 ID (get_pending_orders 응답의 order_id)
        symbol: 종목코드
        quantity: 취소 수량 (분할 취소 가능)

    Returns:
        {"success": bool, "order_id": str, "message": str} 또는 {"error": "..."}

    사용자가 "주문 취소" 명시적으로 요청 시 호출.
    """
    ctx = _resolve_user_account(user_id)
    if not ctx.get("ok"):
        return {"error": ctx.get("error", "unknown user")}
    if not ctx.get("has_kis"):
        return {"error": "KIS 계좌가 연동되지 않았습니다"}

    try:
        from ..services.web_order import WebOrder
        order = WebOrder()
        return _ok_or_error(order.cancel_order(
            order_id=order_id, symbol=symbol, quantity=quantity
        ))
    except Exception as e:
        logger.error("[TOOLS] cancel_order failed: %s", e)
        return {"error": str(e)}


# ─── 10. get_market_overview ─────────────────────────────

@tool
def get_market_overview() -> Dict[str, Any]:
    """
    시장 전체 개요 — 상위 거래량/등락률 종목을 가져옵니다.

    Returns:
        {
            "top_gainers": [{ticker, name, current_price, change_rate, volume}, ...],
            "top_volume": [...],
            "count": int
        }
        또는 {"error": "..."}

    "시장 어때?", "오늘 핫한 종목" 등 시장 개관 질문에 호출하세요.
    """
    try:
        from ..services.stock_list_data import StockListDataProvider
        provider = StockListDataProvider()
        gainers = provider.get_sorted_market_stocks(sort_by="change_rate", order="desc", limit=10)
        volume = provider.get_sorted_market_stocks(sort_by="volume", order="desc", limit=10)
        return {
            "top_gainers": gainers.get("stocks", []) if "error" not in gainers else [],
            "top_volume": volume.get("stocks", []) if "error" not in volume else [],
            "count": len(gainers.get("stocks", [])) + len(volume.get("stocks", [])),
        }
    except Exception as e:
        logger.error("[TOOLS] get_market_overview failed: %s", e)
        return {"error": str(e)}


# ─── 11. send_telegram ───────────────────────────────────

@tool
def send_telegram(user_id: str, text: str) -> Dict[str, Any]:
    """
    사용자의 텔레그램에 메시지를 발송합니다.

    Args:
        user_id: 사용자 ID (TelegramLink 가 연동돼 있어야 함)
        text: 발송할 메시지 본문 (Markdown 가능)

    Returns:
        {"sent": bool, "telegram_message_id": int|None, "error": str|None}

    Agent 가 분석을 마치고 능동적으로 알림을 보낼 때 호출. 사용자가 채팅에서
    직접 받는 응답엔 호출하지 마세요 (그건 봇 핸들러가 자동 처리).
    """
    import os

    user = _get_user(user_id)
    if not user:
        return {"sent": False, "error": f"user {user_id} not found"}

    link = TelegramLink.objects.filter(user=user).first()
    if not link or not link.chat_id:
        return {"sent": False, "error": "텔레그램 연동이 되지 않았습니다"}

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return {"sent": False, "error": "TELEGRAM_BOT_TOKEN 미설정"}

    try:
        from telegram import Bot
        bot = Bot(token=token)

        # python-telegram-bot 21.x 의 send_message 는 coroutine — sync 컨텍스트에서
        # 호출하려면 asyncio.run 래핑 필요.
        async def _send() -> int:
            msg = await bot.send_message(
                chat_id=link.chat_id, text=text, parse_mode="Markdown"
            )
            return msg.message_id

        msg_id = asyncio.run(_send())
        NotificationLog.objects.create(
            user=user, kind="event", success=True,
            telegram_message_id=msg_id,
        )
        return {"sent": True, "telegram_message_id": msg_id, "error": None}
    except Exception as e:
        logger.error("[TOOLS] send_telegram failed: %s", e)
        NotificationLog.objects.create(
            user=user, kind="event", success=False, error_message=str(e),
        )
        return {"sent": False, "error": str(e)}


# ─── 12. get_notification_history ────────────────────────

@tool
def get_notification_history(user_id: str, days: int = 7) -> Dict[str, Any]:
    """
    사용자에게 보낸 최근 알림 이력을 조회합니다 (중복 방지용).

    Agent 의 priority 평가기가 "어제 이미 같은 종목 알림 보냈는지" 판단할 때 사용.

    Args:
        user_id: 사용자 ID
        days: 최근 며칠 (기본 7일)

    Returns:
        {
            "notifications": [
                {"kind": str, "sent_at": ISO8601, "success": bool,
                 "telegram_message_id": int|None, "error_message": str|None},
                ...
            ],
            "count": int
        }
        또는 {"error": "..."}
    """
    user = _get_user(user_id)
    if not user:
        return {"error": f"user {user_id} not found"}

    try:
        since = timezone.now() - timedelta(days=max(1, int(days)))
        rows = (
            NotificationLog.objects
            .filter(user=user, sent_at__gte=since)
            .order_by("-sent_at")[:100]
        )
        return {
            "notifications": [
                {
                    "kind": r.kind,
                    "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                    "success": r.success,
                    "telegram_message_id": r.telegram_message_id,
                    "error_message": r.error_message,
                }
                for r in rows
            ],
            "count": rows.count() if hasattr(rows, "count") else len(list(rows)),
        }
    except Exception as e:
        logger.error("[TOOLS] get_notification_history failed: %s", e)
        return {"error": str(e)}
