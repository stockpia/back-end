"""
Web_04 상세 리포트 + AI 비서 API
웹 기획에 맞춘 거래내역 상세 리포트 및 AI 비서 기능

기획:
- 3-패널 레이아웃: 6-1 종목 선택, 6-2 거래내역, 6-3 상세 리포트
- 기간 선택: 1달/3달/1년 버튼
- 범위 선택: 전체/종목별
- 설명 블록(narrative): LLM 서술형 분석
- AI 비서: 리포트 해설자 (용어 설명, 문장 해석, 거래 기준 설명)
"""

import os
from typing import Dict, List, Optional
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


class WebDetailReport:
    """
    Web_04 상세 리포트 데이터 프로바이더

    기능:
    - get_panel_stocks(): 6-1 종목 선택 패널 데이터
    - get_detail_report(): 6-3 상세 리포트 (요약 + 서술 블록)
    - ask_ai_assistant(): AI 비서 질의응답
    - get_suggested_questions(): 추천 질문 목록
    """

    PERIODS = {
        "1m": {"label": "1달", "months": 1},
        "3m": {"label": "3달", "months": 3},
        "1y": {"label": "1년", "months": 12},
    }

    def __init__(self):
        """Initialize"""
        try:
            from .HantuStock import HantuStock
        except ImportError:
            from HantuStock import HantuStock
        self.hantu = HantuStock()

        # Gemini (LLM) - narrative + AI 비서
        self.gemini_key = os.environ.get("GEMINI_API_KEY")
        if self.gemini_key:
            try:
                import google.genai as genai
                genai.configure(api_key=self.gemini_key)
                self.genai = genai
            except ImportError:
                self.genai = None
        else:
            self.genai = None

        # 용어 사전 (AI 비서용)
        try:
            from .glossary_api import GlossaryAPI
        except ImportError:
            from glossary_api import GlossaryAPI
        self.glossary = GlossaryAPI()

    # ========================================
    # 6-1: 종목 선택 패널
    # ========================================

    def get_panel_stocks(self, period: str = "1m") -> Dict:
        """
        6-1 종목 선택 패널 데이터

        거래내역이 있는 종목만 자동 구성

        Args:
            period: 조회 기간 ("1m", "3m", "1y")

        Returns:
            {
                "panel_stocks": [
                    {"label": "전체", "value": "ALL", "is_default": true},
                    {"label": "삼성전자", "value": "005930", "ticker": "005930"},
                    ...
                ]
            }
        """
        transactions = self.hantu.get_transaction_history(period=period)

        # 매수 거래가 있는 종목만
        stocks = {}
        for t in transactions:
            if t["sll_buy_dvsn_cd"] != "02":
                continue
            pdno = t["pdno"]
            if pdno not in stocks:
                stocks[pdno] = t["prdt_name"]

        panel = [{"label": "전체", "value": "ALL", "is_default": True}]
        for ticker, name in stocks.items():
            panel.append({
                "label": name,
                "value": ticker,
                "ticker": ticker,
            })

        return {"panel_stocks": panel}

    # ========================================
    # 6-3: 상세 리포트
    # ========================================

    def get_detail_report(self, scope: str = "ALL", period: str = "1m") -> Dict:
        """
        6-3 상세 리포트

        Args:
            scope: 범위 ("ALL" 또는 종목코드)
            period: 기간 ("1m", "3m", "1y")

        Returns:
            {
                "scope": "ALL",
                "period": "1m",
                "actual_period_days": 30,
                "summary_metrics": {...},
                "by_stock_summary": [...],
                "narrative": {...},
                "period_insufficient": false,
                "period_insufficient_message": null
            }
        """
        # 1단계: 거래내역 조회
        transactions = self.hantu.get_transaction_history(period=period)

        # 2단계: 범위 필터링
        if scope != "ALL":
            transactions = [t for t in transactions if t["pdno"] == scope]

        if not transactions:
            return self._empty_report(scope, period)

        # 3단계: 실제 기간 계산
        dates = sorted([t["ord_dt"] for t in transactions])
        first_date = datetime.strptime(dates[0], "%Y%m%d")
        last_date = datetime.strptime(dates[-1], "%Y%m%d")
        actual_days = (last_date - first_date).days + 1

        period_months = self.PERIODS.get(period, {}).get("months", 1)
        expected_days = period_months * 30
        period_insufficient = actual_days < expected_days

        # 4단계: 요약 메트릭 계산
        summary_metrics = self._calculate_metrics(transactions)

        # 5단계: 종목별 요약
        by_stock_summary = self._calculate_by_stock(transactions)

        # 6단계: 보유 종목 평가손익 반영
        eval_profit = self._get_eval_profit(scope)
        summary_metrics["eval_profit"] = eval_profit
        summary_metrics["total_profit"] = summary_metrics["realized_profit"] + eval_profit
        if summary_metrics["total_buy_amount"] > 0:
            summary_metrics["total_profit_rate"] = round(
                summary_metrics["total_profit"] / summary_metrics["total_buy_amount"] * 100, 1
            )
        else:
            summary_metrics["total_profit_rate"] = 0

        # 7단계: narrative 생성 (LLM)
        narrative = self._generate_narrative(
            transactions, summary_metrics, by_stock_summary, actual_days, period
        )

        # 8단계: 기획서 분석 블록
        trading_tendency = self._analyze_trading_tendency(transactions)
        frequency_change = self._analyze_frequency_change(transactions, scope, period)
        water_down_pattern = self._analyze_water_down_pattern(transactions)
        concentration_analysis = self._analyze_concentration(
            transactions, summary_metrics["total_buy_amount"]
        )
        avg_investment = (
            summary_metrics["total_buy_amount"] / max(summary_metrics["buy_trades"], 1)
        )
        volatility_analysis = self._analyze_volatility(transactions, avg_investment)
        risk_observation = self._build_risk_observation(
            trading_tendency, concentration_analysis, volatility_analysis
        )

        # 기간 부족 메시지
        period_msg = None
        if period_insufficient:
            period_msg = (
                f"선택하신 기간 동안의 거래내역이 충분하지 않아\n"
                f"현재 거래내역 {actual_days}일을 기준으로 리포트를 표시했어요.\n\n"
                f"자세한 거래내역은 상단의 거래내역을 스크롤하여 확인하세요 !"
            )

        return {
            "scope": scope,
            "period": period,
            "actual_period_days": actual_days,
            "summary_metrics": summary_metrics,
            "by_stock_summary": by_stock_summary,
            "trading_tendency": trading_tendency,
            "frequency_change": frequency_change,
            "water_down_pattern": water_down_pattern,
            "concentration_analysis": concentration_analysis,
            "volatility_analysis": volatility_analysis,
            "risk_observation": risk_observation,
            "narrative": narrative,
            "period_insufficient": period_insufficient,
            "period_insufficient_message": period_msg,
        }

    def _calculate_metrics(self, transactions: List[Dict]) -> Dict:
        """전체 요약 메트릭 계산"""
        total_buy = 0
        total_sell = 0
        buy_count = 0
        sell_count = 0

        for t in transactions:
            amt = t["tot_ccld_amt"]
            if t["sll_buy_dvsn_cd"] == "02":
                total_buy += amt
                buy_count += 1
            else:
                total_sell += amt
                sell_count += 1

        realized_profit = total_sell - total_buy if total_sell > 0 else 0

        return {
            "total_buy_amount": total_buy,
            "total_sell_amount": total_sell,
            "realized_profit": realized_profit,
            "buy_trades": buy_count,
            "sell_trades": sell_count,
            "total_trades": len(transactions),
        }

    def _calculate_by_stock(self, transactions: List[Dict]) -> List[Dict]:
        """종목별 요약 계산"""
        stocks = {}
        for t in transactions:
            pdno = t["pdno"]
            if pdno not in stocks:
                stocks[pdno] = {
                    "ticker": pdno,
                    "name": t["prdt_name"],
                    "buy_amount": 0,
                    "sell_amount": 0,
                    "buy_qty": 0,
                    "sell_qty": 0,
                }

            if t["sll_buy_dvsn_cd"] == "02":
                stocks[pdno]["buy_amount"] += t["tot_ccld_amt"]
                stocks[pdno]["buy_qty"] += t["tot_ccld_qty"]
            else:
                stocks[pdno]["sell_amount"] += t["tot_ccld_amt"]
                stocks[pdno]["sell_qty"] += t["tot_ccld_qty"]

        result = []
        for pdno, data in stocks.items():
            if data["buy_amount"] > 0:
                profit = data["sell_amount"] - data["buy_amount"]
                rate = round(profit / data["buy_amount"] * 100, 1)
            else:
                profit = data["sell_amount"]
                rate = 0

            data["realized_profit"] = profit
            data["profit_rate"] = rate
            result.append(data)

        # 수익률 내림차순 정렬
        result.sort(key=lambda x: x["profit_rate"], reverse=True)

        # top/bottom 마킹
        if len(result) >= 2:
            result[0]["is_top"] = True
            result[0]["is_bottom"] = False
            result[-1]["is_top"] = False
            result[-1]["is_bottom"] = True
            for r in result[1:-1]:
                r["is_top"] = False
                r["is_bottom"] = False
        elif len(result) == 1:
            result[0]["is_top"] = False
            result[0]["is_bottom"] = False

        return result

    def _get_eval_profit(self, scope: str) -> float:
        """보유 종목 평가손익 조회"""
        try:
            holdings = self.hantu.get_holding_stock_detail()
            if scope == "ALL":
                return sum(h.get("evlu_pfls_amt", 0) for h in holdings)
            else:
                for h in holdings:
                    if h["pdno"] == scope:
                        return h.get("evlu_pfls_amt", 0)
                return 0
        except Exception:
            return 0

    def _generate_narrative(
        self,
        transactions: List[Dict],
        metrics: Dict,
        by_stock: List[Dict],
        period_days: int,
        period: str,
    ) -> Dict:
        """
        서술형 분석 블록 생성 (LLM)

        기획서 narrative 구조:
        - flow: 거래 흐름
        - pattern: 매매 패턴
        - risk_point: 리스크 포인트
        - observation: 추가 관찰
        """
        if not self.genai:
            return self._fallback_narrative(metrics, by_stock)

        # 데이터 요약
        period_label = self.PERIODS.get(period, {}).get("label", "1달")
        stock_names = [s["name"] for s in by_stock]
        top_stock = by_stock[0]["name"] if by_stock else "N/A"

        trade_info = f"""기간: 최근 {period_label} ({period_days}일)
거래 종목: {', '.join(stock_names)}
총 거래 횟수: {metrics['total_trades']}회 (매수 {metrics['buy_trades']}회, 매도 {metrics['sell_trades']}회)
총 매수금액: {metrics['total_buy_amount']:,.0f}원
총 매도금액: {metrics['total_sell_amount']:,.0f}원
실현손익: {metrics['realized_profit']:+,.0f}원
평가손익: {metrics.get('eval_profit', 0):+,.0f}원"""

        for s in by_stock:
            trade_info += f"\n  {s['name']}: 매수 {s['buy_amount']:,.0f}원 / 매도 {s['sell_amount']:,.0f}원 / 손익률 {s['profit_rate']}%"

        prompt = f"""다음은 투자자의 최근 {period_label}간 거래내역 요약입니다.

{trade_info}

위 데이터를 기반으로 아래 4가지 서술형 분석을 각각 1-2문장으로 작성해주세요.

[flow] 거래 흐름: 어떤 종목에 집중했고 어떤 패턴으로 거래했는지
[pattern] 매매 패턴: 분할매수, 매도비중, 거래빈도 등 특징
[risk_point] 리스크 포인트: 현재 포지션의 주의점
[observation] 추가 관찰: 그 외 특징적인 부분

조건:
- "~습니다" 정중한 말투
- 판단/추천이 아닌 관찰 + 설명만
- 각 항목 2문장 이내
- 매수/매도 추천 금지
- 인사말 없이 바로 작성"""

        try:
            model = self.genai.GenerativeModel(
                'gemini-2.5-flash',
                system_instruction="당신은 한국 주식시장 거래내역 분석가입니다. 숫자와 데이터에 기반하여 관찰된 패턴만 설명합니다. 매수/매도 추천은 하지 않습니다."
            )
            response = model.generate_content(
                prompt,
                generation_config={"temperature": 0.3, "max_output_tokens": 1024}
            )
            return self._parse_narrative(response.text.strip())
        except Exception:
            return self._fallback_narrative(metrics, by_stock)

    def _parse_narrative(self, text: str) -> Dict:
        """narrative 응답 파싱"""
        import re

        result = {"flow": "", "pattern": "", "risk_point": "", "observation": ""}

        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)

        current_key = None
        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue

            lower = line.lower()
            if '[flow]' in lower or '거래 흐름' in line:
                current_key = "flow"
                line = re.sub(r'\[flow\]\s*', '', line, flags=re.IGNORECASE)
                line = re.sub(r'거래\s*흐름\s*[:：]?\s*', '', line)
            elif '[pattern]' in lower or '매매 패턴' in line:
                current_key = "pattern"
                line = re.sub(r'\[pattern\]\s*', '', line, flags=re.IGNORECASE)
                line = re.sub(r'매매\s*패턴\s*[:：]?\s*', '', line)
            elif '[risk_point]' in lower or '리스크' in line:
                current_key = "risk_point"
                line = re.sub(r'\[risk_point\]\s*', '', line, flags=re.IGNORECASE)
                line = re.sub(r'리스크\s*포인트\s*[:：]?\s*', '', line)
            elif '[observation]' in lower or '추가 관찰' in line:
                current_key = "observation"
                line = re.sub(r'\[observation\]\s*', '', line, flags=re.IGNORECASE)
                line = re.sub(r'추가\s*관찰\s*[:：]?\s*', '', line)

            line = line.strip(' -:：')
            if current_key and line:
                if result[current_key]:
                    result[current_key] += " " + line
                else:
                    result[current_key] = line

        if not result["flow"]:
            return self._fallback_narrative(None, None)

        return result

    def _fallback_narrative(self, metrics: Optional[Dict], by_stock: Optional[List]) -> Dict:
        """LLM 실패 시 기본 narrative"""
        if metrics and by_stock:
            stock_count = len(by_stock)
            return {
                "flow": f"최근 기간 동안 {stock_count}개 종목에 투자하는 패턴을 보였습니다.",
                "pattern": "거래 패턴을 분석하여 매매 특성을 파악 중입니다.",
                "risk_point": "매도 비중과 실현 수익을 함께 확인하는 것이 좋습니다.",
                "observation": "거래 횟수 대비 매수 비중을 확인하여 추가 매수 여력을 점검해보세요.",
            }

        return {
            "flow": "거래 데이터를 분석 중입니다.",
            "pattern": "매매 패턴을 확인 중입니다.",
            "risk_point": "거래내역 데이터를 기반으로 리스크를 점검 중입니다.",
            "observation": "추가 분석이 필요합니다.",
        }

    # ========================================
    # 기획서 분석 블록 (Section 6 [6-3])
    # ========================================

    def _analyze_trading_tendency(self, transactions: List[Dict]) -> Optional[Dict]:
        """
        거래 성향 분석 블록
        평균 보유일 = (각 매도 거래의 보유일 합) / 매도 건수
        구간: ≤7일 단기 / 8~29일 중기 / ≥30일 장기
        """
        sell_txs = [t for t in transactions if t["sll_buy_dvsn_cd"] == "01"]
        if not sell_txs:
            return None

        total_days = 0
        count = 0
        for sell in sell_txs:
            ticker = sell["pdno"]
            sell_date = datetime.strptime(sell["ord_dt"], "%Y%m%d")
            prev_buys = [
                t for t in transactions
                if t["pdno"] == ticker
                and t["sll_buy_dvsn_cd"] == "02"
                and datetime.strptime(t["ord_dt"], "%Y%m%d") <= sell_date
            ]
            if prev_buys:
                latest_buy = max(prev_buys, key=lambda x: x["ord_dt"])
                buy_date = datetime.strptime(latest_buy["ord_dt"], "%Y%m%d")
                total_days += (sell_date - buy_date).days
                count += 1

        if count == 0:
            return None

        avg_days = round(total_days / count, 1)
        if avg_days <= 7:
            category, range_str = "단기 매매 성향", "7일 이하"
        elif avg_days <= 29:
            category, range_str = "중기 매매 성향", "8~29일"
        else:
            category, range_str = "장기 보유 성향", "30일 이상"

        return {
            "avg_holding_days": avg_days,
            "category": category,
            "category_range": range_str,
            "text": f"평균 보유 기간은 {avg_days}일입니다.\n\n이는 '{category} ({range_str})' 구간에 해당합니다.",
        }

    def _analyze_frequency_change(self, transactions: List[Dict], scope: str, period: str) -> Optional[Dict]:
        """
        매매 빈도 변화 블록 (이전 기간 비교)
        출력 조건: 절대 변화율 ≥ 20%
        """
        from dateutil.relativedelta import relativedelta

        period_months = self.PERIODS.get(period, {}).get("months", 1)
        now = datetime.now()
        curr_start = now - relativedelta(months=period_months)
        prev_end = curr_start
        prev_start = curr_start - relativedelta(months=period_months)

        try:
            prev_all = self.hantu.get_transaction_history(
                start_date=prev_start.strftime("%Y%m%d"),
                end_date=prev_end.strftime("%Y%m%d"),
            )
        except Exception:
            return None

        if scope != "ALL":
            prev_all = [t for t in prev_all if t["pdno"] == scope]

        prev_count = len(prev_all)
        curr_count = len(transactions)

        if prev_count == 0:
            return None

        change_rate = (curr_count - prev_count) / prev_count
        if abs(change_rate) < 0.2:
            return None

        direction = "증가" if change_rate > 0 else "감소"
        change_pct = abs(round(change_rate * 100))

        return {
            "prev_count": prev_count,
            "curr_count": curr_count,
            "change_rate": round(change_rate, 2),
            "direction": direction,
            "text": (
                f"이전 기간 거래 횟수는 {prev_count}회,\n"
                f"이번 기간 거래 횟수는 {curr_count}회입니다.\n\n"
                f"거래 횟수는 {change_pct}% {direction}했습니다."
            ),
        }

    def _analyze_water_down_pattern(self, transactions: List[Dict]) -> Optional[Dict]:
        """
        물타기 패턴 분석 블록 (조건 충족 시만 출력)
        조건: 동일 종목 매수 ≥ 3회 AND 이후 매수가 < 최초 평균 매수가
        """
        buy_by_stock: Dict[str, Dict] = {}
        for t in transactions:
            if t["sll_buy_dvsn_cd"] != "02":
                continue
            pdno = t["pdno"]
            if pdno not in buy_by_stock:
                buy_by_stock[pdno] = {"name": t["prdt_name"], "buys": []}
            buy_by_stock[pdno]["buys"].append({
                "date": t["ord_dt"],
                "price": t["avg_prvs"],
                "qty": t["tot_ccld_qty"],
            })

        for ticker, data in buy_by_stock.items():
            buys = sorted(data["buys"], key=lambda x: x["date"])
            if len(buys) < 3:
                continue

            first_avg_price = buys[0]["price"]
            subsequent = buys[1:]
            if all(b["price"] < first_avg_price for b in subsequent):
                prices = [b["price"] for b in subsequent]
                price_str = ", ".join(f"{p:,.0f}" for p in prices)
                return {
                    "ticker": ticker,
                    "name": data["name"],
                    "buy_count": len(buys),
                    "first_avg_price": first_avg_price,
                    "subsequent_prices": prices,
                    "text": (
                        f"{data['name']}을(를) 총 {len(buys)}회 매수했습니다.\n\n"
                        f"최초 평균 매수 가격은 {first_avg_price:,.0f}원이었으며,\n"
                        f"이후 매수 가격은 {price_str}원입니다.\n\n"
                        f"하락 구간에서 추가 매수가 이루어진 기록이 확인됩니다.\n"
                        f"이러한 방식은 일반적으로 '물타기 전략'으로 분류됩니다."
                    ),
                }
        return None

    def _analyze_concentration(self, transactions: List[Dict], total_buy_amount: float) -> Optional[Dict]:
        """
        종목 집중도 분석 블록
        상위 1종목 비중 = 해당 종목 총 매수 금액 / 전체 매수 금액
        구간: 0~30% 분산 / 30~50% 중간 / 50~70% 높은 / ≥70% 매우 높은
        """
        if total_buy_amount == 0:
            return None

        buy_by_stock: Dict[str, Dict] = {}
        for t in transactions:
            if t["sll_buy_dvsn_cd"] != "02":
                continue
            pdno = t["pdno"]
            if pdno not in buy_by_stock:
                buy_by_stock[pdno] = {"name": t["prdt_name"], "amount": 0.0}
            buy_by_stock[pdno]["amount"] += t["tot_ccld_amt"]

        if len(buy_by_stock) <= 1:
            return None

        top = max(buy_by_stock.values(), key=lambda x: x["amount"])
        ratio = round(top["amount"] / total_buy_amount * 100)

        if ratio < 30:
            category, range_str = "분산 구조", "0~30%"
        elif ratio < 50:
            category, range_str = "중간 집중 구조", "30~50%"
        elif ratio < 70:
            category, range_str = "높은 집중 구조", "50~70%"
        else:
            category, range_str = "매우 높은 집중 구조", "70% 이상"

        return {
            "top_stock_name": top["name"],
            "top_stock_ratio": ratio,
            "category": category,
            "category_range": range_str,
            "text": (
                f"전체 매수 금액 중\n"
                f"{top['name']}가 차지하는 비중은 {ratio}%입니다.\n\n"
                f"이는 '{category} ({range_str})' 구간에 해당합니다."
            ),
        }

    def _analyze_volatility(self, transactions: List[Dict], avg_investment: float) -> Optional[Dict]:
        """
        손익 변동성 분석 블록
        변동률 = (기간 중 최대 손익 - 최소 손익) / 평균 투자금
        구간: <5% 낮은 / 5~15% 중간 / ≥15% 높은
        """
        if avg_investment == 0:
            return None

        sorted_tx = sorted(transactions, key=lambda x: x["ord_dt"])
        cumulative = 0.0
        profits = []
        for t in sorted_tx:
            amt = t["tot_ccld_amt"]
            if t["sll_buy_dvsn_cd"] == "02":
                cumulative -= amt
            else:
                cumulative += amt
            profits.append(cumulative)

        if not profits:
            return None

        volatility = round((max(profits) - min(profits)) / avg_investment * 100, 1)

        if volatility < 5:
            category, range_str = "낮은 변동 구간", "5% 미만"
        elif volatility < 15:
            category, range_str = "중간 변동 구간", "5~15%"
        else:
            category, range_str = "높은 변동 구간", "15% 이상"

        return {
            "max_profit": max(profits),
            "min_profit": min(profits),
            "volatility_rate": volatility,
            "category": category,
            "category_range": range_str,
            "text": (
                f"기간 중 최대 손익 변동폭은 {volatility}%입니다.\n\n"
                f"이는 '{category} ({range_str})'에 해당합니다."
            ),
        }

    def _build_risk_observation(
        self,
        tendency: Optional[Dict],
        concentration: Optional[Dict],
        volatility: Optional[Dict],
    ) -> Optional[Dict]:
        """
        리스크 관찰 포인트 블록
        구간 기준 초과 항목만 출력 ("위험하다" 표현 금지, 구조 설명)
        """
        items = []
        if tendency and tendency["category"] == "단기 매매 성향":
            items.append(f"평균 보유일은 {tendency['avg_holding_days']}일로 단기 구간에 해당합니다.")
        if concentration and concentration["top_stock_ratio"] >= 50:
            items.append(
                f"상위 종목 비중은 {concentration['top_stock_ratio']}%로 {concentration['category']}입니다."
            )
        if volatility and volatility["volatility_rate"] >= 15:
            items.append(f"손익 변동폭은 {volatility['volatility_rate']}%로 높은 변동 구간에 해당합니다.")

        if not items:
            return None

        text = " ".join(items)
        text += "\n\n이 구조에서는 가격 변동이 손익에 직접적으로 반영될 가능성이 있습니다."
        return {"items": items, "text": text}

    def _empty_report(self, scope: str, period: str) -> Dict:
        """거래내역 없음 시 빈 리포트"""
        return {
            "scope": scope,
            "period": period,
            "actual_period_days": 0,
            "summary_metrics": {
                "total_buy_amount": 0,
                "total_sell_amount": 0,
                "realized_profit": 0,
                "eval_profit": 0,
                "total_profit": 0,
                "total_profit_rate": 0,
                "buy_trades": 0,
                "sell_trades": 0,
                "total_trades": 0,
            },
            "by_stock_summary": [],
            "trading_tendency": None,
            "frequency_change": None,
            "water_down_pattern": None,
            "concentration_analysis": None,
            "volatility_analysis": None,
            "risk_observation": None,
            "narrative": None,
            "period_insufficient": True,
            "period_insufficient_message": "선택하신 기간 동안의 거래내역이 없어요.",
        }

    # ========================================
    # AI 비서
    # ========================================

    def ask_ai_assistant(self, question: str, context: Dict) -> Dict:
        """
        AI 비서 질의응답

        역할: 리포트 해설자 (투자 판단 X)
        참조: 리포트 데이터 + 용어 사전

        Args:
            question: 사용자 질문
            context: {
                "scope": "ALL",
                "period": "1m",
                "report_data": {
                    "summary_metrics": {...},
                    "narrative": {...}
                },
                "transaction_summary": {...}
            }

        Returns:
            {
                "answer": "실현손익은 ...",
                "source": "glossary + report_context",
                "related_terms": ["평가손익", "매도"]
            }
        """
        # 1단계: 용어 사전 검색
        glossary_info = self._search_glossary(question)

        # 2단계: 컨텍스트 구성
        report_context = self._build_assistant_context(context)

        # 3단계: AI 답변 생성
        if not self.genai:
            return self._fallback_assistant_answer(question, glossary_info)

        scope = context.get("scope", "ALL")
        period = context.get("period", "1m")
        period_label = self.PERIODS.get(period, {}).get("label", "1달")
        scope_label = "전체" if scope == "ALL" else scope

        prompt = f"""당신은 주토피아 AI 비서입니다.
사용자의 상세 리포트({scope_label} · 최근 {period_label} 기준)에 대한 질문에 답변해주세요.

[리포트 데이터]
{report_context}

[용어 사전 참조]
{glossary_info.get('text', '해당 용어 없음')}

[사용자 질문]
{question}

[답변 규칙]
1. 용어 설명 질문: 한 줄 쉬운 정의 → 현재 리포트에서의 의미 → 이번 거래에서의 적용
2. 문장 해석 질문: 더 쉬운 표현 → 의미하는 거래 패턴
3. 내 거래 기준: 판단이 아닌 관찰된 패턴 + 설명
4. 투자 판단/매수매도 추천 절대 금지
5. "~에요", "~있어요" 친근한 말투
6. 답변 3-5문장 이내"""

        try:
            model = self.genai.GenerativeModel(
                'gemini-2.5-flash',
                system_instruction="당신은 주토피아 AI 비서입니다. 상세 리포트의 '해설자' 역할을 합니다. 새로운 리포트 생성, 투자 판단, 매수/매도 추천, 외부 시황 예측은 하지 않습니다. 리포트 데이터와 용어 사전만을 참조하여 답변합니다."
            )
            response = model.generate_content(
                prompt,
                generation_config={"temperature": 0.3, "max_output_tokens": 1024}
            )

            import re
            answer = re.sub(r'\*\*(.*?)\*\*', r'\1', response.text.strip())

            source = "report_context"
            if glossary_info.get("terms"):
                source = "glossary + report_context"

            return {
                "answer": answer,
                "source": source,
                "related_terms": glossary_info.get("related", []),
            }
        except Exception:
            return self._fallback_assistant_answer(question, glossary_info)

    def _search_glossary(self, question: str) -> Dict:
        """질문에서 용어 검색"""
        # 질문에서 용어 추출 시도
        similar = self.glossary.find_similar(question, limit=3)

        if not similar:
            return {"text": "해당 용어 없음", "terms": [], "related": []}

        texts = []
        terms = []
        related = []

        for s in similar:
            entry = self.glossary.lookup(s["term"])
            if entry:
                terms.append(entry["term"])
                texts.append(
                    f"[{entry['term']}] {entry.get('full_name', '')}: {entry.get('description', '')}"
                )
                for rt in entry.get("related_terms", [])[:3]:
                    if rt not in related:
                        related.append(rt)

        return {
            "text": "\n".join(texts) if texts else "해당 용어 없음",
            "terms": terms,
            "related": related[:5],
        }

    def _build_assistant_context(self, context: Dict) -> str:
        """AI 비서용 컨텍스트 텍스트"""
        report = context.get("report_data", {})
        metrics = report.get("summary_metrics", {})
        narrative = report.get("narrative", {})
        tx_summary = context.get("transaction_summary", {})

        lines = []

        if metrics:
            lines.append(f"총 매수금액: {metrics.get('total_buy_amount', 0):,.0f}원")
            lines.append(f"총 매도금액: {metrics.get('total_sell_amount', 0):,.0f}원")
            lines.append(f"실현손익: {metrics.get('realized_profit', 0):+,.0f}원")
            lines.append(f"평가손익: {metrics.get('eval_profit', 0):+,.0f}원")
            lines.append(f"총 손익: {metrics.get('total_profit', 0):+,.0f}원")

        if narrative:
            lines.append(f"\n거래 흐름: {narrative.get('flow', '')}")
            lines.append(f"매매 패턴: {narrative.get('pattern', '')}")
            lines.append(f"리스크: {narrative.get('risk_point', '')}")

        return "\n".join(lines) if lines else "리포트 데이터 없음"

    def _fallback_assistant_answer(self, question: str, glossary_info: Dict) -> Dict:
        """LLM 실패 시 기본 답변"""
        if glossary_info.get("terms"):
            term = glossary_info["terms"][0]
            entry = self.glossary.lookup(term)
            if entry:
                answer = f"{entry.get('full_name', term)}은(는) {entry.get('description', '관련 용어예요.')}".rstrip()
                return {
                    "answer": answer,
                    "source": "glossary",
                    "related_terms": glossary_info.get("related", []),
                }

        return {
            "answer": "죄송해요, 지금은 답변을 준비하지 못했어요. 잠시 후 다시 질문해주세요.",
            "source": "fallback",
            "related_terms": [],
        }

    def get_suggested_questions(self) -> Dict:
        """
        추천 질문 목록 (AI 비서 초기 노출)
        """
        return {
            "suggested_questions": [
                "실현손익이랑 평가손익 차이가 뭐야?",
                "평균 보유일 기준은 어떻게 계산돼?",
                "종목 집중도 구간 기준 알려줘",
                "이번 기간 거래 횟수 변화 설명해줘",
                "손익 변동률 계산 방식이 뭐야?",
                "물타기 판단 기준은 뭐야?",
            ]
        }


