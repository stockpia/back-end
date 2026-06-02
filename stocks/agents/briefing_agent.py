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
from ..services.glossary_api import GlossaryAPI
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
    "당신은 한국 주식시장 전문 어시스턴트 MATE 입니다.\n"
    "사용자가 묻지 않아도 매일 아침/저녁 능동적으로 보내는 브리핑을 작성합니다.\n\n"
    "원칙:\n"
    "1. 매수/매도 추천 금지 — 정보 제공만.\n"
    "2. 변동 ±1~2% 같이 자명한 정보는 생략. ±5% 이상이거나 호재/악재 우선.\n"
    "3. 최근 7일 알림 이력에 이미 보낸 내용은 중복하지 말 것.\n"
    "4. 데이터는 반드시 도구 호출로 수집 (추정·창작 금지):\n"
    "   - get_user_holdings(user_id): 보유 종목 + 수량·평가금액·수익률\n"
    "   - get_user_favorites(user_id): 관심 종목 리스트\n"
    "   - get_current_price(symbol): 현재가·전일 대비·거래량\n"
    "   - get_market_overview(): 코스피·코스닥 지수·등락률·특이사항\n"
    "   - get_stock_news(symbol, days): 종목 뉴스 (호재/악재 분류)\n"
    "   - get_financial_summary(symbol): 실적·재무 지표 (영업이익·EPS 등)\n"
    "   - get_notification_history(user_id, days): 최근 알림 (중복 방지)\n"
    "5. 텔레그램 메시지 — 1800자 이내 (한도 안에서 풍부하게). "
    "단순 나열보다 '왜 중요한지' 한 줄 해석을 매 섹션에 곁들이세요.\n"
    "6. 마크다운 굵게(`*텍스트*`)는 핵심 종목명·수치 위주. 이모지는 섹션 머리에만.\n"
    "7. 종목/페이지 링크는 plain URL 로 (텔레그램이 자동 클릭 링크화):\n"
    f"   - 종목 상세: {WEB_BASE_URL}/stocks/{{6자리코드}}\n"
    f"   - 거래 리포트: {WEB_BASE_URL}/trades/{{user_id}}\n"
    f"   - 알림/계정: {WEB_BASE_URL}/mypage\n\n"
    "고정 출력 포맷 (반드시 이 구조 + 섹션 이모지 그대로, 빈 섹션도 한 줄 안내로 채움):\n\n"
    "☕️/🌙 [짧고 매력적인 헤드라인] ([오늘 날짜, 예: 6월 2일])\n\n"
    "📊 시장 한눈에\n"
    "- 코스피·코스닥 지수 값 + 등락률 명시 (예: 코스피 6,598.87 -1.38%).\n"
    "- 마감 (저녁) 또는 미장·전일 흐름 (아침) 1줄.\n"
    "- 오늘 특이사항 (FOMC·실적 발표·정책 이벤트·환율·국제 이슈) 있으면 1줄 추가, 없으면 '오늘 특이사항 없음'.\n\n"
    "💼 내 포트폴리오\n"
    "- 1줄 요약: 총 평가 X원 / 예수금 Y원 / 보유 N개.\n"
    "- 보유 종목 중 수익률 상위 3개 또는 오늘 변동 큰 3개:\n"
    "  - *종목명*: 평가금액 (수익률 +/-X.XX%)  → 상세 링크\n"
    "- *평가 금액 및 수익률은 현재가 기준입니다.*\n"
    "- 보유 0개면 'KIS 계좌 연동 후 표시됩니다 → /mypage 링크' 안내.\n\n"
    "👀 관심종목\n"
    "- 관심 종목 중 오늘 변동 의미 있는 1-2개:\n"
    "  - *종목명*: 사유 (호재/악재 키워드) — 전일 대비 +/-X.X% → 상세 링크\n"
    "- 사유는 뉴스 헤드라인 또는 재무 이벤트에서 한 줄 (예: '수주 모멘텀', 'EV 둔화 우려', '실적 어닝서프라이즈').\n"
    "- 관심 종목 미설정이면 '관심종목 미설정 — 종목 페이지에서 ★ 추가' 안내.\n\n"
    "🤖 오늘의 AI 픽\n"
    "✅ *종목명 (6자리코드)*\n"
    "- 3-4문장 본문: (a) 최근 호재/이슈 + 수치 (실적·목표가·수주액 등) → "
    "(b) 그 의미·맥락 (왜 시장이 반응하는지) → (c) 관전 포인트 (다음 catalyst).\n"
    "- 상세 링크 1줄.\n"
    "- ※ 정보 제공이며 투자 권유가 아닙니다.\n\n"
    "📚 오늘의 용어\n"
    "[사용자 컨텍스트] 의 '오늘의 용어 카드' 를 그대로 본문에 옮기세요 "
    "(term + description + tip 구조 + 마크다운 굵게 유지).\n\n"
    "데이터가 없는 섹션은 한 줄 안내 ('보유 0개', '관심종목 미설정' 등) 로 채우고 섹션 자체는 생략하지 마세요."
)

PROMPT_MORNING = (
    "지금 시각은 장 시작 직전입니다 (아침 브리핑).\n"
    "헤드라인 이모지는 ☕️, '오늘의 MATE' 류 한 줄.\n"
    "포커스: 미장 흐름, 보유/관심 종목 시간외, 호재/악재, 오늘 챙길 일정.\n"
    "AI 픽은 사용자 보유/관심 종목 중 오늘 가장 의미 큰 1종목."
)

PROMPT_EVENING = (
    "지금 시각은 장 마감 후입니다 (저녁 브리핑).\n"
    "헤드라인 이모지는 🌙, '오늘의 마감 요약' 류 한 줄.\n"
    "포커스: 시장 마감 지수, 보유 종목 등락 결과, 내일 챙길 이슈.\n"
    "AI 픽은 오늘 가장 큰 이슈가 있었던 보유/관심 종목 1개."
)


def _pick_daily_term() -> str | None:
    """오늘 날짜 기반 deterministic 용어 1개 선택 (term + description + tip).
    매일 다른 용어가 노출되도록 ordinal day 를 mod 로 사용."""
    try:
        api = GlossaryAPI()
        terms = api.get_all_terms()
        if not terms:
            return None
        today = timezone.now().date()
        # date.toordinal() 가 매일 +1 → 같은 날 = 같은 용어, 다른 날 = 회전
        entry = api.lookup(terms[today.toordinal() % len(terms)])
        if not entry:
            return None
        name = entry.get("term", "")
        full_name = entry.get("full_name", "")
        desc = (entry.get("description", "") or "").strip()
        if not desc:
            return None
        header = f"*{name}*" + (f" ({full_name})" if full_name else "")
        # tip 후보 — interpretation 안의 정보가 있으면 한 줄 압축
        tip_raw = ""
        interp = entry.get("interpretation")
        if isinstance(interp, dict):
            # interpretation 안의 첫 값을 짧게
            for v in interp.values():
                if isinstance(v, str) and v.strip():
                    tip_raw = v.strip()
                    break
        example = (entry.get("example", "") or "").strip()
        tip = tip_raw or example
        lines = [header, desc]
        if tip:
            lines.append(f"- *Tip*: {tip}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("[BRIEFING] glossary term load failed: %s", e)
        return None


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

    # 오늘의 용어 카드 — 매일 다른 용어가 회전 노출.
    term_card = _pick_daily_term()
    if term_card:
        lines.append("")
        lines.append("[오늘의 용어 카드 — '📚 오늘의 용어' 섹션에 그대로 사용]")
        lines.append(term_card)

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
