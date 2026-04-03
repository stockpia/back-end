from django.db import models

class KisAccount(models.Model):
    """한국투자증권 연동 계좌 정보"""
    user_id = models.CharField(max_length=100, unique=True, default="default_user", help_text="사용자 식별자")
    
    # 기본 정보
    name = models.CharField(max_length=50, help_text="이름")
    birthdate = models.CharField(max_length=8, help_text="생년월일 (YYYYMMDD)")
    phone = models.CharField(max_length=20, help_text="전화번호")
    
    # 계좌 정보
    account_number = models.CharField(max_length=20, help_text="한국투자증권 계좌번호")
    app_key = models.TextField(help_text="APP_KEY")
    app_secret_key = models.TextField(help_text="APP_SECRET_KEY")
    env = models.CharField(max_length=10, default="vps", help_text="prod(실전) or vps(모의)")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} - {self.account_number}"
