from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/stocks/search/$', consumers.StockSearchConsumer.as_asgi()),
    re_path(r'ws/stocks/ticker/(?P<symbol>\w+)/$', consumers.StockTickerConsumer.as_asgi()),
]
