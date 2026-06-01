import requests
import urllib.parse
import os
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.core.cache import cache
from django.contrib.auth.hashers import check_password

from .services.stock_chart_data import StockChartDataProvider
from .services.stock_list_data import StockListDataProvider
from .services.stock_news_data import StockNewsDataProvider
from .services.stock_averaging_data import StockAveragingDataProvider
from .services.web_stock_report import WebStockReport
from .services.web_detail_report import WebDetailReport
from .services.web_order import WebOrder
from .services.stock_search import search_stocks, lookup_company_name
from .models import User, KisAccount, TelegramLink, LinkToken
from datetime import timedelta
from django.utils import timezone

from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi


def _looks_like_security_code(s: str) -> bool:
    """
    이미 형태가 종목코드/증권코드처럼 보이면 외부 검색 skip.
    - 일반 주식: 6자리 숫자 (예: 005930)
    - ETN/ETF 변종: 0/Q 로 시작, 영문 1자(X 등) 포함 가능 (예: 0197X0, Q760027)
    - ELW 등: 6~8자, 영숫자 혼합
    네이버 자동완성 + pykrx 가 이 변종 코드를 대부분 인식 못하므로,
    형태로 판별되면 그대로 반환해서 한투 API 가 직접 처리하게 둠.
    """
    if not s:
        return False
    if s.isdigit() and len(s) == 6:           # 일반 주식
        return True
    if 6 <= len(s) <= 8 and s.isalnum() and any(c.isdigit() for c in s):
        # 영숫자 혼합 6~8자 — ETN/ETF/ELW 변종 코드 패턴
        # 첫 글자가 0 또는 Q 이거나 X 가 포함되면 거의 확실히 종목 코드
        if s[0] in "0Q" or "X" in s.upper():
            return True
    return False


def resolve_symbol(symbol_or_name: str) -> str:
    """
    이름(예: 삼성전자) → 종목코드(005930), 종목코드는 그대로.
    ETN/ETF 변종(0197X0 등)도 그대로 반환.

    검색은 네이버 금융 검색 (stock_search.search_stocks) 단일 경로로 위임.
    이전 fallback: ac.finance.naver.com 자동완성 (NXDOMAIN) + pykrx 전종목 스캔
    (KRX 스크래핑 단절) — 둘 다 실패가 기본값이라 제거.
    """
    s = symbol_or_name
    if s.isdigit():
        return s
    if s.upper() == "ALL":
        return "ALL"
    if _looks_like_security_code(s.upper()):
        return s.upper()

    target = s.replace(" ", "").upper()
    try:
        results = search_stocks(s, limit=20)
    except Exception as e:
        return f"ERROR: search_stocks raised {type(e).__name__}: {e}"

    if not results:
        return f"ERROR: no match for '{s}'"

    # 정확 일치 우선 (공백 무시 + 대문자 비교)
    for r in results:
        if r["name"].replace(" ", "").upper() == target:
            return r["ticker"]

    # 부분 일치 첫 번째
    return results[0]["ticker"]


def _kst_now():
    """현재 한국 시각 (Asia/Seoul). Django TIME_ZONE 도 Asia/Seoul 이라
    naive datetime 만 필요."""
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=9)))


def _market_status_and_ttl(chart_range: str) -> tuple[str, int]:
    """
    range='1d' 차트의 시장 상태 + 캐시 TTL 결정.

    Returns:
        (status, ttl_seconds)
        status: 'open' (정규장 중) | 'closed' (장 마감 후 / 주말 / 공휴일)

    정규장 중: TTL 60초 (실시간 갱신).
    장외: 다음 정규장 시작 (09:00 KST 평일) 까지의 초 — 그 동안 KIS 호출 없음.
    """
    if chart_range != '1d':
        # 일봉은 일/주/년 단위라 5분 캐시면 충분 (변경 빈도 낮음)
        return ('open', 300)

    from datetime import timedelta
    now = _kst_now()
    weekday = now.weekday()  # Mon=0 .. Sun=6
    open_t = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if weekday < 5 and open_t <= now <= close_t:
        # 정규장 중
        return ('open', 60)

    # 다음 정규장 시작 시각 계산
    nxt = open_t
    if now > close_t and weekday < 5:
        # 평일 장 마감 후 → 다음 날 09:00
        nxt = open_t + timedelta(days=1)
    elif now < open_t and weekday < 5:
        # 평일 장 시작 전 → 오늘 09:00 (그대로)
        pass
    # 주말 또는 위에서 다음날이 토/일인 경우 → 월요일까지 점프
    while nxt.weekday() >= 5:
        nxt = nxt + timedelta(days=1)

    ttl = max(60, int((nxt - now).total_seconds()))
    return ('closed', ttl)


