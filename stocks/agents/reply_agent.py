"""
ReplyAgent — 사용자가 보낸 채팅 메시지(텔레그램/CLI)를 처리하는 LangGraph Agent.

흐름 (MATE_Agentic_설계_가이드.md §10-4):
  load_context → think (LLM + tools 바인딩) → [tool_call → think]* → END
  - 사용자 컨텍스트 (보유 종목 요약) 를 첫 SystemMessage 로 주입
  - 도구 자율 호출 (ALL_TOOLS) → 결과 다시 LLM 으로 → 최종 답변
  - 최대 5 step 후 강제 종료 (무한 루프 방지)
  - AgentConversation 모델에 멀티턴 메모리 저장

LLM:
  REPLY_AGENT_MODEL env (default "gpt-4o-mini") — tool calling 안정성 위해 gpt-5 보다
  검증된 gpt-4o-mini 를 기본값. OPENAI_API_KEY 그대로 사용.
"""
from __future__ import annotations

import logging
import os
from typing import Annotated, List, Optional, TypedDict

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

from ..models import AgentConversation, KisAccount, User
from .tools_registry import ALL_TOOLS

logger = logging.getLogger(__name__)

REPLY_AGENT_MODEL = (os.environ.get("REPLY_AGENT_MODEL") or "gpt-4o-mini").strip()
MAX_STEPS = 5
HISTORY_LIMIT = 10

SYSTEM_INSTRUCTION = (
    "당신은 한국 주식시장 전문 어시스턴트 MATE 입니다. "
    "초보 투자자가 이해할 수 있도록 쉽고 친근하게 설명합니다. "
    "매수/매도 추천은 하지 않고 정보 제공만 합니다.\n\n"
    "도구 사용 원칙:\n"
    "1. 사용자 보유 종목 / 시세 / 뉴스 / 재무 등 데이터가 필요하면 적절한 도구를 호출하세요.\n"
    "2. 도구 응답에 {\"error\": ...} 가 포함되면 사용자에게 솔직히 한계를 알리세요.\n"
    "3. 매매 (매수/매도) 는 현재 지원하지 않습니다. 매매 요청 시 정보 제공으로 안내하세요.\n"
    "4. 답변은 텔레그램 메시지로 보내질 거니 너무 길지 않게 2-4문장으로 정리하세요."
)


# ─── State ────────────────────────────────────────────────

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    user_id: str
    step_count: int


# ─── 컨텍스트 로딩 ───────────────────────────────────────

def _load_user_context_text(user_id: str) -> str:
    """사용자 보유/연동 상태를 짧은 요약 텍스트로. 첫 SystemMessage 에 부착."""
    try:
        user = User.objects.filter(user_id=user_id).first()
        if not user:
            return f"[사용자 컨텍스트] user_id={user_id} 정보 없음."

        lines = [f"[사용자 컨텍스트] 이름: {user.name}"]
        has_kis = KisAccount.objects.filter(user=user).exists()
        lines.append(f"KIS 계좌 연동: {'예' if has_kis else '아니오'}")
        lines.append(
            "알림 설정: morning="
            f"{user.notify_morning}, evening={user.notify_evening}, event={user.notify_event}"
        )
        return "\n".join(lines)
    except Exception as e:
        logger.warning("[REPLY-AGENT] load user context failed: %s", e)
        return f"[사용자 컨텍스트] 로드 실패: {e}"


def _load_history(user_id: str, limit: int = HISTORY_LIMIT) -> List[BaseMessage]:
    """AgentConversation → LangChain 메시지 변환 (오래된 → 최신 순)."""
    try:
        user = User.objects.filter(user_id=user_id).first()
        if not user:
            return []
        rows = list(
            AgentConversation.objects
            .filter(user=user)
            .order_by("-created_at")[:limit]
        )
        rows.reverse()
        messages: List[BaseMessage] = []
        for r in rows:
            if r.role == "user":
                messages.append(HumanMessage(content=r.content))
            elif r.role == "assistant":
                messages.append(AIMessage(
                    content=r.content,
                    tool_calls=r.tool_calls or [],
                ))
            elif r.role == "tool":
                messages.append(ToolMessage(
                    content=r.content,
                    tool_call_id=r.tool_call_id or "",
                ))
            # 'system' 은 매번 새로 생성하므로 저장된 건 무시
        return messages
    except Exception as e:
        logger.warning("[REPLY-AGENT] load history failed: %s", e)
        return []


def _persist_message(
    user_id: str,
    role: str,
    content: str,
    *,
    tool_calls: Optional[list] = None,
    tool_call_id: Optional[str] = None,
) -> None:
    try:
        user = User.objects.filter(user_id=user_id).first()
        if not user:
            return
        AgentConversation.objects.create(
            user=user,
            role=role,
            content=content or "",
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )
    except Exception as e:
        logger.error("[REPLY-AGENT] persist failed (role=%s): %s", role, e)


