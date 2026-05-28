"""
단일 사용자 즉시 브리핑 발송 (검증/시연용).

사용 예:
    python manage.py test_briefing --user-id <UUID> --kind morning
    python manage.py test_briefing --phone 010-1234-5678 --kind evening --dry-run

옵션:
    --send : 텔레그램 실제 발송. 기본값은 stdout 만.
"""
from __future__ import annotations

import asyncio
import logging
import os

from django.core.management.base import BaseCommand, CommandError

from stocks.agents.briefing_agent import BriefingAgent
from stocks.models import NotificationLog, TelegramLink, User

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "단일 사용자에게 즉시 브리핑 생성 (옵션으로 텔레그램 발송)"

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--user-id", type=str)
        group.add_argument("--phone", type=str)
        parser.add_argument(
            "--kind", type=str, required=True, choices=["morning", "evening"]
        )
        parser.add_argument(
            "--send", action="store_true",
            help="실제 텔레그램 발송 (기본값은 stdout 만)"
        )

    def handle(self, *args, **opts):
        kind: str = opts["kind"]

        if opts.get("user_id"):
            user = User.objects.filter(user_id=opts["user_id"]).first()
        else:
            user = User.objects.filter(phone=opts["phone"]).first()
        if not user:
            raise CommandError("사용자를 찾을 수 없습니다.")

        self.stdout.write(self.style.NOTICE(
            f"[user] {user.name} ({user.phone}) — user_id={user.user_id}"
        ))
        self.stdout.write(self.style.NOTICE(f"[kind] {kind}"))

        body = BriefingAgent().compose(str(user.user_id), kind)
        if not body:
            raise CommandError("브리핑 본문 생성 실패 (빈 응답)")

        self.stdout.write("─" * 60)
        self.stdout.write(self.style.SUCCESS("[body]"))
        self.stdout.write(body)
        self.stdout.write("─" * 60)

        if not opts.get("send"):
            self.stdout.write(self.style.WARNING(
                "[dry-run] 실제 발송하려면 --send 옵션 추가"
            ))
            return

        link = TelegramLink.objects.filter(user=user).first()
        if not link:
            raise CommandError("이 사용자는 텔레그램 연동이 안 돼 있어요.")

        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            raise CommandError("TELEGRAM_BOT_TOKEN 미설정")

        from telegram import Bot

        async def _send() -> int:
            bot = Bot(token=token)
            msg = await bot.send_message(
                chat_id=link.chat_id, text=body[:3900], parse_mode="Markdown"
            )
            return msg.message_id

        try:
            msg_id = asyncio.run(_send())
            NotificationLog.objects.create(
                user=user, kind=kind, success=True, telegram_message_id=msg_id,
            )
            self.stdout.write(self.style.SUCCESS(
                f"[sent] telegram_message_id={msg_id}"
            ))
        except Exception as e:
            NotificationLog.objects.create(
                user=user, kind=kind, success=False, error_message=str(e)[:500],
            )
            raise CommandError(f"발송 실패: {e}")
