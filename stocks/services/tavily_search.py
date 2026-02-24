"""
Tavily 웹 검색 모듈
종목 관련 최신 뉴스 및 정보를 검색합니다.
"""

import os
from typing import Dict, List, Optional
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


class TavilySearchClient:
    """
    Tavily API를 사용한 웹 검색 클라이언트
    주식 뉴스, 애널리스트 의견, 시장 반응 등을 검색
    """

    def __init__(self):
        """Tavily API 초기화"""
        self.api_key = os.environ.get("TAVILY_API_KEY")
        if not self.api_key:
            print("TAVILY_API_KEY not found in environment")
            self.available = False
            return

        try:
            from tavily import TavilyClient
            self.client = TavilyClient(api_key=self.api_key)
            self.available = True
        except ImportError:
            print("Warning: tavily-python not installed. Run: pip install tavily-python")
            self.available = False
        # 에러 처리
        except Exception as e:
            print(f"Error: Tavily 클라이언트 초기화 실패: {e}")
            self.available = False

    def search_stock_news(
        self,
        company_name: str,
        ticker: str,
        max_results: int = 5
    ) -> Dict:
        """
        종목 관련 최신 뉴스 검색

        Args:
            company_name: 회사명 (예: "삼성전자")
            ticker: 종목코드 (예: "005930")
            max_results: 최대 결과 수

        Returns:
            검색 결과 딕셔너리
        """
        if not self.available:
            return {"error": "Tavily not available", "results": []}

        query = f"{company_name} 주식 뉴스 최신"

        try:
            response = self.client.search(
                query=query,
                search_depth="basic",
                max_results=max_results,
                include_answer=True,
                include_domains=["naver.com", "hankyung.com", "mk.co.kr", "sedaily.com", "edaily.co.kr"]
            )

            return {
                "query": query,
                "answer": response.get("answer", ""),
                "results": [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:300],  # 300자로 제한
                        "score": r.get("score", 0)
                    }
                    for r in response.get("results", [])
                ],
                "searched_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            return {"error": str(e), "results": []}

    def search_analyst_opinion(
        self,
        company_name: str,
        max_results: int = 3
    ) -> Dict:
        """
        애널리스트 의견/목표주가 검색

        Args:
            company_name: 회사명
            max_results: 최대 결과 수

        Returns:
            검색 결과 딕셔너리
        """
        if not self.available:
            return {"error": "Tavily not available", "results": []}

        query = f"{company_name} 목표주가 애널리스트 리포트 2026"

        try:
            response = self.client.search(
                query=query,
                search_depth="basic",
                max_results=max_results,
                include_answer=True
            )

            return {
                "query": query,
                "answer": response.get("answer", ""),
                "results": [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:300]
                    }
                    for r in response.get("results", [])
                ],
                "searched_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            return {"error": str(e), "results": []}

    def search_market_sentiment(
        self,
        company_name: str,
        max_results: int = 5
    ) -> Dict:
        """
        시장 반응/커뮤니티 의견 검색

        Args:
            company_name: 회사명
            max_results: 최대 결과 수

        Returns:
            검색 결과 딕셔너리
        """
        if not self.available:
            return {"error": "Tavily not available", "results": []}

        query = f"{company_name} 주식 전망 투자 의견"

        try:
            response = self.client.search(
                query=query,
                search_depth="basic",  # 비용 절감 (advanced → basic)
                max_results=max_results,
                include_answer=True
            )

            return {
                "query": query,
                "answer": response.get("answer", ""),
                "results": [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:300]
                    }
                    for r in response.get("results", [])
                ],
                "searched_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            return {"error": str(e), "results": []}

    def get_comprehensive_info(
        self,
        company_name: str,
        ticker: str
    ) -> Dict:
        """
        종합 정보 검색 (뉴스 + 애널리스트 + 시장반응)

        Args:
            company_name: 회사명
            ticker: 종목코드

        Returns:
            종합 검색 결과
        """
        news = self.search_stock_news(company_name, ticker)
        analyst = self.search_analyst_opinion(company_name)

        return {
            "company_name": company_name,
            "ticker": ticker,
            "news": news,
            "analyst": analyst,
            "searched_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

    def format_for_llm(self, search_result: Dict) -> str:
        """
        검색 결과를 LLM 프롬프트용 텍스트로 변환

        Args:
            search_result: 검색 결과 딕셔너리

        Returns:
            포맷된 텍스트
        """
        lines = []

        # 뉴스 섹션
        if "news" in search_result:
            news = search_result["news"]
            lines.append("### 최신 뉴스")
            if news.get("answer"):
                lines.append(f"요약: {news['answer'][:500]}")
            for i, r in enumerate(news.get("results", [])[:3], 1):
                lines.append(f"{i}. [{r['title']}]")
                lines.append(f"   {r['content'][:150]}...")
            lines.append("")

        # 애널리스트 의견 섹션
        if "analyst" in search_result:
            analyst = search_result["analyst"]
            lines.append("### 애널리스트 의견")
            if analyst.get("answer"):
                lines.append(f"요약: {analyst['answer'][:500]}")
            for i, r in enumerate(analyst.get("results", [])[:2], 1):
                lines.append(f"{i}. [{r['title']}]")
                lines.append(f"   {r['content'][:150]}...")
            lines.append("")

        return "\n".join(lines) if lines else "웹 검색 결과 없음"


# ========================================
# 테스트
# ========================================

if __name__ == "__main__":
    print("=" * 60)
    print("Tavily 웹 검색 테스트")
    print("=" * 60)
    print()

    try:
        client = TavilySearchClient()
        print("[OK] Tavily 클라이언트 초기화 성공")
        print()

        # 테스트: 삼성전자 뉴스 검색
        company = "삼성전자"
        ticker = "005930"

        print(f"검색 중: {company} ({ticker})")
        print("-" * 40)

        result = client.get_comprehensive_info(company, ticker)

        # 뉴스 출력
        print("\n[최신 뉴스]")
        news = result.get("news", {})
        if news.get("answer"):
            print(f"AI 요약: {news['answer'][:200]}...")
        for r in news.get("results", [])[:3]:
            print(f"  - {r['title']}")

        # 애널리스트 출력
        print("\n[애널리스트 의견]")
        analyst = result.get("analyst", {})
        if analyst.get("answer"):
            print(f"AI 요약: {analyst['answer'][:200]}...")
        for r in analyst.get("results", [])[:2]:
            print(f"  - {r['title']}")

        # LLM용 포맷
        print("\n[LLM 프롬프트용 텍스트]")
        print("-" * 40)
        formatted = client.format_for_llm(result)
        print(formatted[:500] + "..." if len(formatted) > 500 else formatted)

        print("\n[OK] 테스트 완료!")

    except Exception as e:
        print(f"[ERROR] {e}")
