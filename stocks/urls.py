from django.urls import path
from .views import StockChartView, StockListView, StockHoldingView, StockWatchlistView, StockNewsView

urlpatterns = [
    # web01-1 종목 차트 API (/api/web/stocks/{symbol}/chart)
    path('stocks/<str:symbol>/chart', StockChartView.as_view()),

    # web01-2 전체 종목 리스트 API (/api/web/stocks/list)
    path('stocks/list', StockListView.as_view()),

    # web01-3 보유 종목 리스트 API (/api/web/stocks/holdings)
    path('stocks/holdings', StockHoldingView.as_view()),

    # web01-4 관심 종목 리스트 API (/api/web/stocks/watchlist)
    path('stocks/watchlist', StockWatchlistView.as_view()),


    # web02-1 종목 뉴스 및 AI 요약 API (/api/web/stocks/{symbol}/news)
    path('stocks/<str:symbol>/news', StockNewsView.as_view()),
]