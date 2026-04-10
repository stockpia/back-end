"""
ASGI config for stockpia project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator
import stocks.routing

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'stockpia.settings')

# 기본 Django HTTP 애플리케이션
django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter({
    # HTTP 요청은 기존 Django 뷰가 처리
    "http": django_asgi_app,
    # WebSocket 요청은 Channels가 처리
    "websocket": AllowedHostsOriginValidator(  # settings.ALLOWED_HOSTS 검증
        AuthMiddlewareStack(
            URLRouter(
                stocks.routing.websocket_urlpatterns
            )
        )
    ),
})
