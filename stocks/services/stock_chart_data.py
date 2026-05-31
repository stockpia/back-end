"""
종목 차트 페이지용 데이터 조회 모듈
웹 기획서 Web_01 종목 차트 페이지에 필요한 모든 데이터 제공

주요 기능:
- 종목 기본 정보 (헤더용)
- 차트 데이터 (일봉/분봉)
- 펀더멘탈 지표 (PER/PBR/ROE)
- 기술적 분석 (RSI/MA/추세)
- Plotly 차트 생성 (fig.to_json())
"""

import time
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Optional, List

try:
    import FinanceDataReader as fdr
except ImportError:
    fdr = None

# 종목코드 → 회사명 단일 lookup. 한투 inquire-price 응답에 name 필드가 없을 때
# 종목코드가 그대로 노출되는 문제 회피용.
# 이전엔 pykrx.get_market_ticker_name 을 사용했으나 KRX scraping 단절로 항상 빈값 →
# 네이버 종목 페이지 title 파싱(stock_search.lookup_company_name)으로 위임.
from .stock_search import lookup_company_name as _lookup_company_name

try:
    from .HantuStock import HantuStock
except ImportError:
    try:
        from .HantuStock import HantuStock
    except ImportError:
        HantuStock = None

try:
    from .dart_client import DartClient
except ImportError:
    try:
        from .dart_client import DartClient
    except ImportError:
        DartClient = None

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    go = None
    make_subplots = None


# 모듈 레벨 HantuStock 싱글톤 (프로세스 내 1회만 초기화, 토큰 재발급 방지)
#_hantu_shared = None


