"""
Web_05 종목 리포트 API
웹 기획에 맞춘 종목 리포트 생성 및 관심 종목 관리

기획:
- Chatbot_02와 동일한 데이터 소스, 웹에서는 더 구조적이고 깊게 제공
- 5개 섹션: 투자 요약, 주가 동향, 재무 분석, 밸류에이션, 투자 의견
- 관심 종목 관리 (웹 ↔ 챗봇 실시간 동기화)
- 계좌 연동 없이 사용 가능
"""

import os
import json
from typing import Dict, List, Optional
from datetime import datetime
from dotenv import load_dotenv

from .stock_chart_data import StockChartDataProvider

load_dotenv()

class WebStockReport:
    """
    Web_05 종목 리포트 데이터 프로바이더

    기능:
    - get_report(): 전체 리포트 조회 (요약 + 5개 섹션)
    - add_favorite() / remove_favorite(): 관심 종목 관리
    - get_favorites(): 관심 종목 목록 조회
    """

    SECTIONS = {
        "investment_summary": "투자 요약",
        "price_trend": "주가 동향",
        "financial_analysis": "재무 분석",
        "valuation": "밸류에이션",
        "investment_opinion": "투자 의견",
    }

    FAVORITES_DIR = "./favorites"

    def __init__(self):
        """Initialize"""
        self.chart_provider = StockChartDataProvider()

        # Gemini (LLM)
        self.gemini_key = os.environ.get("GEMINI_API_KEY")
        if self.gemini_key:
            try:
                import google.generativeai as genai
                genai.configure(api_key=self.gemini_key)
                self.genai = genai
            except ImportError:
                self.genai = None
        else:
            self.genai = None

        # 관심 종목 디렉토리 생성
        os.makedirs(self.FAVORITES_DIR, exist_ok=True)

    # ========================================
    # 데이터 수집 (Chatbot_02와 동일)
    # ========================================

    def _collect_stock_data(self, symbol: str) -> Dict:
        """종목 데이터 수집 — 5개 데이터 소스(한투·FDR·RSI·수익률·DART) 병렬 호출."""
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {
                "info":        ex.submit(self.chart_provider.get_stock_info,           symbol),
                "fundamental": ex.submit(self.chart_provider.get_fundamental_metrics,  symbol),
                "technical":   ex.submit(self.chart_provider.get_technical_indicators, symbol),
                "returns":     ex.submit(self._calculate_returns,                       symbol),
                "dart":        ex.submit(self.chart_provider.get_dart_metrics,          symbol),
            }
            return {k: f.result() for k, f in futures.items()}

    def _calculate_returns(self, symbol: str) -> Dict:
        """기간별 수익률 계산 (chart_provider FDR 캐시 활용)"""
        try:
            df = self.chart_provider.get_historical_data(symbol)
            if df.empty:
                return {"error": "데이터 없음"}

            current = float(df['close'].iloc[-1])
            periods = {"1m": 21, "3m": 63, "1y": 252}
            returns = {}
            for key, days in periods.items():
                if len(df) > days:
                    past_price = float(df['close'].iloc[-days])
                    returns[key] = round((current - past_price) / past_price * 100, 1)
                else:
                    returns[key] = None
            return returns
        except Exception as e:
            return {"error": str(e)}

    # ========================================
    # LLM 텍스트 생성 (웹용 확장)
    # ========================================

    def _generate_llm_text(self, prompt: str, tone_guide: Optional[str] = None) -> Optional[str]:
        """LLM 텍스트 생성. OpenAI primary → Gemini fallback (llm_client 가 자동 처리).

        Args:
            prompt: 본 프롬프트.
            tone_guide: 투자성향 톤 가이드 (None 이면 무시). 본 프롬프트 앞에
                "[독자 성향]\\n<guide>\\n\\n" prefix 로 붙음.
        """
        from .llm_client import generate
        if tone_guide:
            prompt = f"[독자 성향]\n{tone_guide}\n\n{prompt}"
        # 섹션별 응답은 1~5문장이면 충분 → 1024 로 한도 낮춰 LLM 이 빠르게 종료
        return generate(prompt, temperature=0.3, max_output_tokens=1024)

    def _parse_opinion_response(self, raw: str) -> Dict:
        """
        투자의견 LLM 응답에서 JSON 추출 (LLM 이 코드 펜스/머리말을 붙여도 회수).
        실패 시 빈 dict 반환.
        """
        import json, re
        if not raw:
            return {}
        text = raw.strip()
        # ```json ... ``` 같은 마크다운 코드 펜스 제거
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        # 가장 바깥쪽 {...} 추출
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}

    def _clean_markdown(self, text: str) -> str:
        """마크다운 서식 제거"""
        import re
        cleaned = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
        cleaned = re.sub(r'\*(.*?)\*', r'\1', cleaned)
        return cleaned.strip()

    def _parse_bullet_lines(self, text: str) -> List[str]:
        """LLM 응답에서 bullet 포인트 추출"""
        import re
        lines = text.strip().split('\n')
        bullets = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            cleaned = re.sub(r'^[\•\*\-\·\■\□\▪\▸]\s*', '', line)
            cleaned = re.sub(r'^\d+[\.\)]\s*', '', cleaned)
            cleaned = re.sub(r'\*\*(.*?)\*\*', r'\1', cleaned)
            cleaned = cleaned.strip()
            # 빈 문자열이나 단독 특수문자 필터링
            if len(cleaned) < 3:
                continue
            if cleaned != line.strip() or line.startswith(('•', '*', '-', '·')):
                bullets.append(cleaned)
        return bullets

    # ========================================
    # 웹용 섹션 생성 (챗봇보다 상세)
    # ========================================

    def _build_investment_summary(self, data: Dict, company_name: str) -> Dict:
        """투자 요약 (웹 확장판)"""
        info = data.get("info", {})
        fundamental = data.get("fundamental", {})
        technical = data.get("technical", {})
        returns = data.get("returns", {})
        dart = data.get("dart", {})
        rsi_info = technical.get("rsi", {})

        # DART 데이터 라인
        dart_line = ""
        if "error" not in dart:
            parts = []
            if dart.get("debt_ratio") is not None:
                parts.append(f"부채비율: {dart['debt_ratio']:.1f}%")
            if dart.get("operating_cf") is not None:
                cf_sign = "+" if dart["operating_cf"] > 0 else ""
                parts.append(f"영업현금흐름: {cf_sign}{dart['operating_cf']:,.0f}원")
            if parts:
                dart_line = "\n" + " / ".join(parts)

        prompt = f"""{company_name}의 투자 데이터입니다.

현재가: {info.get('current_price', 'N/A')}원 ({info.get('price_change', 0):+}원)
PER: {fundamental.get('per', 'N/A')} / PBR: {fundamental.get('pbr', 'N/A')} / ROE: {fundamental.get('roe', 'N/A')}%
RSI: {rsi_info.get('value', 'N/A')} ({rsi_info.get('signal', {}).get('description', 'N/A')})
1개월: {returns.get('1m', 'N/A')}% / 3개월: {returns.get('3m', 'N/A')}% / 1년: {returns.get('1y', 'N/A')}%{dart_line}

웹 상세 리포트용으로 다음을 작성해주세요:
1. 이 종목에 대한 종합 투자 요약 (3~5줄, 자연스러운 문단 형태)
2. 핵심 포인트 3~5개를 bullet으로 작성
3. 주요 체크 포인트 1줄

형식:
[요약]
(종합 투자 요약 문단)

[포인트]
• 포인트1
• 포인트2
• 포인트3

[체크포인트]
✔️ 체크포인트 내용

조건:
- "~에요", "~있어요" 친근한 말투
- 매수/매도 추천 금지
- 인사말 없이 바로 작성
- 위에 제공된 데이터만 언급하세요. 데이터에 없는 지표는 언급하지 마세요."""

        result = self._generate_llm_text(prompt, tone_guide=data.get('_tone_guide'))

        full_text = ""
        key_points = []
        checkpoint = ""

        if result:
            sections = result.split('[포인트]')
            if len(sections) >= 2:
                summary_part = sections[0].replace('[요약]', '').strip()
                full_text = self._clean_markdown(summary_part)

                rest = sections[1]
                checkpoint_parts = rest.split('[체크포인트]')
                key_points = self._parse_bullet_lines(checkpoint_parts[0])[:5]
                if len(checkpoint_parts) >= 2:
                    checkpoint = self._clean_markdown(checkpoint_parts[1]).replace('✔️', '').strip()
            else:
                # 형식이 안 맞으면 전체를 full_text로
                full_text = self._clean_markdown(result)
                key_points = self._parse_bullet_lines(result)[:5]

        if not full_text:
            full_text = f"{company_name}은(는) 현재 시장에서 관심을 받고 있는 종목이에요. 주요 지표를 확인하고 투자 판단에 참고해보세요."
            key_points = [
                "최근 시장에서 관심을 받고 있는 종목이에요",
                "주요 지표를 확인하고 투자 판단에 참고해보세요",
                "자세한 내용은 각 섹션에서 확인할 수 있어요"
            ]
            checkpoint = "실적 흐름과 시장 환경 변화를 함께 살펴보세요."

        return {
            "full_text": full_text,
            "key_points": key_points,
            "checkpoint": checkpoint,
        }

    def _build_price_trend(self, data: Dict, company_name: str) -> Dict:
        """주가 동향"""
        returns = data.get("returns", {})
        technical = data.get("technical", {})
        rsi_info = technical.get("rsi", {})
        rsi_signal = rsi_info.get("signal", {})
        trend = technical.get("trend", {})
        ma = technical.get("moving_averages", {})

        rsi_desc = rsi_signal.get("description", "데이터 없음") if isinstance(rsi_signal, dict) else str(rsi_signal)
        rsi_value = rsi_info.get("value")

        if rsi_value and rsi_value >= 70:
            rsi_interpretation = "현재 과매수 구간에 위치해 있어 조정 가능성이 있어요."
        elif rsi_value and rsi_value <= 30:
            rsi_interpretation = "현재 과매도 구간에 위치해 있어 반등 가능성이 있어요."
        else:
            rsi_interpretation = "현재 과열도 침체도 아닌 중립 구간에 위치해 있어요."

        # 이동평균선 정배열/역배열
        ma5 = ma.get("ma5")
        ma20 = ma.get("ma20")
        ma60 = ma.get("ma60")
        if ma5 and ma20 and ma60:
            if ma5 > ma20 > ma60:
                ma_status = "정배열"
                ma_description = "단기·중기·장기 이동평균선이 정배열 상태로, 상승 추세가 유지되고 있어요."
            elif ma5 < ma20 < ma60:
                ma_status = "역배열"
                ma_description = "단기·중기·장기 이동평균선이 역배열 상태로, 하락 추세에 있어요."
            else:
                ma_status = "혼조"
                ma_description = "이동평균선이 혼조 상태로, 뚜렷한 추세가 형성되지 않았어요."
        else:
            ma_status = "데이터 없음"
            ma_description = "이동평균선 데이터를 확인 중이에요."

        return {
            "returns": {
                "1m": returns.get("1m"),
                "3m": returns.get("3m"),
                "1y": returns.get("1y"),
            },
            "technical": {
                "rsi": {
                    "value": rsi_value,
                    "signal": rsi_desc,
                    "interpretation": rsi_interpretation,
                },
                "moving_average": {
                    "ma5": ma5,
                    "ma20": ma20,
                    "ma60": ma60,
                    "status": ma_status,
                    "description": ma_description,
                },
                "trend": {
                    "description": trend.get("description", "데이터 없음") if isinstance(trend, dict) else str(trend),
                },
            },
        }

    def _build_financial_analysis(self, data: Dict, company_name: str) -> Dict:
        """재무 분석 (웹 확장판)"""
        fundamental = data.get("fundamental", {})
        dart = data.get("dart", {})

        # DART 재무 데이터 섹션
        dart_section = ""
        if "error" not in dart:
            dart_lines = []
            if dart.get("debt_ratio") is not None:
                dart_lines.append(f"부채비율: {dart['debt_ratio']:.1f}%")
            if dart.get("revenue") is not None:
                dart_lines.append(f"매출액: {dart['revenue']:,.0f}원")
            if dart.get("operating_margin") is not None:
                dart_lines.append(f"영업이익률: {dart['operating_margin']:.1f}%")
            if dart.get("operating_cf") is not None:
                dart_lines.append(f"영업활동 현금흐름: {dart['operating_cf']:,.0f}원")
            if dart.get("investing_cf") is not None:
                dart_lines.append(f"투자활동 현금흐름: {dart['investing_cf']:,.0f}원")
            if dart.get("financing_cf") is not None:
                dart_lines.append(f"재무활동 현금흐름: {dart['financing_cf']:,.0f}원")
            if dart_lines:
                dart_section = "\n" + "\n".join(dart_lines)
                dart_section += f"\n(출처: {dart.get('report_label', 'DART')})"

        prompt = f"""{company_name}의 재무 데이터입니다.
PER: {fundamental.get('per', 'N/A')} / PBR: {fundamental.get('pbr', 'N/A')}
EPS: {fundamental.get('eps', 'N/A')}원 / BPS: {fundamental.get('bps', 'N/A')}원
ROE: {fundamental.get('roe', 'N/A')}%{dart_section}

웹 상세 리포트용으로 다음을 작성해주세요:
1. 재무 상태에 대한 해석 (3~4줄, 자연스러운 문단)
2. 핵심 포인트 3~5개를 bullet으로

형식:
[해석]
(재무 해석 문단)

[포인트]
• 포인트1
• 포인트2
• 포인트3

조건:
- "~에요", "~있어요" 친근한 말투
- 매수/매도 추천 금지
- 인사말 없이 바로 작성
- 위에 제공된 데이터만 언급하세요. 데이터에 없는 지표는 언급하지 마세요."""

        result = self._generate_llm_text(prompt, tone_guide=data.get('_tone_guide'))

        interpretation = ""
        key_points = []

        if result:
            sections = result.split('[포인트]')
            if len(sections) >= 2:
                interpretation = self._clean_markdown(sections[0].replace('[해석]', '')).strip()
                key_points = self._parse_bullet_lines(sections[1])[:5]
            else:
                interpretation = self._clean_markdown(result)
                key_points = self._parse_bullet_lines(result)[:5]

        if not interpretation:
            interpretation = "재무 데이터를 기반으로 분석 중이에요. 주요 재무 지표를 참고해주세요."
            key_points = [
                "재무 데이터를 기반으로 분석 중이에요",
                "주요 재무 지표를 확인해보세요",
            ]

        # 기획서 스키마: revenue / operating_profit 시계열 리스트
        revenue_series = dart.get("revenue_series", []) if "error" not in dart else []
        op_income_series = dart.get("operating_income_series", []) if "error" not in dart else []
        net_income_series = dart.get("net_income_series", []) if "error" not in dart else []

        # 밸류에이션 지표 + DART 보조 지표
        dart_metrics = {}
        if "error" not in dart:
            for key in ("debt_ratio", "revenue", "operating_income", "operating_margin",
                        "operating_cf", "investing_cf", "financing_cf"):
                if dart.get(key) is not None:
                    dart_metrics[key] = dart[key]
            if dart.get("report_label"):
                dart_metrics["dart_report_label"] = dart["report_label"]

        return {
            "revenue": revenue_series,
            "operating_profit": op_income_series,
            "net_income": net_income_series,
            "metrics": {
                "per": fundamental.get("per", 0),
                "pbr": fundamental.get("pbr", 0),
                "eps": fundamental.get("eps", 0),
                "bps": fundamental.get("bps", 0),
                "roe": fundamental.get("roe", 0),
                **dart_metrics,
            },
            "interpretation": interpretation,
            "key_points": key_points,
        }

    def _build_valuation(self, data: Dict, company_name: str) -> Dict:
        """밸류에이션 (웹 확장판). 0 값은 N/A 로 정규화 (적자 종목/ETN 처리)."""
        fundamental = data.get("fundamental", {})

        # 0 또는 None 은 "미제공 (N/A)" 로 간주.
        # 음수는 의미 있는 값 (적자 PER -180 등) 이라 그대로 유지.
        def _normalize(v):
            if v is None or v == 0:
                return None
            return v

        per = _normalize(fundamental.get("per"))
        pbr = _normalize(fundamental.get("pbr"))
        roe = _normalize(fundamental.get("roe"))
        eps = _normalize(fundamental.get("eps"))

        # ETN/ETF 또는 회사명 lookup 실패 시 prompt 에 명시
        is_etn_like = company_name == data.get("info", {}).get("ticker", "")
        ticker_hint = f"(종목코드 {company_name}, 회사명 lookup 미제공)" if is_etn_like else ""

        def _fmt(v, unit=""):
            return "N/A (미제공)" if v is None else f"{v}{unit}"

        prompt = f"""{company_name} {ticker_hint} 밸류에이션 지표:
PER: {_fmt(per)} / PBR: {_fmt(pbr)} / ROE: {_fmt(roe, '%')} / EPS: {_fmt(eps, '원')}

웹 상세 리포트용 밸류에이션 해석 (3~4줄):
- N/A 인 지표는 "제공 안 됨" 으로 인지하고 해당 지표 기반 판단 하지 말 것.
  N/A 만 있으면 "이 종목은 밸류에이션 지표가 제공되지 않아 해석이 어려워요" 같이 안내.
- 음수 PER/ROE 는 적자 종목 의미 (저평가 X). "적자 상태" 로 해석.
- 업종 평균 대비 수준 언급 (가능한 경우)
- 매수/매도 추천 금지
- "~에요", "~있어요" 친근한 말투, 인사말 없이 해석만"""

        result = self._generate_llm_text(prompt, tone_guide=data.get('_tone_guide'))
        interpretation = ""
        if result:
            cleaned = self._clean_markdown(result)
            lines = [l.strip() for l in cleaned.split('\n') if l.strip()]
            filtered = [
                line for line in lines
                if not any(skip in line for skip in ['안녕하세요', '안녕!', '살펴볼게요', '함께 살펴'])
            ]
            interpretation = "\n".join(filtered) if filtered else cleaned

        if not interpretation:
            # fallback: 데이터 유무 기반
            if per is None and pbr is None and roe is None and eps is None:
                interpretation = "이 종목은 밸류에이션 지표가 제공되지 않아 해석이 어려워요. ETN/ETF 거나 재무 데이터가 충분치 않은 신규 종목일 수 있어요."
            elif per and per < 0:
                interpretation = "현재 적자 상태로 PER 이 음수에요. 실적 흑자 전환 여부를 함께 살펴보세요."
            elif per and per > 25:
                interpretation = "현재 주가는 다소 높은 수준으로 평가되고 있어요. 시장의 성장 기대감이 반영된 것으로 보여요."
            elif per and per < 10:
                interpretation = "현재 주가는 상대적으로 저평가 구간으로 보여요. 실적 대비 합리적인 수준이에요."
            else:
                interpretation = "현재 주가는 업종 평균 수준으로 해석돼요."

        # 응답에 None 그대로 (프론트에서 N/A 표시), 음수는 유지.
        return {
            "per": per,
            "pbr": pbr,
            "roe": roe,
            "eps": eps,
            "interpretation": interpretation,
        }

    def _build_investment_opinion(self, data: Dict, company_name: str) -> Dict:
        """투자 의견 (웹 확장판 - 장단점/관점 분리)"""
        info = data.get("info", {})
        fundamental = data.get("fundamental", {})
        technical = data.get("technical", {})
        returns = data.get("returns", {})
        dart = data.get("dart", {})
        rsi_info = technical.get("rsi", {})
        trend = technical.get("trend", {})

        # DART 데이터 라인
        dart_line = ""
        if "error" not in dart:
            parts = []
            if dart.get("debt_ratio") is not None:
                parts.append(f"부채비율: {dart['debt_ratio']:.1f}%")
            if dart.get("operating_cf") is not None:
                cf_sign = "+" if dart["operating_cf"] > 0 else ""
                parts.append(f"영업현금흐름: {cf_sign}{dart['operating_cf']:,.0f}원")
            if parts:
                dart_line = "\n" + " / ".join(parts)

        prompt = f"""{company_name} 종합 투자 데이터:
현재가: {info.get('current_price', 'N/A')}원
PER: {fundamental.get('per', 'N/A')} / ROE: {fundamental.get('roe', 'N/A')}%
RSI: {rsi_info.get('value', 'N/A')} / 추세: {trend.get('description', 'N/A') if isinstance(trend, dict) else trend}
1년 수익률: {returns.get('1y', 'N/A')}%{dart_line}

위 데이터로 웹 종합 투자 의견을 다음 JSON 형식으로만 응답하세요.
다른 텍스트(인사말/설명/마크다운 코드 펜스) 절대 포함 금지. 순수 JSON 만:

{{
  "pros": ["장점 한 줄", "장점 한 줄"],
  "cons": ["유의 한 줄", "유의 한 줄"],
  "checkpoints": ["체크 한 줄", "체크 한 줄"],
  "perspective": {{
    "short_term": "단기 관점 한 줄",
    "mid_term":   "중기 관점 한 줄",
    "long_term":  "장기 관점 한 줄"
  }}
}}

조건:
- "~에요", "~있어요" 친근한 말투
- 매수/매도 추천 금지, 정보 제공만
- 위에 제공된 데이터만 언급. 없는 지표는 언급 금지.
- pros/cons/checkpoints 는 각각 2~4개.
- 모든 문자열은 한국어, 1문장 (이모지·마크다운 X)."""

        result = self._generate_llm_text(prompt, tone_guide=data.get('_tone_guide'))

        pros, cons, checkpoints = [], [], []
        perspective = {"short_term": "", "mid_term": "", "long_term": ""}

        if result:
            parsed = self._parse_opinion_response(result)
            pros = parsed.get("pros") or []
            cons = parsed.get("cons") or []
            checkpoints = parsed.get("checkpoints") or []
            p = parsed.get("perspective") or {}
            if isinstance(p, dict):
                perspective["short_term"] = (p.get("short_term") or "").strip()
                perspective["mid_term"]   = (p.get("mid_term")   or "").strip()
                perspective["long_term"]  = (p.get("long_term")  or "").strip()

        # fallback
        if not pros:
            pros = ["주요 지표를 종합적으로 검토해보세요", "시장 환경과 함께 판단하는 것이 좋아요"]
        if not cons:
            cons = ["시장 변동성에 따른 리스크를 고려해야 해요"]
        if not checkpoints:
            checkpoints = ["실적 흐름과 시장 환경 변화를 주시해주세요"]
        if not perspective["short_term"]:
            perspective = {
                "short_term": "단기 변동성에 유의가 필요해요.",
                "mid_term": "실적 흐름을 확인하며 판단해보세요.",
                "long_term": "장기적 성장성을 기준으로 평가해보세요.",
            }

        return {
            "pros": pros,
            "cons": cons,
            "checkpoints": checkpoints,
            "perspective": perspective,
        }

    # ========================================
    # 메인 API: 전체 리포트 조회
    # ========================================

    # 투자성향 1-5 단계별 LLM 톤 가이드 (프론트 investmentProfile.ts 의 aiPrompt 와 정합)
    PROFILE_TONE_GUIDES = {
        1: "독자는 안정형 투자자입니다. 하방 방어력과 배당·실적 안정성을 강조하고, 변동성과 손실 가능성에 보수적 톤으로 경고하세요.",
        2: "독자는 안정추구형 투자자입니다. 우량주·대형주 중심으로 설명하고 급등주/테마주 관련 위험은 반드시 명시하세요.",
        3: "독자는 위험중립형 투자자입니다. 팩트 중심 분석가 톤으로 호재와 악재의 비중을 5:5로 균형있게 다루고 펀더멘털을 철저히 분석하세요.",
        4: "독자는 적극투자형 투자자입니다. 주도 섹터의 자금 이동과 실적 턴어라운드 모멘텀을 강조하되, 진입 시 리스크 관리도 짚어주세요.",
        5: "독자는 공격투자형 투자자입니다. 거래량 급증·신고가 돌파·강력한 공시 재료 위주로 행동주의적 톤으로 브리핑하되, 단기 변동성 경고는 유지하세요.",
    }

    def get_report(
        self,
        symbol: str,
        company_name: str,
        user_id: str = "default",
        profile_level: int = 3,
    ) -> Dict:
        """
        전체 종목 리포트 조회 (GET /api/web/stocks/{symbol}/report)

        Args:
            symbol: 종목코드
            company_name: 회사명
            user_id: 사용자 ID (관심 종목 확인용)
            profile_level: 투자성향 (1=안정형 .. 5=공격투자형). default=3 위험중립형.
                각 섹션의 LLM 프롬프트 prefix 에 톤 가이드로 주입.

        Returns:
            기획서 Section 6-1 응답 스키마에 맞는 JSON
        """
        data = self._collect_stock_data(symbol)
        info = data.get("info", {})

        if "error" in info:
            return {"error": info["error"]}

        fundamental = data.get("fundamental", {})
        returns = data.get("returns", {})
        technical = data.get("technical", {})
        rsi_info = technical.get("rsi", {})
        rsi_signal = rsi_info.get("signal", {})
        rsi_desc = rsi_signal.get("description", "데이터 없음") if isinstance(rsi_signal, dict) else str(rsi_signal)

        # 관심 종목 여부
        is_favorite = self._is_favorite(user_id, symbol)

        # 투자성향 톤 가이드 — 각 섹션 빌더에서 LLM 프롬프트 prefix 로 사용 가능하도록
        # 호출자 data dict 에 저장.
        data["_tone_guide"] = self.PROFILE_TONE_GUIDES.get(
            profile_level, self.PROFILE_TONE_GUIDES[3]
        )
        data["_profile_level"] = profile_level

        # 5개 섹션을 ThreadPool 로 병렬 생성 (직렬 5-15초 → 가장 느린 1개 시간)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {
                "investment_summary": ex.submit(self._build_investment_summary, data, company_name),
                "price_trend":        ex.submit(self._build_price_trend,        data, company_name),
                "financial_analysis": ex.submit(self._build_financial_analysis, data, company_name),
                "valuation":          ex.submit(self._build_valuation,          data, company_name),
                "investment_opinion": ex.submit(self._build_investment_opinion, data, company_name),
            }
            investment_summary = futures["investment_summary"].result()
            price_trend        = futures["price_trend"].result()
            financial_analysis = futures["financial_analysis"].result()
            valuation          = futures["valuation"].result()
            investment_opinion = futures["investment_opinion"].result()

        return {
            "symbol": symbol,
            "company_name": company_name,
            "generated_at": datetime.now().isoformat(),
            "is_favorite": is_favorite,
            "summary": {
                "investment_summary": investment_summary.get("full_text", ""),
                "current_price": info.get("current_price", 0),
                "price_change": info.get("price_change", 0),
                "price_change_pct": info.get("change_rate", 0),
                "return_1m": returns.get("1m"),
                "return_3m": returns.get("3m"),
                "return_1y": returns.get("1y"),
                # 0 은 "데이터 없음(N/A)" 으로 정규화. 음수(적자 PER 등)는 유지.
                # Python: -180 or None == -180, 0 or None == None
                "per": fundamental.get("per") or None,
                "pbr": fundamental.get("pbr") or None,
                "roe": fundamental.get("roe") or None,
                "rsi": rsi_desc,
            },
            "sections": {
                "investment_summary": investment_summary,
                "price_trend": price_trend,
                "financial_analysis": financial_analysis,
                "valuation": valuation,
                "investment_opinion": investment_opinion,
            },
        }

    # ========================================
    # 관심 종목 관리
    # ========================================

    def _get_favorites_path(self, user_id: str) -> str:
        """관심 종목 파일 경로"""
        return os.path.join(self.FAVORITES_DIR, f"{user_id}.json")

    def _load_favorites(self, user_id: str) -> List[Dict]:
        """관심 종목 목록 로드"""
        path = self._get_favorites_path(user_id)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return []

    def _save_favorites(self, user_id: str, favorites: List[Dict]):
        """관심 종목 목록 저장"""
        path = self._get_favorites_path(user_id)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(favorites, f, ensure_ascii=False, indent=2)

    def _is_favorite(self, user_id: str, symbol: str) -> bool:
        """관심 종목 여부 확인"""
        favorites = self._load_favorites(user_id)
        return any(f["symbol"] == symbol for f in favorites)

    def add_favorite(self, user_id: str, symbol: str, company_name: str) -> Dict:
        """
        관심 종목 추가 (POST /api/web/stocks/{symbol}/favorite)

        Returns:
            {"symbol": "005930", "is_favorite": true, "message": "..."}
        """
        favorites = self._load_favorites(user_id)

        # 이미 존재하면 중복 추가 방지
        if any(f["symbol"] == symbol for f in favorites):
            return {
                "symbol": symbol,
                "is_favorite": True,
                "message": "이미 관심 종목에 등록되어 있습니다",
            }

        favorites.append({
            "symbol": symbol,
            "company_name": company_name,
            "added_at": datetime.now().isoformat(),
        })
        self._save_favorites(user_id, favorites)

        return {
            "symbol": symbol,
            "is_favorite": True,
            "message": "관심 종목에 추가되었습니다",
        }

    def remove_favorite(self, user_id: str, symbol: str) -> Dict:
        """
        관심 종목 해제 (POST /api/web/stocks/{symbol}/favorite)

        Returns:
            {"symbol": "005930", "is_favorite": false, "message": "..."}
        """
        favorites = self._load_favorites(user_id)
        before_count = len(favorites)
        favorites = [f for f in favorites if f["symbol"] != symbol]
        self._save_favorites(user_id, favorites)

        removed = len(favorites) < before_count
        return {
            "symbol": symbol,
            "is_favorite": False,
            "message": "관심 종목에서 해제되었습니다" if removed else "관심 종목에 등록되어 있지 않습니다",
        }

    def get_favorites(self, user_id: str) -> Dict:
        """
        관심 종목 목록 조회 (GET /api/web/favorites)

        Returns:
            {"favorites": [...], "total_count": 2}
        """
        favorites = self._load_favorites(user_id)
        return {
            "favorites": favorites,
            "total_count": len(favorites),
        }


