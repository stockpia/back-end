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
from .models import User, KisAccount

from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi


def resolve_symbol(symbol_or_name: str) -> str:
    """
    이름(예: 삼성전자)이 들어오면 종목코드(005930)로 변환,
    종목코드가 들어오면 그대로 반환.
    """
    if symbol_or_name.isdigit():
        return symbol_or_name
    
    if symbol_or_name.upper() == "ALL":
        return "ALL"

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
            
            # 1순위: 정확히 일치하는 종목 찾기
            for category in items:
                for item in category:
                    if len(item) >= 2 and item[1].isdigit():
                        if item[0] == symbol_or_name:
                            return str(item[1])
                            
            # 2순위: 첫 번째 종목 반환
            for category in items:
                for item in category:
                    if len(item) >= 2 and item[1].isdigit():
                        return str(item[1])
            debug_logs.append("AC No items found")
    except Exception as e:
        debug_logs.append(f"AC Error: {str(e)}")

    # 2. 네이버 금융 검색 스크래핑
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
        # utf-8 또는 euc-kr 중 하나로 렌더링되므로, 자동 감지된 인코딩 사용
        soup = BeautifulSoup(res.content, 'html.parser', from_encoding='euc-kr')
        
        # 주식 종목 검색 결과 테이블 파싱
        # td.tit 클래스를 가진 a 태그 찾기
        a_tags = soup.select("td.tit a")
        if a_tags:
            for a_tag in a_tags:
                href = a_tag.get('href', '')
                if 'code=' in href:
                    # 일치하는 이름이 있는지 확인 (카카오, 두산 등 짧은 이름 매칭)
                    name = a_tag.text.strip().replace(" ", "").upper()
                    if name == target or target in name:
                        return href.split('code=')[1].split('&')[0]
            
            # 정확히 일치하는 걸 못찾았으면 첫번째 결과 반환
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

            web_report = WebStockReport()
            
            # 1. 프론트엔드에서 넘어온 값을 우선적으로 받는다.
            company_name = request.query_params.get('company_name')

            # 2. 값이 아예 없거나, 실수로 종목 코드가 이름 자리에 들어왔을 때만 API로 검색을 시도한다.
            if not company_name or company_name == symbol or company_name.isdigit():
                info = web_report.chart_provider.get_stock_info(symbol)
                company_name = info.get('company_name', original_symbol if not original_symbol.isdigit() else symbol)

            cache_key = f"web05_report_{symbol}_{user_id}"

            cached_data = cache.get(cache_key)
            if cached_data:
               return Response(cached_data)

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


class UserSignUpView(APIView):
    """
    사용자 회원가입 API
    POST /api/web/users/signup
    """
    def post(self, request):
        phone = request.data.get('phone')
        name = request.data.get('name')
        birthdate = request.data.get('birthdate')
        password = request.data.get('password')

        if not all([phone, name, birthdate, password]):
            return Response({"error": "모든 필수 정보를 입력해주세요."}, status=status.HTTP_400_BAD_REQUEST)

        if User.objects.filter(phone=phone).exists():
            return Response({"error": "이미 가입된 전화번호입니다."}, status=status.HTTP_400_BAD_REQUEST)

        user = User(phone=phone, name=name, birthdate=birthdate)
        user.set_password(password)
        user.save()

        return Response({
            "message": "회원가입이 성공적으로 완료되었습니다.",
            "user_id": user.user_id
        }, status=status.HTTP_201_CREATED)


class UserSignInView(APIView):
    """
    사용자 로그인 API
    POST /api/web/users/signin
    """
    def post(self, request):
        phone = request.data.get('phone')
        password = request.data.get('password')

        if not all([phone, password]):
            return Response({"error": "전화번호와 비밀번호를 모두 입력해주세요."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(phone=phone)
            if not user.check_password(password):
                return Response({"error": "비밀번호가 일치하지 않습니다."}, status=status.HTTP_400_BAD_REQUEST)
            
            # 여기서 JWT 토큰 또는 세션 생성 로직 추가 (지금은 user_id만 반환)
            return Response({
                "message": "로그인에 성공했습니다.",
                "user_id": user.user_id,
                "name": user.name
            }, status=status.HTTP_200_OK)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)


class KisAccountConnectView(APIView):
    """
    KIS 계좌 연결 API (로그인 후 사용)
    POST /api/web/kis/connect
    """

    def post(self, request):
        user_id = request.data.get('user_id') # 실제로는 인증된 사용자 정보에서 가져와야 함
        if not user_id:
            return Response({"error": "사용자 정보가 필요합니다."}, status=status.HTTP_401_UNAUTHORIZED)
        
        try:
            user = User.objects.get(user_id=user_id)
        except User.DoesNotExist:
            return Response({"error": "존재하지 않는 사용자입니다."}, status=status.HTTP_404_NOT_FOUND)

        # .env 파일에서 기본값 가져오기
        app_key = os.environ.get('KIS_APP_KEY')
        app_secret_key = os.environ.get('KIS_APP_SECRET')
        account_id = os.environ.get('KIS_ACCOUNT_ID')
        account_suffix = os.environ.get('KIS_ACCOUNT_SUFFIX')
        env = os.environ.get('KIS_ENV', 'vps')
        
        if not all([app_key, app_secret_key, account_id, account_suffix]):
            return Response({"error": ".env 파일에 KIS 관련 설정이 올바르지 않습니다."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        account_number = f"{account_id}-{account_suffix}"

        # KisAccount 모델에 정보 저장/업데이트
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
        
        message = "KIS 계좌가 성공적으로 연결되었습니다." if created else "KIS 계좌 정보가 업데이트되었습니다."
        return Response({"message": message}, status=status.HTTP_200_OK)


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
