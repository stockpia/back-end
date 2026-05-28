"""
ReplyAgent CLI 테스트 명령어.

사용 예:
    python manage.py test_reply_agent --user-id <UUID> --message "삼전 어때?"
    python manage.py test_reply_agent --phone 010-1234-5678 --message "내 보유 종목 알려줘"

연결된 사용자가 없으면 --phone 으로 lookup 한 뒤 user_id 를 콘솔에 보여주고
ReplyAgent.handle 결과를 출력합니다.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from stocks.models import User


class Command(BaseCommand):
    help = "ReplyAgent 를 CLI 에서 호출해 응답을 출력 (텔레그램 거치지 않음)"

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--user-id", type=str, help="User.user_id (UUID)")
        group.add_argument("--phone", type=str, help="User.phone 으로 lookup")
        parser.add_argument("--message", type=str, required=True, help="보낼 메시지")

    def handle(self, *args, **opts):
        message: str = opts["message"]

        if opts.get("user_id"):
            user_id = opts["user_id"]
            user = User.objects.filter(user_id=user_id).first()
        else:
            user = User.objects.filter(phone=opts["phone"]).first()
            user_id = str(user.user_id) if user else None

        if not user:
            raise CommandError("해당 사용자를 찾을 수 없습니다.")

        self.stdout.write(self.style.NOTICE(
            f"[user] {user.name} ({user.phone}) — user_id={user.user_id}"
        ))
        self.stdout.write(self.style.NOTICE(f"[in ] {message}"))

        from stocks.agents.reply_agent import ReplyAgent
        reply = ReplyAgent().handle(user_id, message)
        self.stdout.write(self.style.SUCCESS(f"[out] {reply}"))
