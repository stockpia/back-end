import json
from channels.generic.websocket import AsyncWebsocketConsumer
from pykrx import stock
import asyncio

class StockSearchConsumer(AsyncWebsocketConsumer):
    """
    종목 검색을 위한 웹소켓 소비자
    """
    async def connect(self):
        await self.accept()

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        """
        클라이언트로부터 검색어를 받아서 결과를 전송
        """
        text_data_json = json.loads(text_data)
        keyword = text_data_json.get('keyword', '')

        if not keyword:
            await self.send(text_data=json.dumps({
                'type': 'search_result',
                'data': []
            }))
            return

        try:
            # pykrx를 사용하여 종목 리스트 가져오기
            tickers = stock.get_market_ticker_list()
            results = []
            for ticker in tickers:
                name = stock.get_market_ticker_name(ticker)
                if keyword.lower() in name.lower():
                    results.append({'symbol': ticker, 'name': name})
            
            # 검색 결과 전송
            await self.send(text_data=json.dumps({
                'type': 'search_result',
                'data': results[:20]  # 상위 20개 결과만 전송
            }))
        except Exception as e:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': str(e)
            }))


class StockTickerConsumer(AsyncWebsocketConsumer):
    """
    특정 종목의 실시간 가격 정보를 전송하는 웹소켓 소비자
    """
    async def connect(self):
        self.symbol = self.scope['url_route']['kwargs']['symbol']
        self.room_group_name = f'stock_{self.symbol}'

        # 그룹에 참여
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()
        
        # 연결 시 첫 데이터 전송
        await self.send_initial_price()

    async def disconnect(self, close_code):
        # 그룹에서 탈퇴
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def send_initial_price(self):
        """연결 시 현재가를 즉시 조회하여 전송"""
        try:
            price_info = stock.get_market_ohlcv_by_date(stock.get_nearest_business_day(), stock.get_nearest_business_day(), self.symbol)
            if not price_info.empty:
                latest_price = price_info.iloc[0]
                await self.send(text_data=json.dumps({
                    'type': 'ticker_update',
                    'symbol': self.symbol,
                    'price': latest_price['종가'],
                    'change': latest_price['종가'] - latest_price['시가'],
                    'change_rate': (latest_price['종가'] - latest_price['시가']) / latest_price['시가'] * 100 if latest_price['시가'] != 0 else 0,
                }))
        except Exception as e:
            await self.send(text_data=json.dumps({
                'type': 'error',
                'message': f"Initial price fetch failed: {str(e)}"
            }))

    # 그룹으로부터 메시지를 수신
    async def ticker_update(self, event):
        # 웹소켓으로 메시지 전송
        await self.send(text_data=json.dumps(event))