# ========================================
# 테스트
# ========================================

if __name__ == "__main__":
    import json

    print("=" * 60)
    print("Web_04 상세 리포트 + AI 비서 테스트")
    print("=" * 60)
    print()

    web = WebDetailReport()

    # 1. 종목 선택 패널
    print("[1] 종목 선택 패널 (6-1)")
    print("-" * 40)
    panel = web.get_panel_stocks(period="1m")
    for stock in panel["panel_stocks"]:
        default = " (기본)" if stock.get("is_default") else ""
        print(f"  {stock['label']} ({stock['value']}){default}")
    print()

    # 2. 상세 리포트 - 전체
    print("[2] 상세 리포트 (6-3) - 전체 / 1달")
    print("-" * 40)
    report = web.get_detail_report(scope="ALL", period="1m")
    metrics = report["summary_metrics"]
    print(f"  기간: {report['actual_period_days']}일")
    print(f"  매수: {metrics['total_buy_amount']:,.0f}원 / 매도: {metrics['total_sell_amount']:,.0f}원")
    print(f"  실현손익: {metrics['realized_profit']:+,.0f}원")
    print(f"  평가손익: {metrics.get('eval_profit', 0):+,.0f}원")
    print(f"  기간 부족: {report['period_insufficient']}")

    if report.get("narrative"):
        print(f"\n  [narrative]")
        for key, value in report["narrative"].items():
            print(f"    {key}: {value[:60]}...")

    if report.get("by_stock_summary"):
        print(f"\n  [종목별 요약]")
        for s in report["by_stock_summary"]:
            print(f"    {s['name']}: 매수 {s['buy_amount']:,.0f}원 / 손익률 {s['profit_rate']}%")
    print()

    # 3. AI 비서
    print("[3] AI 비서 테스트")
    print("-" * 40)
    questions = [
        "실현손익이랑 평가손익 차이가 뭐야?",
        "이번 기간 리스크 포인트 알려줘",
    ]

    context = {
        "scope": "ALL",
        "period": "1m",
        "report_data": {
            "summary_metrics": metrics,
            "narrative": report.get("narrative", {}),
        },
    }

    for q in questions:
        print(f"\n  Q: {q}")
        answer = web.ask_ai_assistant(q, context)
        print(f"  A: {answer['answer'][:100]}...")
        print(f"  출처: {answer['source']}")
        if answer.get("related_terms"):
            print(f"  연관: {answer['related_terms']}")
    print()

    # 4. 추천 질문
    print("[4] 추천 질문")
    print("-" * 40)
    suggested = web.get_suggested_questions()
    for q in suggested["suggested_questions"]:
        print(f"  - {q}")
    print()

    print("=" * 60)
    print("테스트 완료")
    print("=" * 60)
