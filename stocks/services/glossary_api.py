"""
용어 사전 API
glossary.json 기반 용어 조회 및 유사 검색

용도:
- Chatbot_03 용어 설명 (공용)
- Web_04 AI 비서 용어 해석
"""

import json
import os
from typing import Dict, List, Optional


class GlossaryAPI:
    """
    주식 용어 사전 API

    기능:
    - lookup(): 용어 정확 검색
    - find_similar(): 유사 용어 검색
    - get_related_terms(): 연관 용어 조회
    - get_all_terms(): 전체 용어 목록
    """

    def __init__(self, glossary_path: str = None):
        """Initialize"""
        if glossary_path is None:
            glossary_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "data", "glossary.json"
            )
        self._glossary_path = glossary_path
        self._data = self._load_glossary()

    def _load_glossary(self) -> Dict:
        """용어 사전 로드"""
        try:
            with open(self._glossary_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def lookup(self, term: str) -> Optional[Dict]:
        """
        용어 정확 검색

        Args:
            term: 검색할 용어 (예: "PER", "물타기")

        Returns:
            {
                "term": "PER",
                "full_name": "주가수익비율",
                "english": "Price Earnings Ratio",
                "category": "재무비율",
                "description": "...",
                "formula": "...",
                "example": "...",
                "interpretation": {...},
                "related_terms": [...]
            }
            또는 None (없는 경우)
        """
        # 정확 매칭
        if term in self._data:
            result = self._data[term].copy()
            result["term"] = term
            return result

        # 대소문자 무시 매칭
        term_upper = term.upper()
        for key, value in self._data.items():
            if key.upper() == term_upper:
                result = value.copy()
                result["term"] = key
                return result

        # full_name 매칭
        for key, value in self._data.items():
            if value.get("full_name", "") == term:
                result = value.copy()
                result["term"] = key
                return result

        return None

    def find_similar(self, query: str, limit: int = 5) -> List[Dict]:
        """
        유사 용어 검색 (부분 매칭)

        Args:
            query: 검색어
            limit: 최대 결과 수

        Returns:
            [{"term": "PER", "full_name": "주가수익비율", "category": "재무비율"}, ...]
        """
        results = []
        query_lower = query.lower()

        for key, value in self._data.items():
            score = 0
            full_name = value.get("full_name", "")
            english = value.get("english", "")
            description = value.get("description", "")

            key_lower = key.lower()
            full_lower = full_name.lower()
            eng_lower = english.lower()
            desc_lower = description.lower()

            # 키 매칭 (양방향: 검색어가 키에 포함 OR 키가 검색어에 포함)
            if query_lower in key_lower or key_lower in query_lower:
                score += 10
            # full_name 매칭 (양방향)
            if query_lower in full_lower or full_lower in query_lower:
                score += 8
            # english 매칭 (양방향)
            if query_lower in eng_lower or eng_lower in query_lower:
                score += 6
            # description 매칭 (단방향: 검색어가 설명에 포함)
            if query_lower in desc_lower:
                score += 3

            if score > 0:
                results.append({
                    "term": key,
                    "full_name": full_name,
                    "category": value.get("category", ""),
                    "score": score,
                })

        # 점수 내림차순 정렬
        results.sort(key=lambda x: x["score"], reverse=True)

        # score 제거 후 반환
        for r in results:
            del r["score"]

        return results[:limit]

    def get_related_terms(self, term: str) -> List[Dict]:
        """
        연관 용어 조회

        Args:
            term: 기준 용어

        Returns:
            [{"term": "PBR", "full_name": "주가순자산비율"}, ...]
        """
        entry = self.lookup(term)
        if not entry:
            return []

        related = entry.get("related_terms", [])
        results = []
        for rt in related:
            info = self.lookup(rt)
            if info:
                results.append({
                    "term": info["term"],
                    "full_name": info.get("full_name", ""),
                    "category": info.get("category", ""),
                })
            else:
                results.append({
                    "term": rt,
                    "full_name": "",
                    "category": "",
                })

        return results

    def get_all_terms(self) -> List[str]:
        """전체 용어 목록"""
        return list(self._data.keys())

    def get_term_count(self) -> int:
        """용어 수"""
        return len(self._data)


# ========================================
# 테스트
# ========================================

if __name__ == "__main__":
    print("=" * 60)
    print("용어 사전 API 테스트")
    print("=" * 60)
    print()

    api = GlossaryAPI()
    print(f"총 용어 수: {api.get_term_count()}개")
    print()

    # 1. 정확 검색
    print("[1] 정확 검색")
    print("-" * 40)
    result = api.lookup("PER")
    if result:
        print(f"  용어: {result['term']} ({result['full_name']})")
        print(f"  카테고리: {result['category']}")
        print(f"  설명: {result['description'][:50]}...")
    print()

    # 2. full_name 검색
    print("[2] full_name 검색")
    print("-" * 40)
    result = api.lookup("물타기")
    if result:
        print(f"  용어: {result['term']} ({result.get('english', '')})")
        print(f"  설명: {result['description'][:50]}...")
    print()

    # 3. 유사 검색
    print("[3] 유사 검색: '이동평균'")
    print("-" * 40)
    results = api.find_similar("이동평균")
    for r in results:
        print(f"  {r['term']} ({r['full_name']}) - {r['category']}")
    print()

    # 4. 연관 용어
    print("[4] 연관 용어: 'RSI'")
    print("-" * 40)
    related = api.get_related_terms("RSI")
    for r in related:
        print(f"  {r['term']} ({r['full_name']})")
    print()

    print("=" * 60)
    print("테스트 완료")
    print("=" * 60)
