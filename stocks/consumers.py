import json
import asyncio
from asgiref.sync import async_to_sync
from channels.generic.websocket import AsyncWebsocketConsumer
from .services.web_orderbook_ws import get_web_order_book_stream

# --- Singleton 관리를 위한 전역 변수 ---
# key: symbol (종목코드), value: 구독자 수 (참조 카운트)
kis_ws_subscribers = {} 

def global_send_data_to_group(symbol, data, message_type):
    """
    KIS 스트림 스레드에서 호출되는 콜백 함수 (동기)
    채널 레이어를 통해 해당 종목 그룹에 메시지를 비동기적으로 전송
    """
    from channels.layers import get_channel_layer
    channel_layer = get_channel_layer()
    
    group_name = f'stock_{message_type}_{symbol}'
    
    async_to_sync(channel_layer.group_send)(
        group_name,
        {
            'type': 'broadcast_message',
            'data': data
        }
    )

class StockSearchConsumer(AsyncWebsocketConsumer):
    """
    종목 검색을 위한 웹소켓 소비자
    """
    async def connect(self):
        await self.accept()

    async def disconnect(self, close_code):
        pass

    async def receive(self, text_data):
        text_data_json = json.loads(text_data)
        keyword = text_data_json.get('keyword', '')
        # (이하 로직은 기존과 동일)
        # ...

class StockTickerConsumer(AsyncWebsocketConsumer):
    """
    특정 종목의 실시간 호가 정보를 전송하는 웹소켓 소비자
    """
    async def connect(self):
        self.symbol = self.scope['url_route']['kwargs']['symbol']
        self.room_group_name = f'stock_ticker_{self.symbol}'

        # 그룹에 참여
        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

        # --- Singleton 로직 시작 ---
        stream = get_web_order_book_stream()
        
        # KIS 스트림이 연결되어 있지 않으면 연결
        if not stream.is_connected:
            try:
                stream.connect()
            except Exception as e:
                await self.send_error(f"Failed to connect to KIS WebSocket API: {e}")
                await self.close()
                return

        # 해당 종목에 대한 구독자가 없으면 새로 구독
        if self.symbol not in kis_ws_subscribers or kis_ws_subscribers[self.symbol] == 0:
            stream.subscribe(self.symbol, lambda data: global_send_data_to_group(self.symbol, data, 'ticker'))
            kis_ws_subscribers[self.symbol] = 1
        else:
            # 이미 구독 중이면 구독자 수만 증가
            kis_ws_subscribers[self.symbol] += 1

        await self.send_status(f'Subscribed to {self.symbol} real-time ticker.')

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

        if self.symbol in kis_ws_subscribers:
            kis_ws_subscribers[self.symbol] -= 1
            if kis_ws_subscribers[self.symbol] <= 0:
                stream = get_web_order_book_stream()
                stream.unsubscribe(self.symbol)
                kis_ws_subscribers.pop(self.symbol, None)

    async def broadcast_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'ticker_update',
            'data': event['data']
        }))

    async def send_status(self, message):
        await self.send(text_data=json.dumps({'type': 'status', 'message': message}))

    async def send_error(self, message):
        await self.send(text_data=json.dumps({'type': 'error', 'message': message}))


class OrderBookConsumer(AsyncWebsocketConsumer):
    """
    특정 종목의 실시간 호가 정보를 전송하는 웹소켓 소비자
    """
    async def connect(self):
        self.symbol = self.scope['url_route']['kwargs']['symbol']
        self.room_group_name = f'stock_orderbook_{self.symbol}'

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

        stream = get_web_order_book_stream()
        if not stream.is_connected:
            try:
                stream.connect()
            except Exception as e:
                await self.send_error(f"Failed to connect to KIS WebSocket API: {e}")
                await self.close()
                return

        if self.symbol not in kis_ws_subscribers or kis_ws_subscribers[self.symbol] == 0:
            stream.subscribe(self.symbol, lambda data: global_send_data_to_group(self.symbol, data, 'orderbook'))
            kis_ws_subscribers[self.symbol] = 1
        else:
            kis_ws_subscribers[self.symbol] += 1

        await self.send_status(f'Subscribed to {self.symbol} real-time order book.')

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

        if self.symbol in kis_ws_subscribers:
            kis_ws_subscribers[self.symbol] -= 1
            if kis_ws_subscribers[self.symbol] <= 0:
                stream = get_web_order_book_stream()
                stream.unsubscribe(self.symbol)
                kis_ws_subscribers.pop(self.symbol, None)

    async def broadcast_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'orderbook_update',
            'data': event['data']
        }))

    # --- Helper methods for sending messages ---
    async def send_status(self, message):
        await self.send(text_data=json.dumps({'type': 'status', 'message': message}))

    async def send_error(self, message):
        await self.send(text_data=json.dumps({'type': 'error', 'message': message}))
