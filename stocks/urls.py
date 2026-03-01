from django.urls import path
from .views import (StockChartView, StockListView, StockHoldingView, StockWatchlistView,
                    StockNewsView, StockCommunityView, StockCommunityLatestView,
                    AveragingHoldingView, AveragingCalculateQuantityView, AveragingCalculateAmountView,
                    AveragingSaveView, AveragingHistoryView,
                    StockDetailReportView,
                    StockReportView, StockFavoriteToggleView, StockFavoriteListView,
                    )

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


    # web03-1 보유 종목 정보 조회
    # GET /api/web/averaging/holding/{symbol}
    path('averaging/holding/<str:symbol>', AveragingHoldingView.as_view(), name='averaging-holding'),

    # web03-2-1 수량 기준 물타기 계산
    # POST /api/web/averaging/calculate/quantity
    path('averaging/calculate/quantity', AveragingCalculateQuantityView.as_view(), name='averaging-calc-quantity'),

    # web03-2-2 금액 기준 물타기 계산
    # POST /api/web/averaging/calculate/amount
    path('averaging/calculate/amount', AveragingCalculateAmountView.as_view(), name='averaging-calc-amount'),

    # web03-3 계산 결과 저장
    # POST /api/web/averaging/save
    path('averaging/save', AveragingSaveView.as_view(), name='averaging-save'),

    # web03-4 계산 히스토리 조회
    # GET /api/web/averaging/history/{symbol}
    path('averaging/history/<str:symbol>', AveragingHistoryView.as_view(), name='averaging-history'),


    # web04 종목 상세 리포트 API
    # GET /api/web/stocks/<str:symbol>/detail
    path('stocks/<str:symbol>/detail', StockDetailReportView.as_view(), name='stock-detail'),


    # web05-1 종목 리포트 조회 (요약 + 5개 섹션)
    # GET /api/web/stocks/{symbol}/report
    path('stocks/<str:symbol>/report', StockReportView.as_view(), name='stock-report'),

    # web05-2 관심 종목 추가/해제
    # POST /api/web/stocks/{symbol}/favorite
    path('stocks/<str:symbol>/favorite', StockFavoriteToggleView.as_view(), name='stock-favorite-toggle'),

    # web05-3 관심 종목 목록 조회
    # GET /api/web/favorites
    path('favorites', StockFavoriteListView.as_view(), name='stock-favorite-list'),
]