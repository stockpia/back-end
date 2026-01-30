from django.urls import path
from .views import StockChartView, StockListView, StockHoldingView, StockWatchlistView

urlpatterns = [
    # 1. 종목 차트 API (/api/web/stocks/{symbol}/chart)
    path('stocks/<str:symbol>/chart', StockChartView.as_view()),

    # 2. 전체 종목 리스트 API (/api/web/stocks/list)
    path('stocks/list', StockListView.as_view()),

    # 3. 보유 종목 리스트 API (/api/web/stocks/holdings)
    path('stocks/holdings', StockHoldingView.as_view()),

    # 4. 관심 종목 리스트 API (/api/web/stocks/watchlist)
    path('stocks/watchlist', StockWatchlistView.as_view()),
]