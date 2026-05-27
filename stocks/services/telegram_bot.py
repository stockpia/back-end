"""
stocks/services/telegram_bot.py

Telegram MATE 봇 워커.
- 사용자 ↔ Telegram chat_id 매핑 (Web 마이페이지 Deep Link 클릭 → /start <token>)
- Deep link 토큰은 LinkToken 모델 (TTL 10분, 1회용)

실행:
    개발: python -m stocks.services.telegram_bot
    운영: systemd 의 stockpia-bot.service (이 모듈을 python 으로 실행)
"""
from __future__ import annotations

import logging
import os

import django

# Django ORM 사용을 위해 setup 먼저
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "stockpia.settings")
django.setup()

from asgiref.sync import sync_to_async  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.error import TelegramError  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CommandHandler,
    ContextTypes,
)

from stocks.models import LinkToken, TelegramLink  # noqa: E402

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")

WELCOME_NO_TOKEN = (
    "안녕하세요! MATE — AI 투자 비서입니다.\n\n"
    "먼저 웹에서 회원가입 후, 마이페이지에서\n"
    "'텔레그램 알림 받기' 버튼을 눌러주세요.\n\n"
    "그러면 자동으로 이 채팅이 연결되어\n"
    "매일 장 시작/마감 브리핑을 받아보실 수 있어요."
)


# ─── 핸들러 ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/start [token]` — 토큰이 있으면 계정 연결, 없으면 안내."""
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    args = context.args or []

    if not args:
        await context.bot.send_message(chat_id=chat.id, text=WELCOME_NO_TOKEN)
        return

    token_str = args[0].strip()
    result = await _try_link(token_str, chat_id=chat.id, telegram_username=user.username)

    await context.bot.send_message(chat_id=chat.id, text=result)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            "사용 가능한 명령:\n"
            "/start - 연동 안내\n"
            "/help - 이 도움말\n\n"
            "연동 후에는 정해진 시각에 자동으로 브리핑이 도착합니다."
        ),
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """헬스체크용. 운영 후 비활성 가능."""
    chat = update.effective_chat
    if chat is None:
        return
    await context.bot.send_message(chat_id=chat.id, text="pong")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """미처리 예외 캐치 → 로그 + 사용자에게 안내."""
    logger.exception("Unhandled error while processing update: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_chat is not None:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "⚠️ 일시적 오류가 발생했어요. 잠시 후 다시 시도해주세요.\n"
                    "계속 같은 문제가 반복되면 관리자에게 알려주세요."
                ),
            )
    except TelegramError:
        pass


# ─── DB 작업 (sync ORM 을 async wrapper 로) ───────────────

@sync_to_async
def _try_link(token_str: str, chat_id: int, telegram_username: str | None) -> str:
    """LinkToken 조회 → 유효하면 TelegramLink 생성/갱신."""
    try:
        tok = LinkToken.objects.select_related("user").get(token=token_str)
    except LinkToken.DoesNotExist:
        return "❌ 유효하지 않은 링크입니다.\n웹에서 새 링크를 발급받아주세요."

    if not tok.is_valid():
        return (
            "❌ 만료되었거나 이미 사용된 링크입니다.\n"
            "웹 마이페이지에서 새 링크를 발급받아주세요."
        )

    # chat_id 중복(다른 사용자가 동일 chat 으로 이미 연결된 경우) 방어
    existing = TelegramLink.objects.filter(chat_id=chat_id).exclude(user=tok.user).first()
    if existing is not None:
        return (
            "❌ 이 텔레그램 계정은 이미 다른 사용자와 연결되어 있어요.\n"
            "기존 계정의 마이페이지에서 연결 해제 후 다시 시도해주세요."
        )

    TelegramLink.objects.update_or_create(
        user=tok.user,
        defaults={"chat_id": chat_id, "telegram_username": telegram_username},
    )
    tok.consumed = True
    tok.save(update_fields=["consumed"])

    return (
        f"✅ {tok.user.name}님 계정과 연결됐어요!\n\n"
        "매일 장 시작(08:30) / 마감(16:00) 시간에\n"
        "맞춤 브리핑을 보내드릴게요.\n\n"
        "/help 로 사용 가능한 명령을 확인하실 수 있어요."
    )


# ─── 실행 진입점 ─────────────────────────────────────────

def run_bot() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "환경변수 TELEGRAM_BOT_TOKEN 이 설정되지 않았습니다."
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [telegram-bot] %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Starting MATE Telegram bot worker (username=%s)", BOT_USERNAME)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_error_handler(on_error)

    # MVP: long-polling. 운영 webhook 전환은 추후.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
