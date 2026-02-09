from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.core.cache import cache

from .services.stock_chart_data import StockChartDataProvider
from .services.stock_list_data import StockListDataProvider
from .services.stock_news_data import StockNewsDataProvider
from .models import Watchlist


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
    * 사용자 데이터가 없어 임시 데이터 반환(관심 종목 리스트, id)
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
                "company_name": item.company_name,
                "current_price": info.get("current_price", 0),
                "change_rate": info.get("change_rate", 0),
                "price_change": info.get("price_change", 0)
            }
            result.append(stock_data)

        return Response(result)

    # 관심 종목 추가 (POST)
    def post(self, request):
        symbol = request.data.get('symbol')
        company_name = request.data.get('company_name')

        if not symbol:
            return Response({"error": "종목 코드가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        if not company_name:
            from .services.stock_list_data import StockListDataProvider
            try:
                company_name = symbol
            except:
                pass

        # DB에 저장
        obj, created = Watchlist.objects.get_or_create(
            symbol=symbol,
            defaults={'company_name': company_name}
        )

        if created:
            return Response({"message": f"{company_name} 추가 완료", "created": True}, status=status.HTTP_201_CREATED)
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