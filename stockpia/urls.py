"""
URL configuration for stockpia project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import include, path, re_path
from django.http import HttpResponse
from django.conf import settings
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

api_patterns = [
    path('api/web/', include('stocks.urls')),
]

schema_view = get_schema_view(
    openapi.Info(
        title="Stockpia API",
        default_version='v1',
        description="Stockpia 프로젝트 API 문서",
        terms_of_service="https://www.google.com/policies/terms/",
        contact=openapi.Contact(email="contact@stockpia.local"),
        license=openapi.License(name="BSD License"),
    ),
    public=True,
    permission_classes=[permissions.AllowAny],
    authentication_classes=[],
    patterns=api_patterns,  # swagger 스캔 범위 지정(웹소켓 제외)
)

def health_check(request):
    return HttpResponse("Stockpia API Server is Running", status=200)

urlpatterns = [
    path('', health_check),
    path('admin/', admin.site.urls),
    
    # 실제 API 서비스 경로
    path('', include(api_patterns)),

    # Swagger UI 경로
    re_path(r'^swagger(?P<format>\.json|\.yaml)$', schema_view.without_ui(cache_timeout=0), name='schema-json'),
    re_path(r'^swagger/$', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    re_path(r'^redoc/$', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
]
