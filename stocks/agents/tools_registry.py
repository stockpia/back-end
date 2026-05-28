"""
LangGraph 가 바인딩할 도구 레지스트리.

ReplyAgent / BriefingAgent 등 모든 Agent 는 여기서 ALL_TOOLS 를 import 해서
같은 카탈로그를 공유 (Agent 마다 도구 새로 짤 필요 X — 설계 가이드 §3 원칙).
"""
from __future__ import annotations

from typing import Dict, List

from .tools import (
    calculate_averaging,
    cancel_order,
    get_current_price,
    get_financial_summary,
    get_market_overview,
    get_notification_history,
    get_pending_orders,
    get_stock_news,
    get_user_favorites,
    get_user_holdings,
    place_order,
    send_telegram,
)

ALL_TOOLS = [
    get_user_holdings,
    get_user_favorites,
    get_current_price,
    get_stock_news,
    get_financial_summary,
    calculate_averaging,
    get_pending_orders,
    place_order,
    cancel_order,
    get_market_overview,
    send_telegram,
    get_notification_history,
]

# 이름 → 도구 매핑 (LangGraph ToolNode 가 tool_calls[i].name 으로 dispatch)
TOOLS_BY_NAME: Dict[str, object] = {t.name: t for t in ALL_TOOLS}


def list_tool_names() -> List[str]:
    """카탈로그에 등록된 도구 이름들 (테스트 / 디버깅용)."""
    return [t.name for t in ALL_TOOLS]