class StockChartView(APIView):
    """
    Web 01 - 종목 차트 API
    URL: /api/web/stocks/{symbol}/chart
    Param: range(1d|1m|3m|1y), type(candlestick|line|technical|volume)
    symbol: 이름, 종목 코드 둘 다 가능
    """

    # noinspection PyMethodMayBeStatic
    def get(self, request, symbol):
        chart_range = request.query_params.get('range', '3m')
        chart_type = request.query_params.get('type', 'candlestick')
        # 분봉 간격 (range='1d' 일 때만 유효). 허용: 1, 5, 10, 15, 30, 60.
        # 미지정 시 None → get_minute_chart_data 의 default (10분) 사용.
        interval_raw = request.query_params.get('interval')
        interval: int | None = None
        if interval_raw:
            try:
                v = int(interval_raw)
                if v in (1, 5, 10, 15, 30, 60):
                    interval = v
            except (TypeError, ValueError):
                interval = None

        original_symbol = symbol
        symbol = resolve_symbol(symbol)

        if symbol.startswith("ERROR:"):
            return Response(
                {"error": f"'{original_symbol}' 종목 검색 실패. 상세 사유: {symbol}"},
                status=status.HTTP_404_NOT_FOUND
            )

        if not _looks_like_security_code(symbol):
            return Response(
                {"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND
            )

        # interval 도 cache key 에 포함 — 같은 (symbol, 1d, candlestick) 도
        # 분봉 간격이 다르면 다른 응답.
        cache_key = f"chart_{symbol}_{chart_range}_{chart_type}_{interval or 'auto'}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return Response(cached_data)

        try:
            provider = StockChartDataProvider()
            result = provider.get_chart_api(
                symbol, range=chart_range, type=chart_type, interval=interval
            )

            if result.get('plotly') is None:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)

            # 시장 상태 + 적응형 TTL — 장 마감 후엔 다음 정규장 시작까지 캐시
            market_status, ttl = _market_status_and_ttl(chart_range)
            if chart_range == '1d':
                result['market_status'] = market_status  # 'open' | 'closed'
                result['market_close_kst'] = _kst_now().replace(
                    hour=15, minute=30, second=0, microsecond=0
                ).isoformat()

            cache.set(cache_key, result, ttl)

            return Response(result)

        # noinspection PyBroadException
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockListView(APIView):
    """
    Web 01 - 종목 리스트 API
    URL: /api/web/stocks/list
    Param: market(ALL|KOSPI|KOSDAQ), sort(change_rate|price|volume), order(desc|asc)
    """

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def get(self, request):
        _ = request.query_params.get('market', 'ALL')
        sort_by = request.query_params.get('sort', 'change_rate')
        order = request.query_params.get('order', 'desc')

        try:
            provider = StockListDataProvider()
            result = provider.get_sorted_market_stocks(
                sort_by=sort_by,
                order=order,
                limit=3000
            )
            return Response(result)

        # noinspection PyBroadException
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockSearchView(APIView):
    """
    Web - 종목 검색 API
    URL: /api/web/stocks/search?q={query}&limit={20}

    네이버 금융 검색을 통해 전체 KOSPI/KOSDAQ 종목 대상 부분 일치 검색.
    - 한글 부분 입력 ("삼성") → "삼성전자", "삼성SDI", "삼성중공업" 등
    - 6자리 코드 ("035720") → 해당 종목
    - 결과는 1시간 캐시 (네이버 페이지 자체가 비교적 안정적).
    """

    # noinspection PyMethodMayBeStatic
    def get(self, request):
        q = (request.query_params.get('q') or request.query_params.get('query') or '').strip()
        try:
            limit = int(request.query_params.get('limit', 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(50, limit))

        if not q:
            return Response({"query": "", "count": 0, "stocks": []})

        cache_key = f"stock_search:{q.lower()}:{limit}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        stocks = search_stocks(q, limit=limit)
        result = {"query": q, "count": len(stocks), "stocks": stocks}
        cache.set(cache_key, result, timeout=3600)  # 1h
        return Response(result)


class StockHoldingView(APIView):
    """
    Web 01 - 보유 종목 리스트 API (계좌 연동 시 사용 가능)
    URL: /api/web/stocks/holdings
    Param: sort(eval_amount|profit_rate|name), order(desc|asc)
    """

    # noinspection PyMethodMayBeStatic
    def get(self, request):
        sort_by = request.query_params.get('sort', 'eval_amount')
        order = request.query_params.get('order', 'desc')

        try:
            # HantuStock 내부적 초기화
            provider = StockListDataProvider()
            result = provider.get_holding_stocks(sort_by=sort_by, order=order)

            # 에러(계좌 미연동 등) 처리
            if "error" in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)

            return Response(result)

        # noinspection PyBroadException
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class BaseStockInfoView(APIView):
    # 부모 클래스(공통 기능)
    # noinspection PyMethodMayBeStatic
    def get_company_name(self, symbol):
        if not _looks_like_security_code(symbol):
            return symbol
            
        try:
            from .services.HantuStock import HantuStock
            h = HantuStock()
            price_info = h.get_stock_price(symbol)
            if "error" not in price_info and price_info.get("name"):
                return price_info["name"]
        except Exception:
            pass
            
        return symbol


