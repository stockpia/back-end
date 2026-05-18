from django.urls import path
from .views import (
    StockChartView, StockListView, StockHoldingView,
    StockNewsView, StockCommunityView, StockCommunityLatestView,
    AveragingHoldingView, AveragingCalculateQuantityView, AveragingCalculateAmountView,
    AveragingSaveView, AveragingHistoryView,
    StockDetailReportView,
    StockReportView,
    UserSignUpView, UserSignInView, KisAccountConnectView, UserDetailView, # UserDetailView 추가
    OrderBookView, AccountBalanceView, AccountHoldingsView,
    OrderView, PendingOrdersView, CancelOrderView,
)

urlpatterns = [
    # 사용자 인증 및 계좌 연동
    path('users/signup', UserSignUpView.as_view(), name='user-signup'),
    path('users/signin', UserSignInView.as_view(), name='user-signin'),
    path('users/<uuid:user_id>/', UserDetailView.as_view(), name='user-detail'),
    path('kis/connect', KisAccountConnectView.as_view(), name='kis-connect'),

    # web01-1 종목 차트 API (/api/web/stocks/{symbol}/chart)
    path('stocks/<str:symbol>/chart', StockChartView.as_view()),

    # web01-2 전체 종목 리스트 API (/api/web/stocks/list)
    path('stocks/list', StockListView.as_view()),

    # web01-3 보유 종목 리스트 API (/api/web/stocks/holdings)
    path('stocks/holdings', StockHoldingView.as_view()),

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

    # Web 06 매매 (경로를 /api/web/ 하위로 일원화)
    # 1. GET /api/web/stocks/{ticker}/orderbook : 특정 종목의 호가, 현재가, 체결강도 조회
    path('stocks/<str:ticker>/orderbook', OrderBookView.as_view(), name='stock-orderbook'),
    
    # 2. GET /api/web/account/balance : 현재 사용자의 예수금(구매 가능 금액) 및 계좌 상태 조회
    path('account/balance', AccountBalanceView.as_view(), name='account-balance'),
    
    # 3. GET /api/web/account/holdings/{ticker} : 특정 종목의 보유 수량, 평단가 조회
    path('account/holdings/<str:ticker>', AccountHoldingsView.as_view(), name='account-holdings'),
    
    # 4. POST /api/web/orders : 매수/매도 주문 접수
    path('orders', OrderView.as_view(), name='place-order'),
    
    # 5. GET /api/web/orders/pending : 체결 대기 중인 미체결 주문 리스트 조회
    path('orders/pending', PendingOrdersView.as_view(), name='pending-orders'),
    
    # 6. DELETE /api/web/orders/{order_id} : 한국투자증권 API를 호출하여 특정 미체결 주문 취소
    path('orders/<str:order_id>', CancelOrderView.as_view(), name='cancel-order'),
]