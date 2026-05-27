"""
stocks/services/llm_client.py

LLM 통합 호출 헬퍼.
- 우선순위: OpenAI(primary) → Gemini(fallback)
- 모든 텍스트 생성 호출은 이 모듈을 통해서만. silent fail 금지 (에러 로그 항상 남김).
- 키/모델은 환경변수에서 읽음:
    OPENAI_API_KEY, OPENAI_MODEL (default: gpt-5-mini)
    GEMINI_API_KEY                (fallback)
- 둘 다 실패하면 None 반환 — 호출자가 fallback 멘트로 처리.

기획서 §6.4 (환각·인젝션 방지) 의 system_instruction 도 여기서 고정.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_INSTRUCTION = (
    "당신은 한국 주식시장 전문 애널리스트입니다. "
    "초보 투자자가 이해할 수 있도록 쉽고 친근하게 설명합니다. "
    "매수/매도 추천은 하지 않고 정보 제공만 합니다."
)

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini").strip() or "gpt-5-mini"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"


# ─── OpenAI client (lazy) ─────────────────────────────────

_openai_client = None
_openai_failed_init = False


def _get_openai_client():
    global _openai_client, _openai_failed_init
    if _openai_client is not None or _openai_failed_init:
        return _openai_client
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        _openai_failed_init = True
        return None
    try:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=key)
        return _openai_client
    except Exception as e:
        logger.warning("OpenAI client init failed: %s", e)
        _openai_failed_init = True
        return None


# ─── Gemini fallback (lazy) ───────────────────────────────

_gemini_configured = False
_gemini_failed_init = False


def _ensure_gemini():
    global _gemini_configured, _gemini_failed_init
    if _gemini_configured or _gemini_failed_init:
        return _gemini_configured
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        _gemini_failed_init = True
        return False
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        _gemini_configured = True
        return True
    except Exception as e:
        logger.warning("Gemini configure failed: %s", e)
        _gemini_failed_init = True
        return False


# ─── Public API ───────────────────────────────────────────

def generate(
    prompt: str,
    *,
    system_instruction: str = DEFAULT_SYSTEM_INSTRUCTION,
    temperature: float = 0.3,
    max_output_tokens: int = 4096,
) -> Optional[str]:
    """
    LLM 으로 텍스트 생성. OpenAI 우선, 실패 시 Gemini fallback.
    어느 쪽도 성공 못하면 None 반환 (에러는 항상 logger 로 기록).

    Args:
        prompt: 사용자 prompt
        system_instruction: 시스템 지시문 (기본: 한국 주식 애널리스트)
        temperature: 0.0~1.0, 낮을수록 결정적
        max_output_tokens: 응답 최대 토큰

    Returns:
        생성된 텍스트 (앞뒤 공백 제거). 실패 시 None.
    """
    # 1) OpenAI primary
    text = _try_openai(prompt, system_instruction, temperature, max_output_tokens)
    if text:
        return text

    # 2) Gemini fallback
    text = _try_gemini(prompt, system_instruction, temperature, max_output_tokens)
    if text:
        return text

    return None


def _try_openai(prompt: str, system: str, temperature: float, max_tokens: int) -> Optional[str]:
    client = _get_openai_client()
    if client is None:
        return None

    # gpt-5 계열은 temperature 파라미터에 default(1) 외 다른 값을 거부함.
    # max_tokens 도 새로운 SDK 에선 max_completion_tokens 로 이름이 바뀜.
    is_gpt5 = OPENAI_MODEL.startswith("gpt-5")
    kwargs = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if not is_gpt5:
        kwargs["temperature"] = temperature

    # SDK 버전에 따라 max_completion_tokens / max_tokens 어느 쪽이 맞는지 자동 폴백
    for token_key in ("max_completion_tokens", "max_tokens"):
        try:
            resp = client.chat.completions.create(**kwargs, **{token_key: max_tokens})
            text = (resp.choices[0].message.content or "").strip()
            return text or None
        except TypeError:
            # 이 파라미터 미지원 SDK → 다른 키로 재시도
            continue
        except Exception as e:
            logger.error("[LLM-OPENAI-ERROR] %s: %s", type(e).__name__, e)
            return None
    return None


def _try_gemini(prompt: str, system: str, temperature: float, max_tokens: int) -> Optional[str]:
    if not _ensure_gemini():
        return None
    try:
        import google.generativeai as genai
        model = genai.GenerativeModel(GEMINI_MODEL, system_instruction=system)
        resp = model.generate_content(
            prompt,
            generation_config={"temperature": temperature, "max_output_tokens": max_tokens},
        )
        text = (resp.text or "").strip()
        return text or None
    except Exception as e:
        logger.error("[LLM-GEMINI-ERROR] %s: %s", type(e).__name__, e)
        return None
