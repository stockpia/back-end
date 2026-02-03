from django.urls import path
from .views import (StockChartView, StockListView, StockHoldingView, StockWatchlistView,
                    StockNewsView, StockCommunityView, StockCommunityLatestView)

urlpatterns = [
    # web01-1 종목 차트 API (/api/web/stocks/{symbol}/chart)
    path('stocks/<str:symbol>/chart', StockChartView.as_view()),

    # web01-2 전체 종목 리스트 API (/api/web/stocks/list)
    path('stocks/list', StockListView.as_view()),

    # web01-3 보유 종목 리스트 API (/api/web/stocks/holdings)
    path('stocks/holdings', StockHoldingView.as_view()),

    # web01-4 관심 종목 리스트 API (/api/web/stocks/watchlist)
    path('stocks/watchlist', StockWatchlistView.as_view()),


    # web02-1 뉴스 리스트 API (/api/web/stocks/{symbol}/news)
    path('stocks/<str:symbol>/news', StockNewsView.as_view(), name='stock-news'),

    # web02-2. 커뮤니티 과거 글 로드 / 최신 조회 API (/api/web/stocks/{symbol}/community)
    path('stocks/<str:symbol>/community', StockCommunityView.as_view(), name='stock-community'),

    # web02-3. 커뮤니티 새 글 확인용 (/api/web/stocks/{symbol}/community/latest)
    path('stocks/<str:symbol>/community/latest', StockCommunityLatestView.as_view(), name='stock-community-latest'),
]