class StockNewsView(BaseStockInfoView):
    """
    Web 02 - 종목 뉴스 API
    URL: /api/web/stocks/{symbol}/news?cursor={optional}&limit=20
    """

    # noinspection PyMethodMayBeStatic
    def get(self, request, symbol):
        cursor = request.query_params.get('cursor', '1')
        limit = int(request.query_params.get('limit', 20))
        page = int(cursor) if cursor and cursor.isdigit() else 1

        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not _looks_like_security_code(symbol):
            return Response({"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        cache_key = f"news_{symbol}_{page}"
        cached_data = cache.get(cache_key)
        if cached_data: return Response(cached_data)

        try:
            company_name = self.get_company_name(symbol)
            provider = StockNewsDataProvider()
            result = provider.get_news(symbol, company_name, page=page, limit=limit)
            
            if "error" in result:
                return Response({"error": result["error"]}, status=status.HTTP_400_BAD_REQUEST)
                
            next_cursor = str(page + 1) if result.get('items') else None

            response_data = {
                "items": result.get('items', []),
                "next_cursor": next_cursor
            }

            if response_data["items"]:
                cache.set(cache_key, response_data, 600)

            return Response(response_data)
        # noinspection PyBroadException
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockCommunityView(BaseStockInfoView):
    """
    Web 02 - 커뮤니티 API (폴링 및 무한 스크롤)
    URL: /api/web/stocks/{symbol}/community?cursor={optional}&limit=20
    """

    # noinspection PyMethodMayBeStatic
    def get(self, request, symbol):
        cursor = request.query_params.get('cursor', '1')
        limit = int(request.query_params.get('limit', 20))
        page = int(cursor) if cursor and cursor.isdigit() else 1

        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not _looks_like_security_code(symbol):
            return Response({"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        try:
            company_name = self.get_company_name(symbol)
            provider = StockNewsDataProvider()
            result = provider.get_community(symbol, company_name, page=page, limit=limit)
            
            if "error" in result:
                return Response({"error": result["error"]}, status=status.HTTP_400_BAD_REQUEST)
                
            next_cursor = str(page + 1) if result.get('items') else None

            return Response({
                "ai_summary": result.get("ai_summary"),
                "items": result.get("items", []),
                "next_cursor": next_cursor
            })
        # noinspection PyBroadException
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockCommunityLatestView(BaseStockInfoView):
    """
    Web 02 - 새 글 확인용 API (상단 갱신)
    URL: /api/web/stocks/{symbol}/community/latest?since={timestamp}
    """

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def get(self, request, symbol):
        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not _looks_like_security_code(symbol):
            return Response({"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        try:
            company_name = self.get_company_name(symbol)
            provider = StockNewsDataProvider()

            # 최신 글 5개만 조회
            result = provider.get_community(symbol, company_name, page=1, limit=5)
            
            if "error" in result:
                return Response({"error": result["error"]}, status=status.HTTP_400_BAD_REQUEST)
                
            return Response({
                "has_new": True,
                "items": result.get("items", [])
            })
        # noinspection PyBroadException
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AveragingHoldingView(APIView):
    """
    Web 03 - 보유 종목 정보 조회 API
    GET /api/web/averaging/holding/<str:symbol>
    """

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def get(self, request, symbol):
        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not _looks_like_security_code(symbol):
            return Response({"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        cache_key = f"averaging_holding_{symbol}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return Response(cached_data)

        provider = StockAveragingDataProvider()
        result = provider.get_holding_info(symbol)

        if result.get("error"):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        cache.set(cache_key, result, 30)
        return Response(result)


class AveragingCalculateQuantityView(APIView):
    """
    Web 03 - 물타기 계산 API (수량 기준)
    POST /api/web/averaging/calculate/quantity
    """

    # noinspection PyMethodMayBeStatic
    def post(self, request):
        symbol = request.data.get('symbol')
        price = request.data.get('additional_price')
        quantity = request.data.get('additional_quantity')

        if not all([symbol, price, quantity]):
            return Response({"error": "INVALID_INPUT", "message": "모든 필드를 입력해주세요."}, status=status.HTTP_400_BAD_REQUEST)

        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": "INVALID_INPUT", "message": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not _looks_like_security_code(symbol):
            return Response({"error": "INVALID_INPUT", "message": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        try:
            price = float(price)
            quantity = int(quantity)
            if price <= 0 or quantity <= 0:
                raise ValueError
        # noinspection PyBroadException
        except ValueError:
            return Response(
                {"error": "INVALID_INPUT", "message": "0보다 큰 올바른 숫자를 입력해주세요."},
                status=status.HTTP_400_BAD_REQUEST
            )
        provider = StockAveragingDataProvider()
        result = provider.calculate_by_quantity(symbol, price, quantity)
        if result.get("error"):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result)


class AveragingCalculateAmountView(APIView):
    """
    Web 03 - 물타기 계산 API (금액 기준)
    POST /api/web/averaging/calculate/amount
    """

    # noinspection PyMethodMayBeStatic
    def post(self, request):
        symbol = request.data.get('symbol')
        amount = request.data.get('investment_amount')
        price = request.data.get('purchase_price')

        if not all([symbol, amount, price]):
            return Response({"error": "INVALID_INPUT", "message": "모든 필드를 입력해주세요."}, status=status.HTTP_400_BAD_REQUEST)
            
        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": "INVALID_INPUT", "message": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not _looks_like_security_code(symbol):
            return Response({"error": "INVALID_INPUT", "message": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        try:
            amount = float(amount)
            price = float(price)
            if amount <= 0 or price <= 0:
                raise ValueError
        # noinspection PyBroadException
        except ValueError:
            return Response(
                {"error": "INVALID_INPUT", "message": "0보다 큰 올바른 숫자를 입력해주세요."},
                status=status.HTTP_400_BAD_REQUEST
            )

        provider = StockAveragingDataProvider()
        result = provider.calculate_by_amount(symbol, amount, price)

        if result.get("error"):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result)


class AveragingSaveView(APIView):
    """
    Web 03 - 물타기 계산 결과 저장 API
    POST /api/web/averaging/save
    """

    # noinspection PyMethodMayBeStatic
    def post(self, request):
        symbol = request.data.get('symbol')
        calc_mode = request.data.get('calculation_mode')

        if not symbol or not calc_mode:
            return Response(
                {"error": "INVALID_INPUT", "message": "종목 코드와 계산 모드는 필수입니다."},
                status=status.HTTP_400_BAD_REQUEST
            )

        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": "INVALID_INPUT", "message": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not _looks_like_security_code(symbol):
            return Response({"error": "INVALID_INPUT", "message": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        provider = StockAveragingDataProvider()
        result = provider.save_calculation(symbol, request.data, calc_mode)

        if result.get("error"):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        # 저장 성공 시 캐시 무효화 (기본 limit 10 기준)
        cache.delete(f"averaging_history_{symbol}_10")

        return Response(result, status=status.HTTP_201_CREATED)


class AveragingHistoryView(APIView):
    """
    Web 03 - 계산 히스토리 조회 API
    GET /api/web/averaging/history/<str:symbol>?limit=10
    """

    # noinspection PyMethodMayBeStatic
    def get(self, request, symbol):
        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not _looks_like_security_code(symbol):
            return Response({"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        # 쿼리 파라미터에서 limit 추출 (기본 10개)
        try:
            limit = int(request.query_params.get('limit', 10))
        # noinspection PyBroadException
        except ValueError:
            limit = 10

        cache_key = f"averaging_history_{symbol}_{limit}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return Response(cached_data)
        
        provider = StockAveragingDataProvider()
        result = provider.get_calculation_history(symbol, limit)
        
        if result.get("error"):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
            
        cache.set(cache_key, result, 300)
        return Response(result)


class StockDetailReportView(APIView):
    """
    Web04 - 종목 상세 분석 리포트 API
    GET /api/web/stocks/<str:symbol>/detail
    """

    # noinspection PyMethodMayBeStatic
    def get(self, request, symbol):
        user_id = request.query_params.get('user_id', 'default_user')
        period = request.query_params.get('period', '1m')
        demo = request.query_params.get('demo', '').lower() in ('1', 'true', 'yes')

        original_symbol = symbol
        symbol = resolve_symbol(symbol)

        if symbol.startswith("ERROR:"):
            return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not symbol.isdigit() and symbol != "ALL":
            return Response({"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        # 운영 캐시 (demo 모드는 캐시 우회 — 시연마다 fresh)
        cache_key = f"web04_detail_{symbol}_{user_id}_{period}"
        if not demo:
            cached_data = cache.get(cache_key)
            if cached_data:
                return Response(cached_data)

        try:
            detail_report_service = WebDetailReport()

            # demo 모드: 보유 종목 + 평가손익 기준 가상 거래 생성 후 주입 (운영 영향 X)
            injected = None
            if demo:
                from .services.demo_data import build_demo_transactions
                try:
                    holdings = detail_report_service.hantu.get_holding_stock_detail()
                except Exception:
                    holdings = []
                # period 별로 매수 날짜 분포를 다르게 — 1m/3m/1y 탭이 의미 있게 다름
                injected = build_demo_transactions(holdings, period=period)
                if symbol != "ALL":
                    injected = [t for t in injected if t["pdno"] == symbol]

            # 서비스 계층 호출
            result = detail_report_service.get_detail_report(
                scope=symbol,
                period=period,
                transactions=injected,
            )

            # 자동 demo fallback: ?demo= 없이 들어왔는데 실 거래 0건이고 보유종목 있으면
            # 시연용 가상 거래로 자동 재생성. KIS 모의계좌엔 무상입고만 있고 매매기록 없는
            # 경우가 흔하고, 빈 응답은 UX 상 의미 없음. demo_mode 플래그로 사실 명시.
            used_fallback = False
            if not demo and result.get("summary_metrics", {}).get("total_trades", 0) == 0:
                from .services.demo_data import build_demo_transactions
                try:
                    holdings = detail_report_service.hantu.get_holding_stock_detail()
                except Exception:
                    holdings = []
                if holdings:
                    fallback_tx = build_demo_transactions(holdings, period=period)
                    if symbol != "ALL":
                        fallback_tx = [t for t in fallback_tx if t["pdno"] == symbol]
                    if fallback_tx:
                        result = detail_report_service.get_detail_report(
                            scope=symbol,
                            period=period,
                            transactions=fallback_tx,
                        )
                        used_fallback = True

            if demo or used_fallback:
                result["demo_mode"] = True
                result["demo_note"] = (
                    "?demo=1 — 보유 종목 기반 가상 거래로 시연된 응답 (실 거래내역 X)"
                    if demo
                    else "실 거래내역이 없어 보유 종목 기반 시연 데이터로 자동 생성된 응답입니다"
                )

            if "error" in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)

            # demo / fallback 모두 캐시 우회 (시연 항상 fresh)
            if not demo and not used_fallback:
                cache.set(cache_key, result, 3600)
            return Response(result, status=status.HTTP_200_OK)

        # noinspection PyBroadException
        except Exception as e:
            print(f"[API 500 에러 원인 추적] {e}")

            error_response = {
                "error": str(e),
                "message": "현재 데이터 제공 서버 점검으로 인해 상세 리포트를 생성할 수 없습니다."
            }
            return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockReportView(APIView):
    """
    Web05 - 종목 리포트 조회 API (5개 섹션 포함)
    GET /api/web/stocks/<str:symbol>/report
    """

    # noinspection PyMethodMayBeStatic
    def get(self, request, symbol):
        try:
            user_id = request.query_params.get('user_id', 'default_user')
            # 투자성향 (1=안정형 ~ 5=공격투자형). default=3 (위험중립형) — 미설정 사용자.
            profile_raw = request.query_params.get('profile_level')
            try:
                profile_level = int(profile_raw) if profile_raw else 3
            except (TypeError, ValueError):
                profile_level = 3
            if profile_level not in (1, 2, 3, 4, 5):
                profile_level = 3

            original_symbol = symbol
            symbol = resolve_symbol(symbol)

            if symbol.startswith("ERROR:"):
                return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

            if not _looks_like_security_code(symbol):
                return Response({"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

            web_report = WebStockReport()

            # 1. 프론트엔드에서 넘어온 값을 우선적으로 받는다.
            company_name = request.query_params.get('company_name')

            # 2. 값이 아예 없거나, 실수로 종목 코드가 이름 자리에 들어왔을 때만 API로 검색을 시도한다.
            if not company_name or company_name == symbol or company_name.isdigit():
                info = web_report.chart_provider.get_stock_info(symbol)
                company_name = info.get('company_name', original_symbol if not original_symbol.isdigit() else symbol)

            # 캐시 키에 profile_level 포함 — 같은 종목도 성향별로 다른 리포트.
            cache_key = f"web05_report_{symbol}_{user_id}_p{profile_level}"

            cached_data = cache.get(cache_key)
            if cached_data:
               return Response(cached_data)

            result = web_report.get_report(
                symbol, company_name, user_id, profile_level=profile_level
            )

            if "error" in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)

            # 리포트는 LLM 호출 비용·시간이 크고 일 단위 데이터라
            # 4시간 캐시 (재방문 시 즉시 응답).
            cache.set(cache_key, result, 14400)
            return Response(result)

        # noinspection PyBroadException
        except Exception as e:
            print(f"[API 500 에러 원인 추적 - StockReportView] {e}")

            error_response = {
                "error": str(e),
                "message": "리포트 생성 중 서버 내부 오류가 발생했습니다."
            }
            return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UserSignUpView(APIView):
    """
    사용자 회원가입 API
    POST /api/web/users/signup

    필수: login_id, name, birthdate, password
    선택: phone (부가 정보, unique 아님)
    """
    def post(self, request):
        login_id = (request.data.get('login_id') or '').strip()
        name = (request.data.get('name') or '').strip()
        birthdate = (request.data.get('birthdate') or '').strip()
        password = request.data.get('password') or ''
        phone = (request.data.get('phone') or '').strip() or None

        if not all([login_id, name, birthdate, password]):
            return Response(
                {"error": "필수 정보(login_id, name, birthdate, password)를 모두 입력해주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if User.objects.filter(login_id=login_id).exists():
            return Response(
                {"error": "이미 사용 중인 아이디입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User(login_id=login_id, name=name, birthdate=birthdate, phone=phone)
        user.set_password(password)
        user.save()

        return Response({
            "message": "회원가입이 성공적으로 완료되었습니다.",
            "user_id": user.user_id,
            "login_id": user.login_id,
        }, status=status.HTTP_201_CREATED)


class UserSignInView(APIView):
    """
    사용자 로그인 API
    POST /api/web/users/signin

    필수: login_id, password
    (구버전 호환을 위해 'phone' 필드로 들어와도 login_id 로 시도)
    """
    def post(self, request):
        login_id = (
            request.data.get('login_id')
            or request.data.get('phone')   # 구버전 클라이언트 호환
            or ''
        ).strip()
        password = request.data.get('password') or ''

        if not all([login_id, password]):
            return Response(
                {"error": "아이디와 비밀번호를 모두 입력해주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(login_id=login_id)
            if not user.check_password(password):
                return Response(
                    {"error": "비밀번호가 일치하지 않습니다."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            return Response({
                "message": "로그인에 성공했습니다.",
                "user_id": user.user_id,
                "login_id": user.login_id,
                "name": user.name,
            }, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)


class KisAccountConnectView(APIView):
    """
    KIS 계좌 연결 / 해제 API.

    POST /api/web/kis/connect
      body: user_id (필수), app_key, app_secret, account_number, env(default vps)
      - app_key/app_secret/account_number 가 모두 제공되면 사용자 키로 연결
      - 셋 중 하나라도 비어 있으면 .env 의 시스템 키로 fallback (관리자/데모용)

    DELETE /api/web/kis/connect
      body: user_id (필수)
      - 해당 사용자의 KisAccount 삭제 (연동 해제)
    """

    def post(self, request):
        user_id = request.data.get('user_id')
        if not user_id:
            return Response({"error": "사용자 정보가 필요합니다."}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            user = User.objects.get(user_id=user_id)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)

        app_key = (request.data.get('app_key') or '').strip()
        app_secret_key = (request.data.get('app_secret') or '').strip()
        account_number = (request.data.get('account_number') or '').strip()
        env = (request.data.get('env') or 'vps').strip() or 'vps'

        source = "user"
        # 사용자 키 셋 중 하나라도 비어있으면 .env fallback
        if not all([app_key, app_secret_key, account_number]):
            app_key = os.environ.get('KIS_APP_KEY') or app_key
            app_secret_key = os.environ.get('KIS_APP_SECRET') or app_secret_key
            account_id = os.environ.get('KIS_ACCOUNT_ID')
            account_suffix = os.environ.get('KIS_ACCOUNT_SUFFIX')
            env = os.environ.get('KIS_ENV', env)
            if account_id and account_suffix:
                account_number = f"{account_id}-{account_suffix}"
            source = "system_env"

        if not all([app_key, app_secret_key, account_number]):
            return Response(
                {"error": "KIS 키/계좌번호 정보가 부족합니다 (.env 도 미설정)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        kis_account, created = KisAccount.objects.update_or_create(
            user=user,
            defaults={
                'account_number': account_number,
                'env': env,
            }
        )
        kis_account.set_app_key(app_key)
        kis_account.set_app_secret_key(app_secret_key)
        kis_account.save()

        return Response({
            "message": "KIS 계좌가 연결되었습니다." if created else "KIS 계좌 정보가 업데이트되었습니다.",
            "kis_connected": True,
            "kis_account_number": account_number,
            "kis_env": env,
            "source": source,  # 'user' or 'system_env' — 운영자가 어느 키로 저장됐는지 확인용
        }, status=status.HTTP_200_OK)

    def delete(self, request):
        user_id = request.data.get('user_id')
        if not user_id:
            return Response({"error": "사용자 정보가 필요합니다."}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            user = User.objects.get(user_id=user_id)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)

        deleted_count, _ = KisAccount.objects.filter(user=user).delete()
        return Response({
            "message": "KIS 계좌 연동이 해제되었습니다." if deleted_count else "이미 미연동 상태입니다.",
            "kis_connected": False,
        }, status=status.HTTP_200_OK)


class UserDetailView(APIView):
    """
    사용자 정보 조회, 수정, 삭제 API
    GET, PATCH, DELETE /api/web/users/{user_id}/
    """

    def get(self, request, user_id):
        try:
            user = User.objects.get(user_id=user_id)
            kis = KisAccount.objects.filter(user=user).first()
            tg = TelegramLink.objects.filter(user=user).first()
            data = {
                "user_id": user.user_id,
                "login_id": user.login_id,
                "phone": user.phone,
                "name": user.name,
                "birthdate": user.birthdate,
                "notify_morning": user.notify_morning,
                "notify_evening": user.notify_evening,
                "notify_event": user.notify_event,
                "kis_connected": bool(kis),
                "kis_account_number": kis.account_number if kis else None,
                "kis_env": kis.env if kis else None,
                "telegram_connected": bool(tg),
                "telegram_username": tg.telegram_username if tg else None,
                "created_at": user.created_at,
                "updated_at": user.updated_at,
            }
            return Response(data)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)

    def patch(self, request, user_id):
        try:
            user = User.objects.get(user_id=user_id)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)

        # 변경 가능한 필드들
        updatable_fields = ['name', 'birthdate', 'notify_morning', 'notify_evening', 'notify_event']

        for field in updatable_fields:
            if field in request.data:
                setattr(user, field, request.data[field])

        user.save()

        return Response({"message": "사용자 정보가 성공적으로 업데이트되었습니다."})

    def delete(self, request, user_id):
        try:
            user = User.objects.get(user_id=user_id)
            user.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)


'''
class StockFavoriteToggleView(APIView):
    """
    Web05 - 관심 종목 추가/해제 API
    POST /api/web/stocks/<str:symbol>/favorite
    """

    def post(self, request, symbol):
        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not _looks_like_security_code(symbol):
            return Response({"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        user_id = request.data.get('user_id', 'default_user')
        company_name = request.data.get('company_name', original_symbol if not original_symbol.isdigit() else symbol)
        action = request.data.get('action')  # "add" or "remove"
        web_report = WebStockReport()

        if action == "add":
            result = web_report.add_favorite(user_id, symbol, company_name)
        elif action == "remove":
            result = web_report.remove_favorite(user_id, symbol)
        else:
            return Response({"error": "action 파라미터는 'add' 또는 'remove'"}, status=status.HTTP_400_BAD_REQUEST)
        cache.delete(f"web05_report_{symbol}_{user_id}")
        return Response(result)


class StockFavoriteListView(APIView):
    """
    Web05 - 관심 종목 목록 조회 API
    GET /api/web/favorites
    """

    def get(self, request):
        user_id = request.query_params.get('user_id', 'default_user')
        
        # 24시간 지난 계좌 삭제 (자동 로그아웃 처리)
        from .models import KisAccount
        expired_accounts = [account for account in KisAccount.objects.all() if account.is_expired()]
        for account in expired_accounts:
            account.delete()

        web_report = WebStockReport()
        result = web_report.get_favorites(user_id)
        return Response(result)
'''

# Web_06: 실시간 주식 매매 관련 API

class OrderBookView(APIView):
    """
    GET /api/web/stocks/{ticker}/orderbook
    특정 종목의 호가(매도/매수 10호가 등), 현재가, 체결강도 조회
    """
    def get(self, request, ticker):
        try:
            service = WebOrder()
            order_book = service.get_order_book(ticker)
            if "error" in order_book:
                return Response(order_book, status=status.HTTP_400_BAD_REQUEST)
            
            return Response(order_book)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AccountBalanceView(APIView):
    """
    GET /api/web/account/balance
    현재 사용자의 예수금(구매 가능 금액) 및 계좌 상태 조회
    """
    def get(self, request):
        try:
            service = WebOrder()
            # get_account_info는 여러 정보를 반환하므로 필요한 예수금만 추출
            account_info = service.get_account_info(symbol="005930") # 아무 종목이나 넣어도 예수금은 동일
            if "error" in account_info:
                 return Response(account_info, status=status.HTTP_400_BAD_REQUEST)

            return Response({"available_cash": account_info.get("available_cash", 0)})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AccountHoldingsView(APIView):
    """
    GET /api/web/account/holdings/{ticker}
    특정 종목의 보유 수량, 평단가 조회
    """
    def get(self, request, ticker):
        try:
            service = WebOrder()
            holding_info = service.get_account_info(ticker)
            if "error" in holding_info:
                 return Response(holding_info, status=status.HTTP_400_BAD_REQUEST)

            return Response({
                "ticker": ticker,
                "holding_quantity": holding_info.get("holding_quantity", 0),
                "average_price": holding_info.get("average_price", 0.0),
                "current_price": holding_info.get("current_price", 0)
            })
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class OrderView(APIView):
    """
    POST /api/web/orders
    매수/매도 주문 접수
    """
    def post(self, request):
        try:
            ticker = request.data.get('ticker')
            side = request.data.get('side')
            order_type = request.data.get('order_type')
            price = int(request.data.get('price', 0))
            quantity = int(request.data.get('quantity', 0))

            if not all([ticker, side, order_type, quantity]):
                return Response({"error": "필수 파라미터가 누락되었습니다."}, status=status.HTTP_400_BAD_REQUEST)

            service = WebOrder()
            result = service.place_order(
                symbol=ticker,
                side=side,
                order_type=order_type,
                price=price,
                quantity=quantity
            )
            
            if not result.get('success'):
                return Response(result, status=status.HTTP_400_BAD_REQUEST)
                
            return Response(result)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PendingOrdersView(APIView):
    """
    GET /api/web/orders/pending
    체결 대기 중인 미체결 주문 리스트 조회
    """
    def get(self, request):
        try:
            service = WebOrder()
            pending_orders = service.get_pending_orders()
            return Response(pending_orders)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CancelOrderView(APIView):
    """
    DELETE /api/web/orders/{order_id}
    특정 미체결 주문 취소
    """
    def delete(self, request, order_id):
        try:
            ticker = request.data.get('ticker')
            quantity = int(request.data.get('quantity', 0))

            if not all([ticker, quantity]):
                 return Response({"error": "필수 파라미터(ticker, quantity)가 누락되었습니다."}, status=status.HTTP_400_BAD_REQUEST)

            service = WebOrder()
            result = service.cancel_order(
                order_id=order_id,
                symbol=ticker,
                quantity=quantity
            )
            
            if not result.get('success'):
                return Response(result, status=status.HTTP_400_BAD_REQUEST)

            return Response(result)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ─── Telegram 연동 (MATE 비서) ───────────────────────────

class TelegramConnectView(APIView):
    """
    POST /api/web/users/telegram/connect
    Web 마이페이지에서 호출. 1회용 LinkToken 발급 → Deep Link 응답.

    Request:
        { "user_id": "<uuid>" }
    Response 200:
        {
            "deep_link": "https://t.me/<bot_username>?start=<token>",
            "token": "<token>",
            "expires_at": "2026-05-27T08:50:00Z"
        }
    """

    TOKEN_TTL_MINUTES = 10

    def post(self, request):
        user_id = (request.data.get("user_id") or "").strip()
        if not user_id:
            return Response({"error": "user_id 가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(user_id=user_id)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)

        bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip()
        if not bot_username:
            return Response(
                {"error": "서버에 TELEGRAM_BOT_USERNAME 이 설정되지 않았습니다."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        token_obj = LinkToken.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(minutes=self.TOKEN_TTL_MINUTES),
        )

        return Response(
            {
                "deep_link": f"https://t.me/{bot_username}?start={token_obj.token}",
                "token": token_obj.token,
                "expires_at": token_obj.expires_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )


class TelegramStatusView(APIView):
    """
    GET /api/web/users/telegram/status?user_id=<uuid>
    """

    def get(self, request):
        user_id = (request.query_params.get("user_id") or "").strip()
        if not user_id:
            return Response({"error": "user_id 가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(user_id=user_id)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)

        link = TelegramLink.objects.filter(user=user).first()
        if link is None:
            return Response({"linked": False}, status=status.HTTP_200_OK)

        return Response(
            {
                "linked": True,
                "telegram_username": link.telegram_username,
                "linked_at": link.linked_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )


class TelegramUnlinkView(APIView):
    """
    DELETE /api/web/users/telegram/unlink
    Body: { "user_id": "<uuid>" }
    """

    def delete(self, request):
        user_id = (request.data.get("user_id") or "").strip()
        if not user_id:
            return Response({"error": "user_id 가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(user_id=user_id)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)

        deleted, _ = TelegramLink.objects.filter(user=user).delete()
        return Response(
            {"unlinked": bool(deleted)},
            status=status.HTTP_200_OK,
        )


class UserNotifySettingsView(APIView):
    """
    사용자 알림 수신 설정 조회/변경.

    GET  /api/web/users/notify-settings?user_id=<uuid>
        → { "notify_morning": bool, "notify_evening": bool, "notify_event": bool }

    PATCH /api/web/users/notify-settings
        Body: {
            "user_id": "<uuid>",
            "notify_morning": bool,   # 선택
            "notify_evening": bool,   # 선택
            "notify_event":   bool    # 선택
        }
        → 변경된 후의 전체 설정 반환
    """

    ALLOWED_FIELDS = ("notify_morning", "notify_evening", "notify_event")

    def _serialize(self, user: User) -> dict:
        return {
            "notify_morning": user.notify_morning,
            "notify_evening": user.notify_evening,
            "notify_event": user.notify_event,
        }

    def get(self, request):
        user_id = (request.query_params.get("user_id") or "").strip()
        if not user_id:
            return Response({"error": "user_id 가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user = User.objects.get(user_id=user_id)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._serialize(user), status=status.HTTP_200_OK)

    def patch(self, request):
        user_id = (request.data.get("user_id") or "").strip()
        if not user_id:
            return Response({"error": "user_id 가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user = User.objects.get(user_id=user_id)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)

        changed = []
        for field in self.ALLOWED_FIELDS:
            if field in request.data:
                value = request.data.get(field)
                if not isinstance(value, bool):
                    return Response(
                        {"error": f"{field} 는 boolean 이어야 합니다."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                setattr(user, field, value)
                changed.append(field)

        if changed:
            user.save(update_fields=changed)

        return Response(self._serialize(user), status=status.HTTP_200_OK)
