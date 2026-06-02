"""
BriefingAgent — 하루 2회 자동 브리핑 본문 생성기.

ReplyAgent 와 LangGraph 그래프 구조는 동일하지만 목적이 다름:
- 사용자가 묻기 전에 능동적으로 보내는 메시지
- 시스템 프롬프트가 kind (morning|evening) 에 따라 분기
- 응답은 텔레그램 메시지 본문 (사용자 발송은 호출자 책임)

스팸 방지 정책 (MATE_Agentic_설계_가이드.md §5-2):
- 시스템 프롬프트에 "단순 ±1~2% 변동이면 SKIP", "어제 보낸 내용 중복 X" 명시
- 사용자 컨텍스트에 최근 7일 알림 이력을 포함시켜 LLM 이 중복 판단

발송 흐름:
    BriefingAgent.compose(user_id, kind="morning") → str (본문)
    호출자가 send_telegram tool 로 발송 + NotificationLog 저장.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Annotated, List, TypedDict

from django.utils import timezone
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from ..models import KisAccount, NotificationLog, User
from .tools_registry import ALL_TOOLS

logger = logging.getLogger(__name__)

BRIEFING_MODEL = (os.environ.get("BRIEFING_AGENT_MODEL") or "gpt-4o-mini").strip()
MAX_STEPS = 6  # ReplyAgent 보다 1 더 — 도구 여러 개 호출 여지

# 웹앱 deep link 베이스 — 텔레그램 메시지의 종목/페이지 링크에 사용.
# 운영에서 도메인 변경 시 .env 의 MATE_WEB_BASE_URL 로 오버라이드.
WEB_BASE_URL = (
    os.environ.get("MATE_WEB_BASE_URL") or "https://aitrademate.netlify.app"
).strip().rstrip("/")

SYSTEM_BASE = (
    "당신은 한국 주식시장 전문 어시스턴트 MATE 입니다. "
    "사용자가 묻지 않았지만 능동적으로 보내는 브리핑을 작성합니다.\n\n"
    "원칙:\n"
    "1. 매수/매도 추천 금지 — 정보 제공만.\n"
    "2. 변동 ±1~2% 같이 자명한 정보는 생략. ±5% 이상이거나 호재/악재만 언급.\n"
    "3. 최근 7일 알림 이력에 이미 보낸 내용은 중복하지 말 것.\n"
    "4. 정보가 부족할 땐 도구 (get_user_holdings, get_current_price, get_stock_news, "
    "get_market_overview, get_notification_history 등) 를 자율 호출.\n"
    "5. 답변은 텔레그램 메시지 — 5~8문장 / 1200자 이내. 정보 양보다 '왜 중요한지' 한 줄 해석을 곁들이세요.\n"
    "6. 마크다운 굵게(`*텍스트*`)는 가능하나 과용 금지.\n"
    "7. 종목 / 페이지 링크 동봉:\n"
    f"   - 종목 상세 리포트: {WEB_BASE_URL}/stocks/{{6자리코드}} — 차트·재무·AI 의견.\n"
    f"   - 보유 종목 거래 리포트: {WEB_BASE_URL}/trades/{{user_id}} — 매매 기록·집중도.\n"
    f"   - 알림/계정 설정: {WEB_BASE_URL}/mypage\n"
    "   주요 종목 언급 시 위 URL 형식으로 한 줄에 하나씩 첨부 (텔레그램이 자동 클릭 링크화).\n"
    "8. 구조: ① 한 줄 헤드라인 → ② 핵심 이슈 2-4개 (각 1-2문장 + 링크) → ③ 다음 행동 안내 한 줄.\n"
)

PROMPT_MORNING = (
    "지금 시각은 장 시작 직전입니다. "
    "사용자의 보유 종목 + 관심 종목 기준으로 '오늘 시장 시작 전에 알아둘 것' "
    "을 정리해 주세요. 미장 흐름, 보유 종목 시간외, 호재/악재 뉴스 우선. "
    "각 종목 언급 시 상세 페이지 링크를 한 줄로 첨부하세요."
)

PROMPT_EVENING = (
    "지금 시각은 장 마감 후입니다. "
    "사용자의 보유 종목 결과 위주로 '오늘 시장이 어땠는지' "
    "를 정리해 주세요. 등락 큰 종목, 내일 챙길 이슈 우선. "
    "큰 변동 종목은 상세 리포트 링크, 마지막 줄에는 거래 리포트(/trades/{user_id}) 링크로 마무리."
)


# ─── State + Graph ───────────────────────────────────────

class BriefingState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    user_id: str
    kind: str
    step_count: int


def _user_context_text(user_id: str, kind: str) -> str:
    """보유 / 알림 설정 / 최근 알림 이력을 짧은 텍스트로 묶어 컨텍스트로 주입."""
    user = User.objects.filter(user_id=user_id).first()
    if not user:
        return f"[사용자 컨텍스트] user_id={user_id} 정보 없음."

    lines = [
        f"[사용자 컨텍스트] 이름: {user.name}",
        f"user_id: {user_id} (링크용 — /trades/{user_id} 등)",
        f"kind: {kind} (morning=장 시작 전, evening=장 마감 후)",
        f"KIS 연동: {'예' if KisAccount.objects.filter(user=user).exists() else '아니오'}",
        f"알림 설정: morning={user.notify_morning}, evening={user.notify_evening}, event={user.notify_event}",
        f"웹앱 base URL: {WEB_BASE_URL}",
    ]

    # 최근 7일 알림 이력
    since = timezone.now() - timedelta(days=7)
    recent = (
        NotificationLog.objects
        .filter(user=user, sent_at__gte=since)
        .order_by("-sent_at")[:10]
    )
    if recent:
        lines.append("최근 7일 알림 이력 (중복 방지용):")
        for r in recent:
            lines.append(f"  - {r.sent_at:%m-%d %H:%M} kind={r.kind} success={r.success}")
    return "\n".join(lines)


def _make_llm() -> ChatOpenAI:
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY 미설정 — BriefingAgent 사용 불가")
    return ChatOpenAI(
        model=BRIEFING_MODEL, api_key=key,
        temperature=0.3, max_retries=2, timeout=30,
    )


def _think_factory(llm_with_tools):
    def think(state: BriefingState) -> dict:
        step = state.get("step_count", 0) + 1
        if step > MAX_STEPS:
            return {
                "messages": [AIMessage(content="(분석 단계 초과 — 브리핑을 건너뜁니다.)")],
                "step_count": step,
            }
        try:
            ai = llm_with_tools.invoke(state["messages"])
        except Exception as e:
            logger.error("[BRIEFING] LLM invoke failed: %s", e)
            return {
                "messages": [AIMessage(content="(브리핑 생성 중 일시적 문제가 발생했어요.)")],
                "step_count": step,
            }
        return {"messages": [ai], "step_count": step}
    return think


def _should_continue(state: BriefingState) -> str:
    if state.get("step_count", 0) >= MAX_STEPS:
        return END
    last = state["messages"][-1] if state["messages"] else None
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


def _build_graph(llm_with_tools):
    g = StateGraph(BriefingState)
    g.add_node("think", _think_factory(llm_with_tools))
    g.add_node("tools", ToolNode(ALL_TOOLS))
    g.add_edge(START, "think")
    g.add_conditional_edges("think", _should_continue, {"tools": "tools", END: END})
    g.add_edge("tools", "think")
    return g.compile()


# ─── Public API ──────────────────────────────────────────

class BriefingAgent:
    """BriefingAgent().compose(user_id, kind) → str (텔레그램 본문)."""

    _shared_graph = None
    _shared_llm = None

    @classmethod
    def _get_graph(cls):
        if cls._shared_graph is None:
            cls._shared_llm = _make_llm()
            cls._shared_graph = _build_graph(cls._shared_llm.bind_tools(ALL_TOOLS))
        return cls._shared_graph

    def compose(self, user_id: str, kind: str) -> str:
        if kind not in ("morning", "evening"):
            return f"(알 수 없는 brief kind: {kind})"

        try:
            graph = self._get_graph()
        except Exception as e:
            logger.error("[BRIEFING] graph init failed: %s", e)
            return ""

        ctx_text = _user_context_text(user_id, kind)
        prompt = PROMPT_MORNING if kind == "morning" else PROMPT_EVENING

        messages: List[BaseMessage] = [
            SystemMessage(content=SYSTEM_BASE),
            SystemMessage(content=ctx_text),
            HumanMessage(content=prompt),
        ]

        try:
            final = graph.invoke({
                "messages": messages, "user_id": user_id,
                "kind": kind, "step_count": 0,
            })
        except Exception as e:
            logger.error("[BRIEFING] graph invoke failed: %s", e)
            return ""

        # 마지막 AIMessage 의 content 만 반환
        for m in reversed(final.get("messages", [])):
            if isinstance(m, AIMessage) and m.content and not m.tool_calls:
                return m.content
            if isinstance(m, ToolMessage):
                continue
        return ""
