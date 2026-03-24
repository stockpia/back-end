import requests
import urllib.parse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.core.cache import cache

from .services.stock_chart_data import StockChartDataProvider
from .services.stock_list_data import StockListDataProvider
from .services.stock_news_data import StockNewsDataProvider
from .services.stock_averaging_data import StockAveragingDataProvider
from .services.web_stock_report import WebStockReport
from .services.web_detail_report import WebDetailReport


def resolve_symbol(symbol_or_name: str) -> str:
    """
    이름(예: 삼성전자)이 들어오면 종목코드(005930)로 변환,
    종목코드가 들어오면 그대로 반환.
    """
    if symbol_or_name.isdigit():
        return symbol_or_name

    target = symbol_or_name.replace(" ", "").upper()
    debug_logs = []

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://finance.naver.com/'
    }

    # 1. 네이버 금융 자동완성 API 활용
    url = "https://ac.finance.naver.com/ac"
    params = {
        'q': symbol_or_name,
        'q_enc': 'utf-8',
        'st': '111',
        'r_format': 'json',
        'r_enc': 'utf-8'
    }
    
    try:
        res = requests.get(url, params=params, headers=headers, timeout=5)
        debug_logs.append(f"AC Status: {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            items = data.get('items', [])
            for category in items:
                for item in category:
                    if len(item) >= 2 and item[1].isdigit():
                        return str(item[1])
            debug_logs.append("AC No items found")
    except Exception as e:
        debug_logs.append(f"AC Error: {str(e)}")

    # 2. 네이버 금융 검색 스크래핑 (EUC-KR 인코딩 필수)
    try:
        from bs4 import BeautifulSoup
        encoded_query = urllib.parse.quote(symbol_or_name.encode('euc-kr'))
        search_url = f"https://finance.naver.com/search/search.naver?query={encoded_query}"
        
        res = requests.get(search_url, headers=headers, timeout=5)
        debug_logs.append(f"Search Status: {res.status_code}")
        
        # 검색어가 완벽히 일치하여 바로 종목 페이지로 리다이렉트 된 경우
        if 'code=' in res.url:
            return res.url.split('code=')[1].split('&')[0]
            
        # 검색 결과 리스트 페이지로 간 경우 첫 번째 결과의 href 추출
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        a_tags = soup.select("td.tit a")
        if a_tags:
            href = a_tags[0].get('href', '')
            if 'code=' in href:
                return href.split('code=')[1].split('&')[0]
        debug_logs.append("Search No tags found")
    except Exception as e:
        debug_logs.append(f"Search Error: {str(e)}")

    # 3. pykrx 라이브러리를 활용한 Fallback
    try:
        from pykrx import stock as pystock
        from datetime import datetime
        from dateutil.relativedelta import relativedelta
        
        today = datetime.now()
        found_date = False
        for i in range(10):
            d = (today - relativedelta(days=i)).strftime("%Y%m%d")
            kospi_tickers = pystock.get_market_ticker_list(d, market="KOSPI")
            if kospi_tickers:
                found_date = True
                kosdaq_tickers = pystock.get_market_ticker_list(d, market="KOSDAQ")
                all_tickers = kospi_tickers + kosdaq_tickers
                
                for t in all_tickers:
                    name = pystock.get_market_ticker_name(t)
                    if name and name.replace(" ", "").upper() == target:
                        return str(t)
                        
                for t in all_tickers:
                    name = pystock.get_market_ticker_name(t)
                    if name and target in name.replace(" ", "").upper():
                        return str(t)
                break
        if not found_date:
            debug_logs.append("pykrx No trading dates found")
    except Exception as e:
        debug_logs.append(f"pykrx Error: {str(e)}")

    # 변환 실패 시 디버그 로그 반환
    return f"ERROR: {' | '.join(debug_logs)}"


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

        original_symbol = symbol
        symbol = resolve_symbol(symbol)

        if symbol.startswith("ERROR:"):
            return Response(
                {"error": f"'{original_symbol}' 종목 검색 실패. 상세 사유: {symbol}"},
                status=status.HTTP_404_NOT_FOUND
            )

        if not symbol.isdigit():
            return Response(
                {"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."},
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
        if not symbol.isdigit():
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

        if not symbol.isdigit():
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

        if not symbol.isdigit():
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

        if not symbol.isdigit():
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

        if not symbol.isdigit():
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

        if not symbol.isdigit():
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

        if not symbol.isdigit():
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

        if not symbol.isdigit():
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

        if not symbol.isdigit():
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

        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not symbol.isdigit() and symbol != "ALL":
            return Response({"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        cache_key = f"web04_detail_{symbol}_{user_id}_{period}"
        cached_data = cache.get(cache_key)

        if cached_data:
            return Response(cached_data)

        try:
            detail_report_service = WebDetailReport()

            # 서비스 계층 호출
            result = detail_report_service.get_detail_report(
                scope=symbol,
                period=period,
            )

            if "error" in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)

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

            original_symbol = symbol
            symbol = resolve_symbol(symbol)
            
            if symbol.startswith("ERROR:"):
                return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

            if not symbol.isdigit():
                return Response({"error": f"'{original_symbol}' 종목을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

            # 1. 프론트엔드에서 넘어온 값을 우선적으로 받는다.
            company_name = request.query_params.get('company_name')

            # 2. 값이 아예 없거나, 실수로 종목 코드가 이름 자리에 들어왔을 때만 API로 검색을 시도한다.
            if not company_name or company_name == symbol or company_name.isdigit():
                chart_provider = StockChartDataProvider()
                info = chart_provider.get_stock_info(symbol)
                company_name = info.get('company_name', original_symbol if not original_symbol.isdigit() else symbol)

            cache_key = f"web05_report_{symbol}_{user_id}"

            # 테스트를 위해 임시로 캐시를 비활성화해 둔 상태
            # cached_data = cache.get(cache_key)
            # if cached_data:
            #    return Response(cached_data)

            web_report = WebStockReport()
            result = web_report.get_report(symbol, company_name, user_id)

            if "error" in result:
                return Response(result, status=status.HTTP_400_BAD_REQUEST)

            cache.set(cache_key, result, 1800)
            return Response(result)

        # noinspection PyBroadException
        except Exception as e:
            print(f"[API 500 에러 원인 추적 - StockReportView] {e}")

            error_response = {
                "error": str(e),
                "message": "리포트 생성 중 서버 내부 오류가 발생했습니다."
            }
            return Response(error_response, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class StockFavoriteToggleView(APIView):
    """
    Web05 - 관심 종목 추가/해제 API
    POST /api/web/stocks/<str:symbol>/favorite
    """

    # noinspection PyMethodMayBeStatic
    def post(self, request, symbol):
        original_symbol = symbol
        symbol = resolve_symbol(symbol)
        
        if symbol.startswith("ERROR:"):
            return Response({"error": f"'{original_symbol}' 종목 검색 실패. {symbol}"}, status=status.HTTP_404_NOT_FOUND)

        if not symbol.isdigit():
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

    # noinspection PyMethodMayBeStatic
    def get(self, request):
        user_id = request.query_params.get('user_id', 'default_user')
        web_report = WebStockReport()
        result = web_report.get_favorites(user_id)
        return Response(result)
