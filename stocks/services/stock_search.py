"""
종목 검색 모듈.
네이버 금융 검색 페이지를 스크래핑해서 부분/완전 일치 결과를 반환.

기존 /stocks/list 가 KIS 랭킹 API 기반 top 30 만 반환하므로,
top 30 밖의 종목 (예: 카카오 035720) 은 검색이 불가능했음.
이 모듈은 KOSPI/KOSDAQ 전체 종목을 대상으로 부분 일치 검색을 제공.
"""
from __future__ import annotations

import logging
import urllib.parse
from functools import lru_cache
from typing import Dict, List

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT = 5
_SEARCH_URL = "https://finance.naver.com/search/search.naver"
_ITEM_URL = "https://finance.naver.com/item/main.naver"


def search_stocks(query: str, limit: int = 20) -> List[Dict[str, str]]:
    """
    네이버 금융 종목 검색.

    네이버는 5-6자 이상 긴 한글명 (예: "두산로보틱스") 에 빈 결과를 자주 주는데,
    짧은 토큰 ("로보틱스", "두산") 에는 정상 응답.
    → 1차 0건이면 query 의 앞/뒤 부분 토큰으로 retry 후 원 query 부분 일치 필터.

    Args:
        query: 검색어 (한글명 부분 일치, 6자리 코드, ETN 코드 모두 가능)
        limit: 최대 결과 수 (1-50)

    Returns:
        [{"ticker": "005930", "name": "삼성전자"}, ...]
        오류 발생 시 빈 리스트.
    """
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(50, int(limit)))

    primary = _naver_search(q, limit)
    if primary:
        return primary

    # 코드 직접 입력 (6자리) 인데 결과 없으면 종목 페이지 직접 조회.
    if q.isdigit() and len(q) == 6:
        name = _fetch_name_by_code(q)
        if name:
            return [{"ticker": q, "name": name}]

    # 한글 긴 쿼리 fallback — 앞/뒤 토큰으로 검색 후 원 쿼리 substring 필터.
    if len(q) >= 4:
        merged: List[Dict[str, str]] = []
        seen: set = set()
        for token in _fallback_tokens(q):
            partial = _naver_search(token, 50)
            for item in partial:
                name = item.get("name", "")
                ticker = item.get("ticker", "")
                if ticker in seen:
                    continue
                if q not in name:
                    continue
                seen.add(ticker)
                merged.append(item)
                if len(merged) >= limit:
                    return merged
        if merged:
            return merged

    return []


def _fallback_tokens(q: str) -> List[str]:
    """긴 한글 쿼리 → 앞 4자 / 뒤 4자 토큰 (중복 제거, 원 쿼리 자체 제외)."""
    tokens: List[str] = []
    seen: set = set()
    for token in (q[-4:], q[:4], q[-3:], q[:3]):
        if not token or token == q or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _naver_search(q: str, limit: int) -> List[Dict[str, str]]:
    """네이버 finance 1회 호출 + 파싱. 결과 없으면 빈 리스트."""
    try:
        encoded = urllib.parse.quote(q.encode("euc-kr"))
        url = f"{_SEARCH_URL}?query={encoded}"
        res = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
    except Exception as e:
        logger.error("[SEARCH-NAVER-ERROR] %s: %s", type(e).__name__, e)
        return []

    final_url = res.url or ""

    # 완전 일치 단일 종목 → 네이버가 종목 상세로 리다이렉트
    if "code=" in final_url and "search/search.naver" not in final_url:
        code = final_url.split("code=")[1].split("&")[0]
        name = _extract_title_name(res.content, fallback=q)
        return [{"ticker": code, "name": name}]

    try:
        soup = BeautifulSoup(res.content, "html.parser", from_encoding="euc-kr")
        a_tags = soup.select("td.tit a")
    except Exception as e:
        logger.error("[SEARCH-PARSE-ERROR] %s: %s", type(e).__name__, e)
        return []

    results: List[Dict[str, str]] = []
    seen = set()
    for a in a_tags:
        href = a.get("href", "")
        if "code=" not in href:
            continue
        code = href.split("code=")[1].split("&")[0]
        if code in seen:
            continue
        seen.add(code)
        name = a.get_text(strip=True)
        if not name:
            continue
        results.append({"ticker": code, "name": name})
        if len(results) >= limit:
            break

    return results


def _extract_title_name(content: bytes, fallback: str = "") -> str:
    """네이버 종목 페이지의 <title> 에서 종목명 추출 (형식: '삼성전자 : 네이버페이 증권')."""
    try:
        soup = BeautifulSoup(content, "html.parser", from_encoding="euc-kr")
        if soup.title:
            title = soup.title.get_text(strip=True)
            if ":" in title:
                return title.split(":")[0].strip() or fallback
            return title or fallback
    except Exception:
        pass
    return fallback


def _fetch_name_by_code(code: str) -> str:
    """6자리 코드로 네이버 종목 페이지를 직접 조회해서 종목명만 가져옴."""
    try:
        res = requests.get(f"{_ITEM_URL}?code={code}", headers=_HEADERS, timeout=_TIMEOUT)
        return _extract_title_name(res.content, fallback="")
    except Exception as e:
        logger.warning("[SEARCH-CODE-LOOKUP] %s failed: %s", code, e)
        return ""


@lru_cache(maxsize=4096)
def lookup_company_name(ticker: str) -> str:
    """
    종목코드 → 회사명 단일 lookup (lru cache 4096).

    이전 구현은 pykrx.get_market_ticker_name 을 사용했으나 KRX scraping
    단절로 항상 빈값 반환 → 네이버 종목 페이지 title 파싱으로 대체.
    실패 시 ticker 그대로 반환 (호출자 컨벤션 유지).
    """
    if not ticker:
        return ticker
    name = _fetch_name_by_code(ticker)
    return name or ticker