class StockChartDataProvider:
    """종목 차트 페이지용 데이터 제공 클래스"""

    def __init__(self, hantu_stock: Optional[HantuStock] = None):
        """
        Args:
            hantu_stock: HantuStock 인스턴스 (선택). 제공 시 실시간 시세/PER/PBR 조회 가능
        """
        global _hantu_shared

        # if fdr is None:
        #     raise ImportError("FinanceDataReader가 설치되지 않았습니다. pip install finance-datareader")

        self._hantu = hantu_stock
        if hantu_stock is None and HantuStock is not None:
            try:
                self._hantu = HantuStock()
            except Exception as e:
                print(f"[WARN] HantuStock 초기화 실패: {e}. 일부 기능 제한됨.")
                self._hantu = None
        else:
            self._hantu = hantu_stock

        # DART 클라이언트 (부채비율/현금흐름 등)
        self._dart = None
        if DartClient is not None:
            try:
                import os
                dart_key = os.environ.get("DART_API_KEY", "")
                if dart_key:
                    self._dart = DartClient(api_key=dart_key)
            except Exception as e:
                print(f"[WARN] DartClient 초기화 실패: {e}")

        # 요청 내 TTL 캐시 (KIS: 60초, FDR: 300초)
        self._price_cache: dict = {}  # ticker → (data_dict, fetch_time)
        self._fdr_cache: dict = {}    # ticker → (DataFrame, fetch_time)

    # ==================== 내부 캐시 헬퍼 ====================

    def _get_stock_price_data(self, ticker: str) -> dict:
        """KIS 현재가 조회 (60초 TTL 캐시 — 동일 요청 내 중복 호출 방지)"""
        cached = self._price_cache.get(ticker)
        if cached and (time.time() - cached[1]) < 60:
            return cached[0]
        if not self._hantu:
            return {"error": "HantuStock 미초기화"}
        data = self._hantu.get_stock_price(ticker)
        self._price_cache[ticker] = (data, time.time())
        return data

    def _get_fdr_df(self, ticker: str) -> pd.DataFrame:
        """FDR 과거 데이터 조회 (300초 TTL 캐시 — 동일 요청 내 중복 다운로드 방지)"""
        if fdr is None:
            raise ImportError("FinanceDataReader가 설치되지 않았거나 import에 실패했습니다.")
        cached = self._fdr_cache.get(ticker)
        if cached and (time.time() - cached[1]) < 300:
            return cached[0]
        df = fdr.DataReader(ticker)
        df.columns = [c.lower() for c in df.columns]
        self._fdr_cache[ticker] = (df, time.time())
        return df

    def get_historical_data(self, ticker: str) -> pd.DataFrame:
        """FDR 과거 데이터 공개 메서드 (chatbot/web _calculate_returns 전용)"""
        return self._get_fdr_df(ticker)

    # ==================== 기본 정보 ====================

    def get_stock_info(self, ticker: str) -> Dict:
        """
        종목 기본 정보 조회 (헤더 + 개요 요약)

        Returns:
            dict: company_name, ticker, current_price, price_change, change_rate
        """
        try:
            # 한투 API 사용 가능하면 실시간 데이터 조회 (캐시 활용)
            if self._hantu:
                data = self._get_stock_price_data(ticker)
                if "error" not in data:
                    return {
                        "company_name": data.get('name') or _lookup_company_name(ticker),
                        "ticker": ticker,
                        "current_price": data.get('current_price', 0),
                        "price_change": data.get('price_change', 0),
                        "change_rate": data.get('change_rate', 0),
                        "open": data.get('open', 0),
                        "high": data.get('high', 0),
                        "low": data.get('low', 0),
                        "volume": data.get('volume', 0),
                        "source": "한국투자증권 API (실시간)"
                    }

            # fallback: FinanceDataReader 사용 (캐시 활용)
            df = self._get_fdr_df(ticker)
            if df.empty:
                return {"error": "데이터를 찾을 수 없습니다"}

            df = df.tail(2)
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else df.iloc[-1]

            current_price = float(latest['close'])
            prev_close = float(prev['close'])
            price_change = current_price - prev_close
            change_rate = (price_change / prev_close) * 100 if prev_close > 0 else 0

            return {
                "company_name": _lookup_company_name(ticker),  # pykrx 로 종목명 lookup
                "ticker": ticker,
                "current_price": current_price,
                "price_change": price_change,
                "change_rate": round(change_rate, 2),
                "prev_close": prev_close,
                "date": str(df.index[-1].date()),
                "source": "FinanceDataReader"
            }
        except Exception as e:
            return {"error": str(e)}

    # ==================== 차트 데이터 ====================

    def get_chart_data(self, ticker: str, period: str = "3m") -> Dict:
        """
        기간별 차트 데이터 조회

        Args:
            ticker: 종목코드
            period: 기간 (1d, 1w, 1m, 3m, 6m, 1y, 3y)

        Returns:
            dict: dates[], ohlcv 데이터
        """
        period_days = {
            "1d": 1,
            "1w": 7,
            "1m": 30,
            "3m": 90,
            "6m": 180,
            "1y": 365,
            "3y": 1095
        }

        days = period_days.get(period, 90)

        try:
            df = self._get_fdr_df(ticker).tail(days).copy()

            return {
                "ticker": ticker,
                "period": period,
                "count": len(df),
                "data": {
                    "dates": [str(d.date()) for d in df.index],
                    "open": df['open'].tolist(),
                    "high": df['high'].tolist(),
                    "low": df['low'].tolist(),
                    "close": df['close'].tolist(),
                    "volume": df['volume'].tolist()
                }
            }
        except Exception as e:
            return {"error": str(e)}

    # ==================== 지표 카드 (PER/PBR/ROE) ====================

    def get_fundamental_metrics(self, ticker: str) -> Dict:
        """
        PER, PBR, ROE 등 펀더멘탈 지표 조회 (한투 API 사용)

        Returns:
            dict: per, pbr, eps, bps, roe
        """
        try:
            # 한투 API로 실시간 조회 (캐시 활용)
            if self._hantu:
                data = self._get_stock_price_data(ticker)

                if "error" not in data:
                    eps = data.get('eps', 0)
                    bps = data.get('bps', 0)
                    # ROE 계산: EPS / BPS * 100
                    roe = (eps / bps * 100) if bps > 0 else 0

                    return {
                        "ticker": ticker,
                        "name": data.get('name', ''),
                        "per": data.get('per', 0),
                        "pbr": data.get('pbr', 0),
                        "eps": eps,
                        "bps": bps,
                        "roe": round(roe, 2),
                        "w52_high": data.get('w52_high', 0),
                        "w52_low": data.get('w52_low', 0),
                        "market_cap": data.get('market_cap', 0),
                        "source": "한국투자증권 API"
                    }

            return {"error": "한투 API를 사용할 수 없습니다. HantuStock 초기화 필요."}
        except Exception as e:
            return {"error": str(e)}

    # ==================== 기술적 분석 ====================

    def get_technical_indicators(self, ticker: str, period: str = "3m") -> Dict:
        """
        기술적 분석 지표 조회 (RSI, 이동평균선)

        Returns:
            dict: rsi, ma5, ma20, ma60, trend
        """
        try:
            df = self._get_fdr_df(ticker).tail(max(100, 60)).copy()

            close = df['close']

            # RSI 계산 (14일 기준)
            rsi = self._calculate_rsi(close, 14)

            # 이동평균선 계산
            ma5 = close.rolling(window=5).mean()
            ma20 = close.rolling(window=20).mean()
            ma60 = close.rolling(window=60).mean()

            # 현재값
            current_price = float(close.iloc[-1])
            current_rsi = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None
            current_ma5 = float(ma5.iloc[-1]) if not pd.isna(ma5.iloc[-1]) else None
            current_ma20 = float(ma20.iloc[-1]) if not pd.isna(ma20.iloc[-1]) else None
            current_ma60 = float(ma60.iloc[-1]) if not pd.isna(ma60.iloc[-1]) else None

            # 추세 판단
            trend = self._determine_trend(current_price, current_ma5, current_ma20, current_ma60)

            # RSI 해석
            rsi_signal = self._interpret_rsi(current_rsi)

            return {
                "ticker": ticker,
                "current_price": current_price,
                "rsi": {
                    "value": round(current_rsi, 2) if current_rsi else None,
                    "signal": rsi_signal
                },
                "moving_averages": {
                    "ma5": round(current_ma5, 0) if current_ma5 else None,
                    "ma20": round(current_ma20, 0) if current_ma20 else None,
                    "ma60": round(current_ma60, 0) if current_ma60 else None
                },
                "trend": trend,
                # 차트용 시계열 데이터
                "series": {
                    "dates": [str(d.date()) for d in df.index[-30:]],
                    "rsi": [round(v, 2) if not pd.isna(v) else None for v in rsi.tail(30)],
                    "ma5": [round(v, 0) if not pd.isna(v) else None for v in ma5.tail(30)],
                    "ma20": [round(v, 0) if not pd.isna(v) else None for v in ma20.tail(30)],
                    "ma60": [round(v, 0) if not pd.isna(v) else None for v in ma60.tail(30)]
                }
            }
        except Exception as e:
            return {"error": str(e)}

    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> pd.Series:
        """RSI 계산"""
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def _determine_trend(self, price: float, ma5: float, ma20: float, ma60: float) -> Dict:
        """추세 판단"""
        if not all([ma5, ma20, ma60]):
            return {"direction": "unknown", "description": "데이터 부족"}

        # 정배열: 가격 > MA5 > MA20 > MA60
        if price > ma5 > ma20 > ma60:
            return {
                "direction": "strong_up",
                "description": "강한 상승 추세 (정배열)",
                "signal": "매수 우위"
            }
        # 역배열: 가격 < MA5 < MA20 < MA60
        elif price < ma5 < ma20 < ma60:
            return {
                "direction": "strong_down",
                "description": "강한 하락 추세 (역배열)",
                "signal": "매도 우위"
            }
        # 가격이 MA20 위
        elif price > ma20:
            return {
                "direction": "up",
                "description": "상승 추세",
                "signal": "매수 관망"
            }
        # 가격이 MA20 아래
        else:
            return {
                "direction": "down",
                "description": "하락 추세",
                "signal": "매도 관망"
            }

    def _interpret_rsi(self, rsi: float) -> Dict:
        """RSI 해석"""
        if rsi is None:
            return {"status": "unknown", "description": "데이터 부족"}

        if rsi >= 70:
            return {
                "status": "overbought",
                "description": "과매수 구간 (조정 가능성)",
                "signal": "매도 고려"
            }
        elif rsi <= 30:
            return {
                "status": "oversold",
                "description": "과매도 구간 (반등 가능성)",
                "signal": "매수 고려"
            }
        elif rsi >= 50:
            return {
                "status": "bullish",
                "description": "상승 모멘텀",
                "signal": "중립-매수"
            }
        else:
            return {
                "status": "bearish",
                "description": "하락 모멘텀",
                "signal": "중립-매도"
            }

    # ==================== 5분봉 데이터 (하루 탭용) ====================

    def get_minute_chart_data(self, ticker: str, interval: int = 10) -> Dict:
        """
        분봉 차트 데이터 조회 (하루 탭용)

        Args:
            ticker: 종목코드
            interval: 분봉 간격 (기본 10분 — 정규장 390분/10 = 39개 캔들로 모바일
                가시성 균형. 이전 5분 = 78개는 모바일에서 너무 좁음)

        Returns:
            dict: 분봉 OHLCV 데이터
        """
        if not self._hantu:
            return {"error": "한투 API를 사용할 수 없습니다. HantuStock 초기화 필요."}

        try:
            data = self._hantu.get_minute_chart(ticker, interval)
            if "error" in data:
                return data

            # times -> dates 키 변환 (일관성 유지)
            raw_data = data.get("data", {})
            return {
                "ticker": ticker,
                "interval": interval,
                "period": "1d",
                "count": data.get("count", 0),
                "data": {
                    "dates": raw_data.get("times", []),
                    "open": raw_data.get("open", []),
                    "high": raw_data.get("high", []),
                    "low": raw_data.get("low", []),
                    "close": raw_data.get("close", []),
                    "volume": raw_data.get("volume", [])
                }
            }
        except Exception as e:
            return {"error": str(e)}

    # ==================== Plotly 차트 생성 ====================

    def create_candlestick_chart(
        self,
        ticker: str,
        period: str = "3m",
        show_ma: bool = True,
        show_volume: bool = True
    ) -> Dict:
        """
        캔들스틱 차트 생성 (Plotly fig.to_json())

        Args:
            ticker: 종목코드
            period: 기간 (1d, 1m, 3m, 1y)
            show_ma: 이동평균선 표시 여부
            show_volume: 거래량 표시 여부
        """
        if go is None or make_subplots is None:
            return {"error": "Plotly가 설치되지 않았거나 import에 실패했습니다."}

        try:
            # 1d는 분봉, 나머지는 일봉
            if period == "1d":
                chart_data = self.get_minute_chart_data(ticker)
            else:
                chart_data = self.get_chart_data(ticker, period)

            if "error" in chart_data:
                return chart_data

            data = chart_data["data"]
            dates = data["dates"]

            # OHL=0 보정 — 거래정지/거래 없음 일자는 KIS 가 OHL=0, Close 만 마지막
            # 종가로 응답함. 그대로 그리면 캔들 몸통이 0 부터 close 까지 길게 뻗어
            # 비현실적으로 보임 (예: 에이리츠 140910 정지 기간). close 값으로 채워
            # OHLC 동일한 1점 캔들 (보합) 로 표시.
            def _patch_zero(series, ref):
                return [r if (v == 0 or v is None) else v for v, r in zip(series, ref)]
            close_ref = list(data["close"])
            data["open"]  = _patch_zero(data["open"],  close_ref)
            data["high"]  = _patch_zero(data["high"],  close_ref)
            data["low"]   = _patch_zero(data["low"],   close_ref)

            # 시계열 데이터를 datetime으로 변환
            if period == "1d":
                # 분봉: HH:MM 형식
                x_data = dates
            else:
                # 일봉: YYYY-MM-DD 형식
                x_data = pd.to_datetime(dates)

            # 서브플롯 생성 (거래량 포함 시)
            if show_volume:
                fig = make_subplots(
                    rows=2, cols=1,
                    shared_xaxes=True,
                    vertical_spacing=0.03,
                    row_heights=[0.7, 0.3]
                )
            else:
                fig = go.Figure()

            # 캔들스틱 차트
            candlestick = go.Candlestick(
                x=x_data,
                open=data["open"],
                high=data["high"],
                low=data["low"],
                close=data["close"],
                name="주가",
                increasing_line_color='#FF3B30',  # 상승: 빨간색
                decreasing_line_color='#007AFF'   # 하락: 파란색
            )

            if show_volume:
                fig.add_trace(candlestick, row=1, col=1)
            else:
                fig.add_trace(candlestick)

            # 이동평균선 추가 (일봉만)
            ma_periods = []
            if show_ma and period != "1d":
                close = pd.Series(data["close"])
                ma_config = [
                    (5, "#FF9500", "MA5"),
                    (20, "#34C759", "MA20"),
                    (60, "#AF52DE", "MA60")
                ]

                for ma_period, color, name in ma_config:
                    if len(close) >= ma_period:
                        ma = close.rolling(window=ma_period).mean()
                        ma_trace = go.Scatter(
                            x=x_data,
                            y=ma.tolist(),
                            mode='lines',
                            name=name,
                            line=dict(color=color, width=1)
                        )
                        if show_volume:
                            fig.add_trace(ma_trace, row=1, col=1)
                        else:
                            fig.add_trace(ma_trace)
                        ma_periods.append(ma_period)

            # 거래량 차트
            if show_volume:
                colors = ['#FF3B30' if c >= o else '#007AFF'
                         for o, c in zip(data["open"], data["close"])]
                volume_trace = go.Bar(
                    x=x_data,
                    y=data["volume"],
                    name="거래량",
                    marker_color=colors,
                    opacity=0.7
                )
                fig.add_trace(volume_trace, row=2, col=1)

            # 레이아웃 설정
            fig.update_layout(
                title=None,
                xaxis_rangeslider_visible=False,
                showlegend=True,
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.02,
                    xanchor="right",
                    x=1
                ),
                margin=dict(l=10, r=10, t=30, b=10),
                height=400 if show_volume else 300
            )

            # Y축 설정
            fig.update_yaxes(title_text="주가", row=1, col=1) if show_volume else None
            fig.update_yaxes(title_text="거래량", row=2, col=1) if show_volume else None

            return {
                "symbol": ticker,
                "range": period,
                "type": "candlestick",
                "plotly": fig.to_json(),
                "meta": {
                    "ma": ma_periods,
                    "generatedAt": datetime.now().isoformat()
                }
            }

        except Exception as e:
            return {"error": str(e)}

    def create_line_chart(self, ticker: str, period: str = "3m") -> Dict:
        """
        라인 차트 생성 (종가 추이)

        Args:
            ticker: 종목코드
            period: 기간

        Returns:
            dict: plotly JSON 및 메타 정보
        """
        if go is None:
            return {"error": "Plotly가 설치되지 않았거나 import에 실패했습니다."}

        try:
            if period == "1d":
                chart_data = self.get_minute_chart_data(ticker)
            else:
                chart_data = self.get_chart_data(ticker, period)

            if "error" in chart_data:
                return chart_data

            data = chart_data["data"]
            dates = data["dates"]

            fig = go.Figure()

            fig.add_trace(go.Scatter(
                x=dates,
                y=data["close"],
                mode='lines',
                name='종가',
                line=dict(color='#007AFF', width=2),
                fill='tozeroy',
                fillcolor='rgba(0, 122, 255, 0.1)'
            ))

            fig.update_layout(
                title=None,
                showlegend=False,
                margin=dict(l=10, r=10, t=10, b=10),
                height=300
            )

            return {
                "symbol": ticker,
                "range": period,
                "type": "line",
                "plotly": fig.to_json(),
                "meta": {
                    "generatedAt": datetime.now().isoformat()
                }
            }

        except Exception as e:
            return {"error": str(e)}

    def create_technical_chart(self, ticker: str, period: str = "3m") -> Dict:
        """
        기술적 분석 차트 생성 (RSI + 볼린저밴드)

        Args:
            ticker: 종목코드
            period: 기간

        Returns:
            dict: plotly JSON 및 메타 정보
        """
        if go is None or make_subplots is None:
            return {"error": "Plotly가 설치되지 않았거나 import에 실패했습니다."}

        try:
            chart_data = self.get_chart_data(ticker, period)
            if "error" in chart_data:
                return chart_data

            data = chart_data["data"]
            dates = pd.to_datetime(data["dates"])
            close = pd.Series(data["close"])

            # RSI 계산
            rsi = self._calculate_rsi(close, 14)

            # 볼린저 밴드 계산
            ma20 = close.rolling(window=20).mean()
            std20 = close.rolling(window=20).std()
            upper_band = ma20 + (std20 * 2)
            lower_band = ma20 - (std20 * 2)

            # 서브플롯 생성
            fig = make_subplots(
                rows=2, cols=1,
                shared_xaxes=True,
                vertical_spacing=0.1,
                row_heights=[0.6, 0.4],
                subplot_titles=("볼린저 밴드", "RSI")
            )

            # 볼린저 밴드 차트
            fig.add_trace(go.Scatter(
                x=dates, y=upper_band,
                mode='lines', name='Upper Band',
                line=dict(color='rgba(250, 128, 114, 0.5)', width=1)
            ), row=1, col=1)

            fig.add_trace(go.Scatter(
                x=dates, y=lower_band,
                mode='lines', name='Lower Band',
                line=dict(color='rgba(250, 128, 114, 0.5)', width=1),
                fill='tonexty',
                fillcolor='rgba(250, 128, 114, 0.1)'
            ), row=1, col=1)

            fig.add_trace(go.Scatter(
                x=dates, y=ma20,
                mode='lines', name='MA20',
                line=dict(color='#FF9500', width=1)
            ), row=1, col=1)

            fig.add_trace(go.Scatter(
                x=dates, y=close,
                mode='lines', name='종가',
                line=dict(color='#007AFF', width=2)
            ), row=1, col=1)

            # RSI 차트
            fig.add_trace(go.Scatter(
                x=dates, y=rsi,
                mode='lines', name='RSI',
                line=dict(color='#AF52DE', width=2)
            ), row=2, col=1)

            # RSI 기준선
            fig.add_hline(y=70, line_dash="dash", line_color="red", row=2, col=1)
            fig.add_hline(y=30, line_dash="dash", line_color="green", row=2, col=1)
            fig.add_hline(y=50, line_dash="dot", line_color="gray", row=2, col=1)

            fig.update_layout(
                title=None,
                showlegend=True,
                legend=dict(orientation="h", y=1.1),
                margin=dict(l=10, r=10, t=50, b=10),
                height=500
            )

            return {
                "symbol": ticker,
                "range": period,
                "type": "technical",
                "plotly": fig.to_json(),
                "meta": {
                    "indicators": ["bollinger", "rsi"],
                    "generatedAt": datetime.now().isoformat()
                }
            }

        except Exception as e:
            return {"error": str(e)}

    def create_volume_chart(self, ticker: str, period: str = "3m") -> Dict:
        """
        거래량 차트 생성

        Args:
            ticker: 종목코드
            period: 기간

        Returns:
            dict: plotly JSON 및 메타 정보
        """
        if go is None:
            return {"error": "Plotly가 설치되지 않았거나 import에 실패했습니다."}

        try:
            if period == "1d":
                chart_data = self.get_minute_chart_data(ticker)
            else:
                chart_data = self.get_chart_data(ticker, period)

            if "error" in chart_data:
                return chart_data

            data = chart_data["data"]
            dates = data["dates"]

            # 색상 결정 (상승/하락)
            colors = ['#FF3B30' if c >= o else '#007AFF'
                     for o, c in zip(data["open"], data["close"])]

            fig = go.Figure()

            fig.add_trace(go.Bar(
                x=dates,
                y=data["volume"],
                name='거래량',
                marker_color=colors
            ))

            fig.update_layout(
                title=None,
                showlegend=False,
                margin=dict(l=10, r=10, t=10, b=10),
                height=200
            )

            return {
                "symbol": ticker,
                "range": period,
                "type": "volume",
                "plotly": fig.to_json(),
                "meta": {
                    "generatedAt": datetime.now().isoformat()
                }
            }

        except Exception as e:
            return {"error": str(e)}

    def get_chart_api(
        self,
        symbol: str,
        range: str = "3m",
        type: str = "candlestick"
    ) -> Dict:
        """
        웹 API용 통합 차트 생성 메서드

        Args:
            symbol: 종목코드
            range: 기간 (1d, 1m, 3m, 1y)
            type: 차트 타입 (candlestick, line, technical, volume)

        Returns:
            dict: 기획서 형식의 응답
        """
        chart_methods = {
            "candlestick": self.create_candlestick_chart,
            "line": self.create_line_chart,
            "technical": self.create_technical_chart,
            "volume": self.create_volume_chart
        }

        method = chart_methods.get(type, self.create_candlestick_chart)
        result = method(symbol, range)

        if "error" in result:
            return {
                "plotly": None,
                "meta": {
                    "reason": result["error"]
                }
            }

        return result

    # ==================== DART 재무 지표 ====================

    def get_dart_metrics(self, ticker: str) -> Dict:
        """
        DART 재무제표 기반 추가 지표 (부채비율, 현금흐름 등)

        Returns:
            dict: debt_ratio, operating_cf, investing_cf, financing_cf 등
            실패 시: {"error": "사유"}
        """
        if not self._dart:
            return {"error": "DART API를 사용할 수 없습니다."}

        try:
            return self._dart.get_financial_summary(ticker)
        except Exception as e:
            return {"error": str(e)}

    # ==================== 통합 API ====================

    def get_chart_page_data(self, ticker: str, chart_period: str = "3m") -> Dict:
        """
        종목 차트 페이지에 필요한 모든 데이터 통합 조회

        웹 기획서 (1) 종목 차트 페이지 데이터:
        - 헤더: 회사명 + 티커
        - 개요 요약: 현재가, 등락
        - 차트: 기간별 주가 차트
        - 지표 카드: PER / PBR / ROE
        - 기술적 분석: RSI / 추세
        """
        result = {
            "ticker": ticker,
            "generated_at": datetime.now().isoformat()
        }

        # 1. 기본 정보 (헤더 + 개요)
        result["info"] = self.get_stock_info(ticker)

        # 2. 차트 데이터
        result["chart"] = self.get_chart_data(ticker, chart_period)

        # 3. 펀더멘탈 지표
        result["fundamentals"] = self.get_fundamental_metrics(ticker)

        # 4. 기술적 분석
        result["technical"] = self.get_technical_indicators(ticker)

        # 5. DART 재무 지표 (부채비율, 현금흐름)
        result["dart_metrics"] = self.get_dart_metrics(ticker)

        return result