# ========================================
# 테스트
# ========================================

if __name__ == "__main__":
    print("=" * 60)
    print("Web_05 종목 리포트 테스트")
    print("=" * 60)
    print()

    web = WebStockReport()

    symbol = "005930"
    company = "삼성전자"
    user_id = "test_user"

    # 1. 전체 리포트 조회
    print("[1] 전체 리포트 조회")
    print("-" * 40)
    report = web.get_report(symbol, company, user_id)

    if "error" not in report:
        summary = report["summary"]
        print(f"종목: {report['company_name']} ({report['symbol']})")
        print(f"현재가: {summary['current_price']:,}원 ({summary['price_change']:+,}원)")
        print(f"수익률: 1M {summary['return_1m']}% / 3M {summary['return_3m']}% / 1Y {summary['return_1y']}%")
        print(f"PER {summary['per']} / PBR {summary['pbr']} / ROE {summary['roe']}")
        print(f"RSI: {summary['rsi']}")
        print(f"관심 종목: {report['is_favorite']}")
        print()

        # 섹션별 내용
        sections = report["sections"]

        print("[투자 요약]")
        inv = sections["investment_summary"]
        print(f"  본문: {inv['full_text'][:100]}...")
        print(f"  포인트: {inv['key_points'][:3]}")
        print()

        print("[주가 동향]")
        pt = sections["price_trend"]
        print(f"  수익률: {pt['returns']}")
        print(f"  RSI: {pt['technical']['rsi']['signal']}")
        print(f"  이동평균: {pt['technical']['moving_average']['status']}")
        print()

        print("[재무 분석]")
        fa = sections["financial_analysis"]
        print(f"  해석: {fa['interpretation'][:100]}...")
        print(f"  포인트: {fa['key_points'][:3]}")
        print()

        print("[밸류에이션]")
        val = sections["valuation"]
        print(f"  PER {val['per']} / PBR {val['pbr']} / ROE {val['roe']}")
        print(f"  해석: {val['interpretation'][:100]}...")
        print()

        print("[투자 의견]")
        op = sections["investment_opinion"]
        print(f"  장점: {op['pros'][:2]}")
        print(f"  유의: {op['cons'][:2]}")
        print(f"  관점: 단기={op['perspective']['short_term'][:30]}...")
        print()
    else:
        print(f"에러: {report['error']}")
    print()

    # 2. 관심 종목 관리
    print("[2] 관심 종목 관리")
    print("-" * 40)

    result = web.add_favorite(user_id, symbol, company)
    print(f"추가: {result}")

    result = web.add_favorite(user_id, "000660", "SK하이닉스")
    print(f"추가: {result}")

    favorites = web.get_favorites(user_id)
    print(f"목록: {favorites['total_count']}개 - {[f['company_name'] for f in favorites['favorites']]}")

    result = web.remove_favorite(user_id, symbol)
    print(f"해제: {result}")

    favorites = web.get_favorites(user_id)
    print(f"목록: {favorites['total_count']}개 - {[f['company_name'] for f in favorites['favorites']]}")
    print()

    print("=" * 60)
    print("테스트 완료")
    print("=" * 60)