# ─── LLM + Graph ─────────────────────────────────────────

def _make_llm() -> ChatOpenAI:
    """gpt-5 계열은 temperature/reasoning_effort 제약이 있어 기본은 gpt-4o-mini."""
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY 미설정 — ReplyAgent 사용 불가")
    return ChatOpenAI(
        model=REPLY_AGENT_MODEL,
        api_key=key,
        temperature=0.3,
        max_retries=2,
        timeout=30,
    )


def _think_node_factory(llm_with_tools):
    def think(state: AgentState) -> dict:
        step = state.get("step_count", 0) + 1
        if step > MAX_STEPS:
            # 강제 종료 — 더 호출 안 되도록 빈 AIMessage (no tool_calls) 추가
            return {
                "messages": [AIMessage(content="(분석 단계 초과로 답변을 마무리합니다.)")],
                "step_count": step,
            }
        try:
            ai_msg = llm_with_tools.invoke(state["messages"])
        except Exception as e:
            logger.error("[REPLY-AGENT] LLM invoke failed: %s", e)
            return {
                "messages": [AIMessage(content="분석 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요.")],
                "step_count": step,
            }
        return {"messages": [ai_msg], "step_count": step}
    return think


def _should_continue(state: AgentState) -> str:
    if state.get("step_count", 0) >= MAX_STEPS:
        return END
    last = state["messages"][-1] if state["messages"] else None
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


def _build_graph(llm_with_tools) -> StateGraph:
    graph = StateGraph(AgentState)
    graph.add_node("think", _think_node_factory(llm_with_tools))
    graph.add_node("tools", ToolNode(ALL_TOOLS))

    graph.add_edge(START, "think")
    graph.add_conditional_edges("think", _should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "think")
    return graph.compile()


# ─── Public API ──────────────────────────────────────────

class ReplyAgent:
    """텔레그램/CLI 등에서 ReplyAgent().handle(user_id, message) 형태로 사용."""

    _shared_graph = None
    _shared_llm = None

    @classmethod
    def _get_graph(cls):
        if cls._shared_graph is None:
            cls._shared_llm = _make_llm()
            llm_with_tools = cls._shared_llm.bind_tools(ALL_TOOLS)
            cls._shared_graph = _build_graph(llm_with_tools)
        return cls._shared_graph

    def handle(self, user_id: str, message: str) -> str:
        """
        사용자 메시지 1건 처리 → 최종 응답 텍스트 반환.

        에러 시 사용자 노출 가능한 짧은 안내 문구를 반환 (raise X).
        """
        if not message or not message.strip():
            return "메시지가 비어 있어요. 무엇을 도와드릴까요?"

        try:
            graph = self._get_graph()
        except Exception as e:
            logger.error("[REPLY-AGENT] graph init failed: %s", e)
            return "Agent 초기화에 실패했어요. 관리자에게 문의해 주세요."

        # 컨텍스트 + 과거 대화 + 새 사용자 메시지
        ctx_text = _load_user_context_text(user_id)
        messages: List[BaseMessage] = [
            SystemMessage(content=SYSTEM_INSTRUCTION),
            SystemMessage(content=ctx_text),
        ]
        messages.extend(_load_history(user_id))
        messages.append(HumanMessage(content=message))

        # 사용자 메시지 먼저 저장 (실패해도 응답엔 영향 X)
        _persist_message(user_id, "user", message)

        try:
            final_state = graph.invoke({
                "messages": messages,
                "user_id": user_id,
                "step_count": 0,
            })
        except Exception as e:
            logger.error("[REPLY-AGENT] graph invoke failed: %s", e)
            return "분석 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요."

        # 새로 생성된 메시지만 추출 + 저장
        all_msgs = final_state.get("messages", [])
        new_msgs = all_msgs[len(messages):] if len(all_msgs) > len(messages) else []
        final_text = "답변을 생성하지 못했어요."
        for m in new_msgs:
            if isinstance(m, AIMessage):
                tool_calls_payload = [
                    {"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args")}
                    for tc in (m.tool_calls or [])
                ] if m.tool_calls else None
                _persist_message(
                    user_id, "assistant", m.content or "",
                    tool_calls=tool_calls_payload,
                )
                if m.content:
                    final_text = m.content  # 마지막 AIMessage.content
            elif isinstance(m, ToolMessage):
                _persist_message(
                    user_id, "tool", str(m.content)[:4000],  # 길면 잘라서 저장
                    tool_call_id=m.tool_call_id,
                )

        return final_text or "답변을 생성하지 못했어요."
