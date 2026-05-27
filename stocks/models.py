import uuid
from django.db import models
from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from datetime import timedelta

from .services.crypto_utils import encrypt, decrypt

class User(models.Model):
    """
    사용자 정보 (users)
    """
    user_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    phone = models.CharField(max_length=20, unique=True, null=False)
    name = models.CharField(max_length=50, null=False)
    birthdate = models.CharField(max_length=8, null=False)  # YYYYMMDD
    password_hash = models.CharField(max_length=128, null=False)
    
    notify_morning = models.BooleanField(default=True)
    notify_evening = models.BooleanField(default=True)
    notify_event = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def set_password(self, raw_password):
        self.password_hash = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password_hash)

    def __str__(self):
        return f"{self.name} ({self.phone})"

class KisAccount(models.Model):
    """
    한국투자증권 계좌 정보 (kis_accounts)
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    account_number = models.CharField(max_length=20, null=False)
    
    # 암호화된 필드
    app_key_enc = models.BinaryField(null=False)
    app_secret_key_enc = models.BinaryField(null=False)
    
    env = models.CharField(max_length=10, default='vps', null=False) # 'vps' or 'prod'
    permission_scope = models.CharField(max_length=20, default='read_trade', null=False) # 'read_only' or 'read_trade'
    
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def set_app_key(self, plain_text_key):
        self.app_key_enc = encrypt(plain_text_key)

    def get_app_key(self):
        return decrypt(self.app_key_enc)

    def set_app_secret_key(self, plain_text_key):
        self.app_secret_key_enc = encrypt(plain_text_key)

    def get_app_secret_key(self):
        return decrypt(self.app_secret_key_enc)

    def __str__(self):
        return f"{self.user.name}'s KIS Account ({self.account_number})"

class TelegramLink(models.Model):
    """
    텔레그램 연동 정보 (telegram_links)
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    chat_id = models.BigIntegerField(unique=True, null=False)
    telegram_username = models.CharField(max_length=64, null=True, blank=True)
    linked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.name} <-> {self.telegram_username or self.chat_id}"

def get_token_expiry_date():
    return timezone.now() + timedelta(minutes=10)


def _generate_link_token():
    """
    LinkToken.token 의 default. 함수 객체로 전달해야 row 생성 때마다 새 값이 평가됨.
    `default=uuid.uuid4().hex` 처럼 호출 결과를 직접 전달하면 모듈 import 시점에 1회만
    평가되어 모든 row 가 같은 PK 를 받게 됨 (IntegrityError).
    """
    return uuid.uuid4().hex

class LinkToken(models.Model):
    """
    일회용 연동 토큰 (link_tokens)
    """
    token = models.CharField(max_length=32, primary_key=True, default=_generate_link_token)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    expires_at = models.DateTimeField(default=get_token_expiry_date)
    consumed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_expired(self):
        return self.expires_at < timezone.now()

class WatchSymbol(models.Model):
    """
    관심 종목 (watch_symbols)
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    symbol = models.CharField(max_length=10, null=False)
    order = models.SmallIntegerField(default=0) # 0, 1, 2
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'symbol')

    def __str__(self):
        return f"{self.user.name} watches {self.symbol}"

class NotificationLog(models.Model):
    """
    알림 발송 로그 (notification_logs)
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    kind = models.CharField(max_length=20, null=False) # 'morning', 'evening', 'event'
    sent_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField()
    error_message = models.TextField(null=True, blank=True)
    telegram_message_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'kind', 'sent_at'], name='notif_user_kind_idx'),
        ]

    def __str__(self):
        return f"Notification for {self.user.name} at {self.sent_at}"
