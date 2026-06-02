"""
보유 종목 ±5% 변동 시 텔레그램 이벤트 알림.

systemd timer (정규장 09:00-15:30 KST, 15분 간격) 가 호출.
같은 종목·같은 날 1회만 알림 (NotificationLog kind="event:{ticker}" 로 중복 방지).

사용:
    python manage.py send_event_alert                  # 운영 발사
    python manage.py send_event_alert --dry-run        # 발사 없이 로그만
    python manage.py send_event_alert --threshold 3.0  # 임계치 ±3%
    python manage.py send_event_alert --force-time     # 장 시간 체크 우회 (테스트)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.utils import timezone

from stocks.models import NotificationLog, TelegramLink

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 5.0
WEB_BASE_URL = (
    os.environ.get("MATE_WEB_BASE_URL") or "https://aitrademate.netlify.app"
).strip().rstrip("/")
KST = dt_timezone(timedelta(hours=9))


def _is_market_hours_now() -> bool:
    """현재 KST 시간이 정규장 (평일 09:00 ~ 15:30) 인지."""
    now = datetime.now(tz=KST)
    if now.weekday() >= 5:  # 토(5), 일(6)
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 <= minutes <= 15 * 60 + 30


class Command(BaseCommand):
    help = "보유 종목 ±X% 변동 시 텔레그램 이벤트 알림 (default ±5%, 종목별 하루 1회)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="발사 없이 로그만")
        parser.add_argument(
            "--threshold", type=float, default=DEFAULT_THRESHOLD,
            help=f"임계치 % (default {DEFAULT_THRESHOLD})",
        )
        parser.add_argument(
            "--force-time", action="store_true",
            help="장 시간 체크 우회 (테스트용)",
        )

    def handle(self, *args, **opts):
        threshold: float = opts["threshold"]
        dry_run: bool = opts["dry_run"]
        force_time: bool = opts["force_time"]

        if not force_time and not _is_market_hours_now():
            self.stdout.write("[event] 장외 시간 — skip")
            return

        # 알림 토글 켠 + 텔레그램 연동된 사용자만
        targets = list(
            TelegramLink.objects
            .select_related("user")
            .filter(user__notify_event=True)
        )
        if not targets:
            tl_total = TelegramLink.objects.count()
            from stocks.models import User as _U
            ne_total = _U.objects.filter(notify_event=True).count()
            self.stdout.write(
                f"[event] 발사 대상 0명 — "
                f"TelegramLink={tl_total}건, notify_event=True 사용자={ne_total}명. "
                f"{'텔레그램 연동 필요' if tl_total == 0 else '둘 다 만족하는 사용자 없음'}"
            )
            return

        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not dry_run and not token:
            self.stderr.write("TELEGRAM_BOT_TOKEN 미설정 — 발송 불가")
            return

        triggered = self._collect_triggered_holdings(threshold)
        if not triggered:
            self.stdout.write(f"[event] |변동률|>={threshold}% 종목 없음")
            return

        self.stdout.write(self.style.NOTICE(
            f"[event] threshold={threshold}% triggered={len(triggered)}종목 "
            f"users={len(targets)} dry_run={dry_run}"
        ))

        today_start = timezone.now().replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

        ok = fail = skipped = 0
        for link in targets:
            for h in triggered:
                ticker = h["ticker"]
                kind_key = f"event:{ticker}"

                # 오늘 이미 보낸 알림인지
                already = NotificationLog.objects.filter(
                    user=link.user, kind=kind_key, success=True,
                    sent_at__gte=today_start,
                ).exists()
                if already:
                    skipped += 1
                    continue

                body = self._format_body(h)

                if dry_run:
                    self.stdout.write("─" * 40)
                    self.stdout.write(f"[{link.user.name}] dry-run: {kind_key}")
                    self.stdout.write(body)
                    ok += 1
                    continue

                msg_id = self._send_telegram(token, link.chat_id, body)
                if msg_id:
                    NotificationLog.objects.create(
                        user=link.user, kind=kind_key, success=True,
                        telegram_message_id=msg_id,
                    )
                    ok += 1
                else:
                    NotificationLog.objects.create(
                        user=link.user, kind=kind_key, success=False,
                        error_message="telegram send failed",
                    )
                    fail += 1

        self.stdout.write(self.style.NOTICE(
            f"[event] done — ok={ok} fail={fail} skipped={skipped}"
        ))

    # ─── helpers ────────────────────────────────────────

    def _collect_triggered_holdings(self, threshold: float) -> list[dict]:
        """보유 종목 순회 → 종목별 get_stock_price 호출 → 일일 변동률 ±threshold% 이상만."""
        try:
            from stocks.services.HantuStock import HantuStock
            from stocks.services.stock_list_data import StockListDataProvider
            holdings_result = StockListDataProvider().get_holding_stocks(
                sort_by="profit_rate", order="desc",
            )
        except Exception as e:
            logger.error("[event] holdings fetch failed: %s", e)
            return []

        if not isinstance(holdings_result, dict):
            return []
        holdings = holdings_result.get("stocks", []) or []
        if not holdings:
            return []

        hantu = HantuStock()
        triggered: list[dict] = []
        for h in holdings:
            ticker = h.get("ticker", "")
            if not ticker:
                continue
            try:
                price = hantu.get_stock_price(ticker)
            except Exception as e:
                logger.warning("[event] price fetch %s failed: %s", ticker, e)
                continue
            if not isinstance(price, dict) or price.get("error"):
                continue
            change = float(price.get("change_rate", 0) or 0)
            if abs(change) >= threshold:
                triggered.append({
                    "ticker": ticker,
                    "name": h.get("name", "") or price.get("name", ""),
                    "current_price": int(price.get("current_price", 0) or 0),
                    "change_rate": change,
                    "price_change": int(price.get("price_change", 0) or 0),
                })
        return triggered

    def _format_body(self, h: dict) -> str:
        change = h["change_rate"]
        arrow = "▲" if change > 0 else "▼"
        diff = h["price_change"]
        diff_str = f"{diff:+,}원" if diff else "-"
        return (
            f"📢 *{h['name']}* ({h['ticker']}) {arrow} *{change:+.2f}%*\n"
            f"현재가 {h['current_price']:,}원 (전일 대비 {diff_str})\n"
            f"{WEB_BASE_URL}/stocks/{h['ticker']}"
        )

    def _send_telegram(self, token: str, chat_id: int, text: str) -> int | None:
        from telegram import Bot

        async def _send() -> int:
            bot = Bot(token=token)
            msg = await bot.send_message(
                chat_id=chat_id, text=text, parse_mode="Markdown",
            )
            return msg.message_id

        try:
            return asyncio.run(_send())
        except Exception as e:
            logger.error("[event] telegram send failed chat=%s: %s", chat_id, e)
            return None
