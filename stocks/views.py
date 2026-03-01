from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.core.cache import cache

from .services.stock_chart_data import StockChartDataProvider
from .services.stock_list_data import StockListDataProvider
from .services.stock_news_data import StockNewsDataProvider
from .services.stock_averaging_data import StockAveragingDataProvider
from .models import Watchlist
from .services.web_stock_report import WebStockReport
from .services.web_detail_report import WebDetailReport


class StockChartView(APIView):
    """
    Web 01 - 종목 차트 API
    URL: /api/web/stocks/{symbol}/chart
    Param: range(1d|1m|3m|1y), type(candlestick|line|technical|volume)
    symbol: 이름, 종목 코드 둘 다 가능
    """

    def get(self, request, symbol):
        chart_range = request.query_params.get('range', '3m')
        chart_type = request.query_params.get('type', 'candlestick')

        if not symbol.isdigit():
            list_provider = StockListDataProvider()
            found_code = list_provider.find_ticker_by_name(symbol)

            if found_code:
                symbol = found_code
            else:
                return Response(
                    {"error": f"'{symbol}' 종목을 찾을 수 없습니다."},
                    status=status.HTTP_404_NOT_FOUND
                )

        cache_key = f"chart_{symbol}_{chart_range}_{chart_type}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return Response(cached_data)

        try:
            provider = StockChartDataProvider()
            result = provider.get_chart_api(symbol, range=chart_range, type=chart_type)

            if result.get('plotly') is None:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)

            ttl = 60 if chart_range == '1d' else 300
            cache.set(cache_key, result, ttl)

            return Response(result)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockListView(APIView):
    """
    Web 01 - 종목 리스트 API
    URL: /api/web/stocks/list
    Param: market(ALL|KOSPI|KOSDAQ), sort(change_rate|price|volume), order(desc|asc)
    """

    def get(self, request):
        market = request.query_params.get('market', 'ALL')
        sort_by = request.query_params.get('sort', 'change_rate')
        order = request.query_params.get('order', 'desc')

        try:
            provider = StockListDataProvider()
            result = provider.get_sorted_market_stocks(
                market=market,
                sort_by=sort_by,
                order=order,
                limit=3000
            )
            return Response(result)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockHoldingView(APIView):
    """
    Web 01 - 보유 종목 리스트 API (계좌 연동 시 사용 가능)
    URL: /api/web/stocks/holdings
    Param: sort(eval_amount|profit_rate|name), order(desc|asc)
    """

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

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockWatchlistView(APIView):
    """
    Web 01 - 관심 종목 리스트 API
    URL: /api/web/stocks/watchlist
    * 모델 연동 - 데이터 직접 입력/삭제
    """

    def get(self, request):
        watchlist = Watchlist.objects.all().order_by('-created_at')

        provider = StockChartDataProvider()
        result = []

        # 관심 종목 목록 조회 (GET)
        for item in watchlist:
            info = provider.get_stock_info(item.symbol)

            stock_data = {
                "symbol": item.symbol,
                "current_price": info.get("current_price", 0),
                "change_rate": info.get("change_rate", 0),
                "price_change": info.get("price_change", 0)
            }
            result.append(stock_data)

        return Response(result)

    # 관심 종목 추가 (POST)
    def post(self, request):
        symbol = request.data.get('symbol')

        if not symbol:
            return Response({"error": "종목 코드가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        # DB에 저장
        obj, created = Watchlist.objects.get_or_create(
            symbol=symbol,
        )

        if created:
            return Response({"message": f"{symbol} 추가 완료", "created": True}, status=status.HTTP_201_CREATED)
        else:
            return Response({"message": "이미 관심 종목에 있습니다.", "created": False}, status=status.HTTP_200_OK)

    # 관심 종목 삭제 (DELETE)
    def delete(self, request):
        symbol = request.data.get('symbol')

        if not symbol:
            symbol = request.query_params.get('symbol')

        if not symbol:
            return Response({"error": "삭제할 종목 코드가 없습니다."}, status=status.HTTP_400_BAD_REQUEST)

        deleted_count, _ = Watchlist.objects.filter(symbol=symbol).delete()

        if deleted_count > 0:
            return Response({"message": "삭제 완료"}, status=status.HTTP_200_OK)
        else:
            return Response({"error": "목록에 없는 종목입니다."}, status=status.HTTP_404_NOT_FOUND)


class BaseStockInfoView(APIView):
    # 부모 클래스(공통 기능)
    def get_company_name(self, symbol):
        if symbol.isdigit():
            try:
                list_provider = StockListDataProvider()
                all_market_data = list_provider.get_sorted_market_stocks(limit=3000)
                stocks = all_market_data.get('stocks', [])
                found_stock = next((s for s in stocks if s['ticker'] == symbol), None)
                return found_stock['name'] if found_stock else symbol
            except:
                return symbol
        return symbol


class StockNewsView(BaseStockInfoView):
    """
    Web 02 - 종목 뉴스 API
    URL: /api/web/stocks/{symbol}/news?cursor={optional}&limit=20
    """

    def get(self, request, symbol):
        cursor = request.query_params.get('cursor', '1')
        limit = int(request.query_params.get('limit', 20))
        page = int(cursor) if cursor and cursor.isdigit() else 1

        cache_key = f"news_{symbol}_{page}"
        cached_data = cache.get(cache_key)
        if cached_data: return Response(cached_data)

        try:
            company_name = self.get_company_name(symbol)
            provider = StockNewsDataProvider()
            result = provider.get_news(symbol, company_name, page=page, limit=limit)
            next_cursor = str(page + 1) if result.get('items') else None

            response_data = {
                "items": result.get('items', []),
                "next_cursor": next_cursor
            }

            if response_data["items"]:
                cache.set(cache_key, response_data, 600)

            return Response(response_data)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockCommunityView(BaseStockInfoView):
    """
    Web 02 - 커뮤니티 API (폴링 및 무한 스크롤)
    URL: /api/web/stocks/{symbol}/community?cursor={optional}&limit=20
    """

    def get(self, request, symbol):
        cursor = request.query_params.get('cursor', '1')
        limit = int(request.query_params.get('limit', 20))
        page = int(cursor) if cursor and cursor.isdigit() else 1

        try:
            company_name = self.get_company_name(symbol)
            provider = StockNewsDataProvider()
            result = provider.get_community(symbol, company_name, page=page, limit=limit)
            next_cursor = str(page + 1) if result.get('items') else None

            return Response({
                "ai_summary": result.get("ai_summary"),
                "items": result.get("items", []),
                "next_cursor": next_cursor
            })
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockCommunityLatestView(BaseStockInfoView):
    """
    Web 02 - 새 글 확인용 API (상단 갱신)
    URL: /api/web/stocks/{symbol}/community/latest?since={timestamp}
    """

    def get(self, request, symbol):
        since = request.query_params.get('since')

        try:
            company_name = self.get_company_name(symbol)
            provider = StockNewsDataProvider()

            # 최신 글 5개만 조회
            result = provider.get_community(symbol, company_name, page=1, limit=5)
            return Response({
                "has_new": True,
                "items": result.get("items", [])
            })
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AveragingHoldingView(APIView):
    """
    Web 03 - 보유 종목 정보 조회 API
    GET /api/web/averaging/holding/<str:symbol>
    """

    def get(self, request, symbol):
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

    def post(self, request):
        symbol = request.data.get('symbol')
        price = request.data.get('additional_price')
        quantity = request.data.get('additional_quantity')

        if not all([symbol, price, quantity]):
            return Response({"error": "INVALID_INPUT", "message": "모든 필드를 입력해주세요."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            price = float(price)
            quantity = int(quantity)
            if price <= 0 or quantity <= 0:
                raise ValueError
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

    def post(self, request):
        symbol = request.data.get('symbol')
        amount = request.data.get('investment_amount')
        price = request.data.get('purchase_price')

        if not all([symbol, amount, price]):
            return Response({"error": "INVALID_INPUT", "message": "모든 필드를 입력해주세요."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            amount = float(amount)
            price = float(price)
            if amount <= 0 or price <= 0:
                raise ValueError
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

    def post(self, request):
        symbol = request.data.get('symbol')
        calc_mode = request.data.get('calculation_mode')

        if not symbol or not calc_mode:
            return Response(
                {"error": "INVALID_INPUT", "message": "종목 코드와 계산 모드는 필수입니다."},
                status=status.HTTP_400_BAD_REQUEST
            )

        provider = StockAveragingDataProvider()
        # 프론트가 보낸 JSON 구조 그대로 통째로 넘겨서 저장
        # [cite: 450, 451, 452, 453, 454, 455, 456, 457, 458, 459, 460, 461, 462, 463, 464]
        result = provider.save_calculation(symbol, request.data, calc_mode)

        if result.get("error"):
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

        return Response(result, status=status.HTTP_201_CREATED)


class AveragingHistoryView(APIView):
    """
    Web 03 - 계산 히스토리 조회 API
    GET /api/web/averaging/history/<str:symbol>?limit=10
    """

    def get(self, request, symbol):
        # 쿼리 파라미터에서 limit 추출 (기본 10개)
        # [cite: 626]
        try:
            limit = int(request.query_params.get('limit', 10))
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

    def get(self, request, symbol):
        user_id = request.query_params.get('user_id', 'default_user')

        # 상세 리포트 캐싱(1시간 유지)
        cache_key = f"web04_detail_{symbol}_{user_id}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return Response(cached_data)

        try:
            detail_report_service = WebDetailReport()

            # 서비스 계층 호출
            result = detail_report_service.get_detailed_report(symbol, user_id=user_id)

            # 외부 API 점검 등으로 인한 에러 발생 시 처리
            if "error" in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)

            cache.set(cache_key, result, 3600)
            return Response(result, status=status.HTTP_200_OK)

        except Exception as e:
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

    def get(self, request, symbol):
        user_id = request.query_params.get('user_id', 'default_user')
        company_name = request.query_params.get('company_name', symbol)

        # 30분(1800초) 캐싱
        cache_key = f"web05_report_{symbol}_{user_id}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return Response(cached_data)
        web_report = WebStockReport()
        result = web_report.get_report(symbol, company_name, user_id)
        if "error" in result:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        cache.set(cache_key, result, 1800)
        return Response(result)


class StockFavoriteToggleView(APIView):
    """
    Web05 - 관심 종목 추가/해제 API
    POST /api/web/stocks/<str:symbol>/favorite
    """

    def post(self, request, symbol):
        user_id = request.data.get('user_id', 'default_user')
        company_name = request.data.get('company_name', symbol)
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
        web_report = WebStockReport()
        result = web_report.get_favorites(user_id)
        return Response(result)