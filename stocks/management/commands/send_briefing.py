"""
하루 2회 자동 브리핑 발송 (systemd timer 가 호출).

사용 예:
    python manage.py send_briefing --kind morning
    python manage.py send_briefing --kind evening
    python manage.py send_briefing --kind morning --dry-run  # 발송 X, 본문만 출력

대상: TelegramLink 연동된 사용자 중 해당 kind 알림 토글이 켜진 사람.
실패해도 다음 사용자로 계속 진행 (한 명 실패가 전체 작업 중단 X).
"""
from __future__ import annotations

import asyncio
import logging
import os

from django.core.management.base import BaseCommand, CommandError

from stocks.agents.briefing_agent import BriefingAgent
from stocks.models import NotificationLog, TelegramLink

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "정기 브리핑 발송 (kind=morning|evening)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--kind", type=str, required=True, choices=["morning", "evening"],
            help="브리핑 종류"
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="발송 안 하고 본문만 stdout 에 출력"
        )

    def handle(self, *args, **opts):
        kind: str = opts["kind"]
        dry_run: bool = opts["dry_run"]

        notify_field = f"notify_{kind}"
        # TelegramLink 연동 + 해당 kind 토글 켜진 사용자만
        links = (
            TelegramLink.objects
            .select_related("user")
            .filter(**{f"user__{notify_field}": True})
        )
        targets = list(links)

        self.stdout.write(self.style.NOTICE(
            f"[briefing] kind={kind} targets={len(targets)} dry_run={dry_run}"
        ))

        if not targets:
            self.stdout.write(self.style.WARNING("[briefing] 대상 사용자 없음 — 종료"))
            return

        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not dry_run and not token:
            raise CommandError("TELEGRAM_BOT_TOKEN 미설정 — 발송 불가 (또는 --dry-run)")

        agent = BriefingAgent()
        ok = 0
        fail = 0
        for link in targets:
            uid = str(link.user.user_id)
            try:
                body = agent.compose(uid, kind)
            except Exception as e:
                logger.exception("[briefing] compose failed user=%s: %s", uid, e)
                fail += 1
                self._log_failure(link.user, kind, f"compose: {e}")
                continue

            if not body:
                fail += 1
                self._log_failure(link.user, kind, "empty body")
                self.stdout.write(self.style.ERROR(f"[{uid}] empty body — skip"))
                continue

            self.stdout.write("─" * 60)
            self.stdout.write(self.style.SUCCESS(f"[{uid}] {link.user.name} ← {kind}"))
            self.stdout.write(body[:600])

            if dry_run:
                ok += 1
                continue

            # 실 발송
            sent_id = self._send_telegram(token, link.chat_id, body)
            if sent_id:
                ok += 1
                NotificationLog.objects.create(
                    user=link.user, kind=kind, success=True,
                    telegram_message_id=sent_id,
                )
            else:
                fail += 1
                self._log_failure(link.user, kind, "telegram send failed")

        self.stdout.write("─" * 60)
        self.stdout.write(self.style.NOTICE(
            f"[briefing] done — ok={ok} fail={fail}"
        ))

    # ─── helpers ────────────────────────────────────────

    def _send_telegram(self, token: str, chat_id: int, text: str) -> int | None:
        from telegram import Bot

        async def _send() -> int:
            bot = Bot(token=token)
            msg = await bot.send_message(
                chat_id=chat_id, text=text[:3900], parse_mode="Markdown"
            )
            return msg.message_id

        try:
            return asyncio.run(_send())
        except Exception as e:
            logger.error("[briefing] telegram send failed chat=%s: %s", chat_id, e)
            return None

    def _log_failure(self, user, kind: str, err: str) -> None:
        try:
            NotificationLog.objects.create(
                user=user, kind=kind, success=False, error_message=err[:500],
            )
        except Exception:
            pass
