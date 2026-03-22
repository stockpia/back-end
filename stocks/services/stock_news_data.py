"""
Web_02 뉴스/커뮤니티 데이터 프로바이더
종목 상세 페이지 하단의 뉴스/커뮤니티 탭 데이터 제공
"""

import os
from typing import Dict, List, Optional
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


class StockNewsDataProvider:
    """
    Web_02 뉴스/커뮤니티 데이터 프로바이더

    기능:
    - 뉴스 탭: 투자 관련 뉴스 (2단계 필터링)
    - 커뮤니티 탭: 시장 반응/투자자 의견
    """

    def __init__(self):
        """Initialize"""
        # Tavily 웹 검색
        try:
            try:
                from .tavily_search import TavilySearchClient
            except ImportError:
                from tavily_search import TavilySearchClient
            self.tavily = TavilySearchClient()
        except Exception as e:
            print(f"Warning: Tavily 초기화 실패 - {e}")
            self.tavily = None

        # Gemini (LLM 필터링용)
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


        # 투자 관련 키워드 (1단계 필터링) — 소문자로 통일 (text.lower() 매칭)
        self.investment_keywords = [
            # 기존 키워드 (소문자 통일)
            "주가", "주식", "투자", "매수", "매도", "목표가", "실적",
            "영업이익", "매출", "배당", "공시", "ir", "애널리스트",
            "증권", "펀드", "etf", "상승", "하락", "전망",
            # 기획서 추가 키워드
            "순이익", "컨센서스",
            "급등", "급락",
            "자사주", "소각", "증자", "무상증자", "유상증자", "액면분할",
            "제재", "승인", "허가", "규제",
            "인수", "합병", "m&a", "분할", "상장",
        ]

        # 제외 키워드 (투자 무관 콘텐츠 차단)
        self.exclude_keywords = ["브랜드", "캠페인", "사회공헌", "인터뷰"]

    # ========================================
    # 뉴스 탭 API
    # ========================================

    def get_news(
        self,
        symbol: str,
        company_name: str,
        page: int = 1,
        limit: int = 20
    ) -> Dict:
        """
        뉴스 탭 데이터 (투자 관련 필터링)

        Args:
            symbol: 종목코드 (예: "005930")
            company_name: 회사명 (예: "삼성전자")
            page: 페이지 번호
            limit: 페이지당 개수

        Returns:
            뉴스 리스트 응답
        """
        if not self.tavily:
            return self._error_response("Tavily 검색 불가")

        # Tavily 검색
        search_result = self.tavily.search_stock_news(
            company_name=company_name,
            ticker=symbol,
            max_results=20  # 필터링 후 줄어들 수 있으므로 넉넉히
        )

        if "error" in search_result:
            return self._error_response(search_result["error"])

        # 2단계 필터링
        raw_results = search_result.get("results", [])
        filtered_news = self._filter_investment_news(raw_results)

        # 페이징
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paged_news = filtered_news[start_idx:end_idx]

        return {
            "symbol": symbol,
            "company_name": company_name,
            "tab": "news",
            "page": page,
            "limit": limit,
            "total_count": len(filtered_news),
            "has_more": end_idx < len(filtered_news),
            "items": [
                {
                    "id": f"news_{i}",
                    "title": item.get("title", ""),
                    "content": item.get("content", "")[:150] + "...",
                    "url": item.get("url", ""),
                    "source": self._extract_source(item.get("url", "")),
                    "published_at": item.get("published_date", "") or search_result.get("searched_at", ""),
                    "is_investment_related": True
                }
                for i, item in enumerate(paged_news, start=start_idx + 1)
            ],
            "fetched_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

    def _filter_investment_news(self, results: List[Dict]) -> List[Dict]:
        """
        투자 관련 뉴스 2단계 필터링
        1단계: 키워드 필터 (포함 + 제외)
        2단계: LLM 분류 (선택)
        """
        filtered = []

        for item in results:
            title = item.get("title", "")
            content = item.get("content", "")
            text = f"{title} {content}".lower()

            # 1단계-A: 제외 키워드 체크 (투자 무관 콘텐츠 차단)
            if any(ek in text for ek in self.exclude_keywords):
                continue

            # 1단계-B: 포함 키워드 필터
            if any(kw in text for kw in self.investment_keywords):
                filtered.append(item)

        # 2단계: LLM 분류 (API 호출 비용 고려하여 선택적)
        # 실제 운영 시 self._llm_classify_news(filtered) 활성화

        return filtered

    def _llm_classify_news(self, news_list: List[Dict]) -> List[Dict]:
        """LLM으로 투자 관련 뉴스 분류 (2단계)"""
        if not self.genai or not news_list:
            return news_list

        # 비용 절감을 위해 배치 처리
        titles = [item.get("title", "") for item in news_list]
        prompt = f"""
다음 뉴스 제목들 중 '투자 판단에 도움이 되는' 뉴스 번호만 반환하세요.
(실적, 주가, 공시, 애널리스트 의견 등)

{chr(10).join(f"{i+1}. {t}" for i, t in enumerate(titles))}

투자 관련 뉴스 번호 (쉼표로 구분):
"""
        try:
            model = self.genai.GenerativeModel('gemini-2.5-flash')
            response = model.generate_content(prompt)

            # 응답 파싱 (예: "1, 3, 5")
            numbers = [int(n.strip()) for n in response.text.split(",") if n.strip().isdigit()]
            return [news_list[n-1] for n in numbers if 0 < n <= len(news_list)]
        except Exception:
            return news_list

    def _extract_source(self, url: str) -> str:
        """URL에서 출처 추출"""
        source_map = {
            "naver.com": "네이버",
            "hankyung.com": "한국경제",
            "mk.co.kr": "매일경제",
            "sedaily.com": "서울경제",
            "edaily.co.kr": "이데일리",
            "businesspost.co.kr": "비즈니스포스트",
            "khan.co.kr": "경향신문"
        }
        for domain, name in source_map.items():
            if domain in url:
                return name
        return "뉴스"

    # ========================================
    # 커뮤니티 탭 API
    # ========================================

    def get_community(
        self,
        symbol: str,
        company_name: str,
        page: int = 1,
        limit: int = 20,
        last_id: Optional[str] = None
    ) -> Dict:
        """
        커뮤니티 탭 데이터 (시장 반응/투자자 의견)

        Args:
            symbol: 종목코드
            company_name: 회사명
            page: 페이지 번호
            limit: 페이지당 개수
            last_id: 마지막 조회 ID (실시간 새 글 확인용)

        Returns:
            커뮤니티 피드 응답
        """
        if not self.tavily:
            return self._error_response("Tavily 검색 불가")

        # 시장 반응 검색
        search_result = self.tavily.search_market_sentiment(
            company_name=company_name,
            max_results=15
        )

        if "error" in search_result:
            return self._error_response(search_result["error"])

        results = search_result.get("results", [])

        # 페이징
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paged_results = results[start_idx:end_idx]

        # AI 요약: page=1일 때만 생성 (비용 절감)
        ai_summary = ""
        if page == 1:
            english_answer = search_result.get("answer", "")
            ai_summary = self._translate_to_korean(company_name, english_answer)

        return {
            "symbol": symbol,
            "company_name": company_name,
            "tab": "community",
            "page": page,
            "limit": limit,
            "total_count": len(results),
            "has_more": end_idx < len(results),
            "ai_summary": ai_summary[:200] if ai_summary else None,
            "items": [
                {
                    "id": f"comm_{i}",
                    "title": item.get("title", ""),
                    "content": item.get("content", "")[:200] + "...",
                    "url": item.get("url", ""),
                    "source": self._extract_source(item.get("url", "")),
                    "sentiment": self._analyze_sentiment(item.get("content", "")),
                    "published_at": item.get("published_date", "") or search_result.get("searched_at", "")
                }
                for i, item in enumerate(paged_results, start=start_idx + 1)
            ],
            "fetched_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "new_count": 0  # 실시간 폴링 시 새 글 개수
        }

    def _analyze_sentiment(self, text: str) -> str:
        """간단한 감성 분석"""
        positive = ["상승", "호재", "매수", "긍정", "좋", "기대"]
        negative = ["하락", "악재", "매도", "부정", "우려", "리스크"]

        text_lower = text.lower()
        pos_count = sum(1 for w in positive if w in text_lower)
        neg_count = sum(1 for w in negative if w in text_lower)

        if pos_count > neg_count:
            return "positive"
        elif neg_count > pos_count:
            return "negative"
        return "neutral"

    def _translate_to_korean(self, company_name: str, english_summary: str) -> str:
        """
        Tavily 영어 요약을 한국어로 번역

        Args:
            company_name: 회사명
            english_summary: Tavily의 영어 요약 (answer)

        Returns:
            한국어 요약 문자열
        """
        if not self.genai or not english_summary:
            return ""

        prompt = f"""
다음은 '{company_name}' 종목에 대한 시장 반응 요약입니다.
이 내용을 투자자가 이해하기 쉽게 한국어 2-3문장으로 번역해주세요.

{english_summary}

조건:
- 자연스러운 한국어로 번역
- 투자 관련 핵심 내용 유지
"""
        try:
            model = self.genai.GenerativeModel('gemini-2.5-flash')
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            print(f"Warning: Gemini 번역 실패 - {e}")
            return ""

    # ========================================
    # 통합 API (Web_02 메인)
    # ========================================

    def get_news_community_api(
        self,
        symbol: str,
        company_name: str,
        tab: str = "community",
        page: int = 1,
        limit: int = 10
    ) -> Dict:
        """
        Web_02 통합 API

        Args:
            symbol: 종목코드
            company_name: 회사명
            tab: "news" 또는 "community" (기본: community)
            page: 페이지 번호
            limit: 페이지당 개수

        Returns:
            탭에 따른 데이터 응답
        """
        if tab == "news":
            return self.get_news(symbol, company_name, page, limit)
        else:
            return self.get_community(symbol, company_name, page, limit)

    def _error_response(self, reason: str) -> Dict:
        """에러 응답 생성"""
        return {
            "error": reason,
            "items": [],
            "fetched_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }


# ========================================
# 테스트
# ========================================

if __name__ == "__main__":
    import json

    print("=" * 60)
    print("Web_02 뉴스/커뮤니티 데이터 테스트")
    print("=" * 60)
    print()

    provider = StockNewsDataProvider()

    # 테스트: 삼성전자
    symbol = "005930"
    company = "삼성전자"

    # 뉴스 탭 테스트
    print("[뉴스 탭]")
    print("-" * 40)
    news_result = provider.get_news(symbol, company, page=1, limit=5)
    print(f"총 {news_result.get('total_count', 0)}건")
    for item in news_result.get("items", [])[:3]:
        print(f"  - [{item['source']}] {item['title'][:40]}...")
    print()

    # 커뮤니티 탭 테스트
    print("[커뮤니티 탭]")
    print("-" * 40)
    comm_result = provider.get_community(symbol, company, page=1, limit=5)
    if comm_result.get("ai_summary"):
        print(f"AI 요약: {comm_result['ai_summary'][:100]}...")
    for item in comm_result.get("items", [])[:3]:
        print(f"  - [{item['sentiment']}] {item['title'][:40]}...")
    print()

    # 통합 API 테스트
    print("[통합 API - community]")
    print("-" * 40)
    result = provider.get_news_community_api(symbol, company, tab="community")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:500] + "...")