# 테스트
if __name__ == "__main__":
    provider = StockChartDataProvider()

    # 삼성전자 테스트
    ticker = "005930"
    print(f"\n{'='*60}")
    print(f"종목 차트 페이지 데이터 테스트: {ticker}")
    print(f"{'='*60}")

    # 개별 테스트
    print("\n[1] 기본 정보:")
    info = provider.get_stock_info(ticker)
    print(f"  종목명: {info.get('company_name')}")
    print(f"  현재가: {info.get('current_price'):,.0f}원")
    print(f"  등락: {info.get('price_change'):+,.0f}원 ({info.get('change_rate'):+.2f}%)")

    print("\n[2] 펀더멘탈 지표:")
    metrics = provider.get_fundamental_metrics(ticker)
    if "error" not in metrics:
        print(f"  PER: {metrics.get('per')}")
        print(f"  PBR: {metrics.get('pbr')}")
        print(f"  ROE: {metrics.get('roe')}%")
        print(f"  EPS: {metrics.get('eps'):,.0f}원")
        print(f"  BPS: {metrics.get('bps'):,.0f}원")
        print(f"  52주 최고: {metrics.get('w52_high'):,}원")
        print(f"  52주 최저: {metrics.get('w52_low'):,}원")
        print(f"  시가총액: {metrics.get('market_cap'):,}억원")
    else:
        print(f"  에러: {metrics.get('error')}")

    print("\n[3] 기술적 분석:")
    tech = provider.get_technical_indicators(ticker)
    if "error" not in tech:
        print(f"  RSI: {tech['rsi']['value']} ({tech['rsi']['signal']['description']})")
        print(f"  MA5: {tech['moving_averages']['ma5']:,.0f}원")
        print(f"  MA20: {tech['moving_averages']['ma20']:,.0f}원")
        print(f"  MA60: {tech['moving_averages']['ma60']:,.0f}원")
        print(f"  추세: {tech['trend']['description']}")
    else:
        print(f"  에러: {tech.get('error')}")

    print("\n[4] 차트 데이터 (최근 5일):")
    chart = provider.get_chart_data(ticker, "1m")
    if "error" not in chart:
        print(f"  데이터 수: {chart['count']}일")
        for i in range(-5, 0):
            print(f"  {chart['data']['dates'][i]}: {chart['data']['close'][i]:,.0f}원")

    print("\n[5] 5분봉 데이터 (하루 탭용):")
    minute_chart = provider.get_minute_chart_data(ticker)
    if "error" not in minute_chart:
        print(f"  데이터 수: {minute_chart['count']}개")
        if minute_chart['count'] > 0:
            data = minute_chart['data']
            print(f"  첫 데이터: {data['dates'][0]} - {data['close'][0]:,}원")
            print(f"  마지막 데이터: {data['dates'][-1]} - {data['close'][-1]:,}원")
    else:
        print(f"  에러: {minute_chart.get('error')}")

    print("\n[6] Plotly 차트 생성 테스트:")
    if go is not None:
        # 캔들스틱 차트 (3개월)
        result = provider.get_chart_api(ticker, "3m", "candlestick")
        if "plotly" in result and result["plotly"]:
            print(f"  캔들스틱 차트 (3m): 생성 완료")
            print(f"    - MA: {result['meta'].get('ma', [])}")
            print(f"    - 생성시간: {result['meta'].get('generatedAt', '')[:19]}")
        else:
            print(f"  캔들스틱 차트: 실패 - {result.get('meta', {}).get('reason', '')}")

        # 라인 차트
        result = provider.get_chart_api(ticker, "1m", "line")
        if "plotly" in result and result["plotly"]:
            print(f"  라인 차트 (1m): 생성 완료")
        else:
            print(f"  라인 차트: 실패")

        # 기술적 분석 차트
        result = provider.get_chart_api(ticker, "3m", "technical")
        if "plotly" in result and result["plotly"]:
            print(f"  기술적 분석 차트: 생성 완료")
            print(f"    - 지표: {result['meta'].get('indicators', [])}")
        else:
            print(f"  기술적 분석 차트: 실패")

        # 거래량 차트
        result = provider.get_chart_api(ticker, "1m", "volume")
        if "plotly" in result and result["plotly"]:
            print(f"  거래량 차트: 생성 완료")
        else:
            print(f"  거래량 차트: 실패")
    else:
        print("  Plotly 미설치 - pip install plotly")

    print(f"\n{'='*60}")
    print("테스트 완료")
    print(f"{'='*60}")
