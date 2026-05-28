"""
User.login_id 신설 + phone unique 해제.

3-step 안전 마이그레이션:
1) login_id 를 nullable 로 추가 (기존 row 가 NULL 로 채워짐)
2) 데이터 backfill — 기존 row 는 phone 값을 login_id 로 복사 (unique 보장됨, phone 도 unique 였으므로)
3) login_id 를 unique + not-null 로 alter, phone 은 nullable + unique 해제
"""
from django.db import migrations, models


def backfill_login_id(apps, schema_editor):
    User = apps.get_model("stocks", "User")
    for u in User.objects.all():
        if not u.login_id:
            u.login_id = u.phone or f"user-{str(u.user_id)[:8]}"
            u.save(update_fields=["login_id"])


def reverse_noop(apps, schema_editor):
    """역방향 — login_id 비우는 거 의미 없음."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("stocks", "0002_agent_conversation"),
    ]

    operations = [
        # 1) login_id nullable 추가
        migrations.AddField(
            model_name="user",
            name="login_id",
            field=models.CharField(max_length=50, null=True, unique=False),
        ),
        # 2) 기존 row backfill
        migrations.RunPython(backfill_login_id, reverse_noop),
        # 3a) login_id unique + not-null
        migrations.AlterField(
            model_name="user",
            name="login_id",
            field=models.CharField(max_length=50, null=False, unique=True),
        ),
        # 3b) phone 부가 정보로 — unique 해제 + nullable
        migrations.AlterField(
            model_name="user",
            name="phone",
            field=models.CharField(max_length=20, null=True, blank=True),
        ),
    ]
