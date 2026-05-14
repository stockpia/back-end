import json
from channels.generic.websocket import AsyncWebsocketConsumer
import asyncio
from asgiref.sync import async_to_sync
from .services.web_orderbook_ws import WebOrderBookStream

# 종목별 KIS WebSocket 스트림 인스턴스를 관리하는 딕셔너리 (싱글톤 패턴)
# key: symbol (종목코드), value: WebOrderBookStream 인스턴스
kis_ws_streams = {}

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
            # pykrx를 사용하여 종목 리스트 가져오기 (pykrx는 동기 함수이므로, 비동기 컨텍스트에서 직접 호출 시 주의 필요)
            # 여기서는 예시로 남겨두지만, 실제 서비스에서는 비동기적으로 처리하거나 다른 API 사용 권장
            from pykrx import stock
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
    특정 종목의 실시간 호가 정보를 전송하는 웹소켓 소비자 (KIS API 연동)
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.symbol = None
        self.room_group_name = None

    async def connect(self):
        self.symbol = self.scope['url_route']['kwargs']['symbol']
        self.room_group_name = f'stock_{self.symbol}'

        # 그룹에 참여
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        # 해당 종목의 KIS WebSocket 스트림이 없으면 새로 생성
        if self.symbol not in kis_ws_streams:
            try:
                stream = WebOrderBookStream()
                stream.connect()
                kis_ws_streams[self.symbol] = stream
                
                # KIS WebSocket으로부터 실시간 호가 데이터 구독
                # 콜백 함수로 self.send_orderbook_data를 전달
                stream.subscribe(self.symbol, self.send_orderbook_data)

                print(f"Created and connected new KIS WebSocket stream for {self.symbol}")

            except Exception as e:
                print(f"WebSocket KIS API connection error for {self.symbol}: {e}")
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': f"Failed to connect to KIS WebSocket API: {str(e)}"
                }))
                await self.close()
                return

        # 연결 성공 메시지 (선택 사항)
        await self.send(text_data=json.dumps({
            'type': 'status',
            'message': f'Subscribed to {self.symbol} real-time order book.'
        }))

    async def disconnect(self, close_code):
        # 그룹에서 탈퇴
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )
        print(f"WebSocket client disconnected for {self.symbol}")

        # 더 이상 해당 종목을 구독하는 클라이언트가 없으면 KIS WebSocket 연결 종료
        # group_send를 통해 그룹에 남아있는 클라이언트 수를 확인하는 것은 복잡하므로,
        # 여기서는 단순하게 연결이 끊길 때마다 KIS 연결도 끊도록 처리 (개선 필요)
        if self.symbol in kis_ws_streams:
            kis_ws_streams[self.symbol].unsubscribe(self.symbol)
            kis_ws_streams[self.symbol].disconnect()
            del kis_ws_streams[self.symbol]
            print(f"Disconnected and removed KIS WebSocket stream for {self.symbol}")

    async def receive(self, text_data):
        # 클라이언트로부터 메시지를 받을 경우 처리 (예: 종목 변경 요청 등)
        pass

    def send_orderbook_data(self, data):
        """
        WebOrderBookStream으로부터 콜백으로 호출될 함수 (동기)
        수신된 호가 데이터를 채널 레이어를 통해 그룹에 비동기적으로 전송
        """
        async_to_sync(self.channel_layer.group_send)(
            self.room_group_name,
            {
                'type': 'orderbook_message',
                'data': data
            }
        )

    async def orderbook_message(self, event):
        """
        그룹으로부터 메시지를 수신하여 웹소켓으로 전송 (비동기)
        """
        await self.send(text_data=json.dumps({
            'type': 'orderbook_update',
            'symbol': event['data']['symbol'],
            'asks': event['data']['asks'],
            'bids': event['data']['bids'],
            'trade_strength': event['data']['trade_strength']
        }))
