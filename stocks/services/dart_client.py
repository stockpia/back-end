"""
DART Open API 클라이언트
재무제표 데이터 조회 (부채비율, 현금흐름 등)

용도:
- Chatbot_02/Web_05 종목 리포트의 재무 분석 데이터 보강
- PER/PBR/EPS/BPS/ROE (한투 API) + 부채비율/현금흐름 (DART API)
"""

import json
import os
import time
import zipfile
import io
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()


class DartClient:
    """
    DART Open API 클라이언트 (디스크 캐싱 포함)

    기능:
    - get_corp_code(): 종목코드 → DART 고유번호
    - get_financials(): 재무제표 원본 조회
    - get_financial_summary(): 핵심 지표 계산 반환
    """

    BASE_URL = "https://opendart.fss.or.kr/api"
    CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dart_cache")
    CORP_CODE_CACHE = "corp_codes.json"
    CORP_CODE_MAX_AGE_DAYS = 30
    FINANCIAL_CACHE_MAX_AGE_DAYS = 7

    # 계정과목 이름 변형 (회사마다 다를 수 있음)
    ACCOUNT_NAMES = {
        "total_liabilities": ["부채총계"],
        "total_equity": ["자본총계"],
        "revenue": ["매출액", "수익(매출액)", "영업수익", "매출"],
        "operating_income": ["영업이익", "영업이익(손실)"],
        "net_income": ["당기순이익", "당기순이익(손실)", "당기순이익(손실)의 귀속"],
        "operating_cf": [
            "영업활동 현금흐름",
            "영업활동으로 인한 현금흐름",
            "영업활동현금흐름",
        ],
        "investing_cf": [
            "투자활동 현금흐름",
            "투자활동으로 인한 현금흐름",
            "투자활동현금흐름",
        ],
        "financing_cf": [
            "재무활동 현금흐름",
            "재무활동으로 인한 현금흐름",
            "재무활동현금흐름",
        ],
    }

    # 보고서 코드 라벨
    REPORT_LABELS = {
        "11011": "사업보고서",
        "11012": "반기보고서",
        "11013": "1분기보고서",
        "11014": "3분기보고서",
    }

    def __init__(self, api_key: str = None, timeout: int = 30):
        self.api_key = api_key or os.environ.get("DART_API_KEY", "")
        self.timeout = timeout
        self._corp_code_map: Dict[str, str] = {}
        os.makedirs(self.CACHE_DIR, exist_ok=True)
        self._load_corp_code_cache()

    # ========================================
    # Corp Code (종목코드 → DART 고유번호)
    # ========================================

    def get_corp_code(self, ticker: str) -> Optional[str]:
        """
        종목코드 → DART 고유번호 변환

        Args:
            ticker: 종목코드 (예: "005930")

        Returns:
            DART 고유번호 (예: "00126380") 또는 None
        """
        if ticker in self._corp_code_map:
            return self._corp_code_map[ticker]

        # 캐시 리로드 시도
        self._load_corp_code_cache()
        if ticker in self._corp_code_map:
            return self._corp_code_map[ticker]

        # 캐시 없으면 다운로드
        self._download_corp_codes()
        return self._corp_code_map.get(ticker)

    def _load_corp_code_cache(self):
        """디스크 캐시에서 corp_code 매핑 로드"""
        cache_path = os.path.join(self.CACHE_DIR, self.CORP_CODE_CACHE)
        if not os.path.exists(cache_path):
            return

        age_days = (time.time() - os.path.getmtime(cache_path)) / 86400
        if age_days > self.CORP_CODE_MAX_AGE_DAYS:
            return

        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                self._corp_code_map = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    def _download_corp_codes(self):
        """DART corpCode.xml ZIP 다운로드 및 캐싱"""
        url = f"{self.BASE_URL}/corpCode.xml"
        params = {"crtfc_key": self.api_key}

        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            if resp.status_code != 200:
                print(f"[WARN] DART corpCode 다운로드 실패: HTTP {resp.status_code}")
                return

            # ZIP → XML 파싱
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                xml_name = zf.namelist()[0]
                with zf.open(xml_name) as xml_file:
                    tree = ET.parse(xml_file)

            root = tree.getroot()
            mapping = {}
            for item in root.findall("list"):
                stock_code = item.findtext("stock_code", "").strip()
                corp_code = item.findtext("corp_code", "").strip()
                if stock_code and corp_code:
                    mapping[stock_code] = corp_code

            self._corp_code_map = mapping

            # 디스크 캐시 저장
            cache_path = os.path.join(self.CACHE_DIR, self.CORP_CODE_CACHE)
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(mapping, f, ensure_ascii=False)

        except Exception as e:
            print(f"[WARN] DART corpCode 다운로드 오류: {e}")

    # ========================================
    # 재무제표 조회
    # ========================================

    def get_financials(
        self,
        corp_code: str,
        year: int,
        reprt_code: str,
        fs_div: str = "CFS"
    ) -> Optional[List[Dict]]:
        """
        재무제표 전체 조회

        Args:
            corp_code: DART 고유번호
            year: 사업연도
            reprt_code: 보고서 코드 (11011=사업, 11012=반기, 11013=1분기, 11014=3분기)
            fs_div: CFS(연결) 또는 OFS(개별)

        Returns:
            계정과목 리스트 또는 None
        """
        # 캐시 확인
        cached = self._load_financial_cache(corp_code, year, reprt_code, fs_div)
        if cached is not None:
            return cached

        url = f"{self.BASE_URL}/fnlttSinglAcntAll.json"
        params = {
            "crtfc_key": self.api_key,
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": reprt_code,
            "fs_div": fs_div,
        }

        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            data = resp.json()

            if data.get("status") == "000":
                items = data.get("list", [])
                self._save_financial_cache(corp_code, year, reprt_code, fs_div, items)
                return items

            # 연결재무제표 없으면 개별재무제표 시도
            if fs_div == "CFS" and data.get("status") in ("013", "020"):
                return self.get_financials(corp_code, year, reprt_code, "OFS")

            return None

        except Exception as e:
            print(f"[WARN] DART 재무제표 조회 오류: {e}")
            return None

    def _load_financial_cache(self, corp_code, year, reprt_code, fs_div) -> Optional[List]:
        filename = f"fin_{corp_code}_{year}_{reprt_code}_{fs_div}.json"
        filepath = os.path.join(self.CACHE_DIR, filename)

        if not os.path.exists(filepath):
            return None

        age_days = (time.time() - os.path.getmtime(filepath)) / 86400
        if age_days > self.FINANCIAL_CACHE_MAX_AGE_DAYS:
            return None

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def _save_financial_cache(self, corp_code, year, reprt_code, fs_div, items):
        filename = f"fin_{corp_code}_{year}_{reprt_code}_{fs_div}.json"
        filepath = os.path.join(self.CACHE_DIR, filename)
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(items, f, ensure_ascii=False)
        except IOError:
            pass

    # ========================================
    # 핵심: 재무 요약 지표
    # ========================================

    def get_financial_summary(self, ticker: str) -> Dict:
        """
        종목의 핵심 재무 지표를 계산하여 반환

        Args:
            ticker: 종목코드 (예: "005930")

        Returns:
            {
                "debt_ratio": 82.5,
                "total_liabilities": ...,
                "total_equity": ...,
                "operating_cf": ...,
                "investing_cf": ...,
                "financing_cf": ...,
                "revenue": ...,
                "operating_income": ...,
                "net_income": ...,
                "operating_margin": 14.3,
                "report_year": 2025,
                "report_type": "11014",
                "report_label": "2025년 3분기보고서",
                "source": "DART Open API",
            }
            실패 시: {"error": "사유"}
        """
        if not self.api_key:
            return {"error": "DART_API_KEY 미설정"}

        corp_code = self.get_corp_code(ticker)
        if not corp_code:
            return {"error": f"종목코드 {ticker}의 DART 고유번호를 찾을 수 없음"}

        # 최신 보고서부터 시도
        attempts = self._determine_report_attempts()

        for year, reprt_code, label in attempts:
            items = self.get_financials(corp_code, year, reprt_code)
            if items:
                return self._compute_metrics(items, year, reprt_code, label)

        return {"error": "DART 재무제표를 조회할 수 없음"}

    def _determine_report_attempts(self) -> List[tuple]:
        """현재 날짜 기준 시도할 보고서 목록 (최신 우선)"""
        now = datetime.now()
        year = now.year
        month = now.month

        attempts = []

        # 보고서 공시 시점 기준
        if month >= 11:
            attempts.append((year, "11014", f"{year}년 3분기보고서"))
        if month >= 8:
            attempts.append((year, "11012", f"{year}년 반기보고서"))
        if month >= 5:
            attempts.append((year, "11013", f"{year}년 1분기보고서"))
        if month >= 4:
            attempts.append((year - 1, "11011", f"{year - 1}년 사업보고서"))
        else:
            attempts.append((year - 2, "11011", f"{year - 2}년 사업보고서"))

        # 전년 사업보고서 fallback
        fallback = (year - 1, "11011", f"{year - 1}년 사업보고서")
        if fallback not in attempts:
            attempts.append(fallback)

        return attempts

    def _compute_metrics(self, items: List[Dict], year: int, reprt_code: str, label: str) -> Dict:
        """재무제표 원본에서 핵심 지표 계산"""
        result = {
            "report_year": year,
            "report_type": reprt_code,
            "report_label": label,
            "source": "DART Open API",
        }

        # 계정과목 추출
        total_liabilities = self._extract_amount(items, "total_liabilities")
        total_equity = self._extract_amount(items, "total_equity")
        revenue = self._extract_amount(items, "revenue")
        operating_income = self._extract_amount(items, "operating_income")
        net_income = self._extract_amount(items, "net_income")
        operating_cf = self._extract_amount(items, "operating_cf")
        investing_cf = self._extract_amount(items, "investing_cf")
        financing_cf = self._extract_amount(items, "financing_cf")

        # 부채비율 계산
        debt_ratio = None
        if total_liabilities is not None and total_equity is not None and total_equity != 0:
            debt_ratio = round(total_liabilities / total_equity * 100, 1)

        # 영업이익률 계산
        operating_margin = None
        if operating_income is not None and revenue is not None and revenue != 0:
            operating_margin = round(operating_income / revenue * 100, 1)

        result["debt_ratio"] = debt_ratio
        result["total_liabilities"] = total_liabilities
        result["total_equity"] = total_equity
        result["revenue"] = revenue
        result["operating_income"] = operating_income
        result["net_income"] = net_income
        result["operating_margin"] = operating_margin
        result["operating_cf"] = operating_cf
        result["investing_cf"] = investing_cf
        result["financing_cf"] = financing_cf

        # 시계열 (전전기 / 전기 / 당기)
        result["revenue_series"] = self._extract_time_series(items, "revenue")
        result["operating_income_series"] = self._extract_time_series(items, "operating_income")
        result["net_income_series"] = self._extract_time_series(items, "net_income")

        return result

    def _extract_amount(self, items: List[Dict], metric_key: str) -> Optional[float]:
        """계정과목 이름으로 당기 금액 추출 (여러 이름 변형 시도)"""
        names = self.ACCOUNT_NAMES.get(metric_key, [])

        for item in items:
            account_nm = item.get("account_nm", "").strip()
            if account_nm in names:
                return self._parse_amount(item.get("thstrm_amount", ""))

        # fallback: 부분 매칭
        for item in items:
            account_nm = item.get("account_nm", "").strip()
            for name in names:
                if name in account_nm or account_nm in name:
                    return self._parse_amount(item.get("thstrm_amount", ""))

        return None

    def _extract_time_series(self, items: List[Dict], metric_key: str) -> List[Dict]:
        """계정과목 이름으로 전전기/전기/당기 시계열 리스트 추출"""
        names = self.ACCOUNT_NAMES.get(metric_key, [])

        for item in items:
            account_nm = item.get("account_nm", "").strip()
            if account_nm in names:
                return self._build_series(item)

        # fallback: 부분 매칭
        for item in items:
            account_nm = item.get("account_nm", "").strip()
            for name in names:
                if name in account_nm or account_nm in name:
                    return self._build_series(item)

        return []

    def _build_series(self, item: Dict) -> List[Dict]:
        """DART 응답 항목에서 전전기/전기/당기 시계열 리스트 구성"""
        series = []
        pairs = [
            ("bfefrmtrm_nm", "bfefrmtrm_amount", "전전기"),
            ("frmtrm_nm",    "frmtrm_amount",    "전기"),
            ("thstrm_nm",    "thstrm_amount",     "당기"),
        ]
        for nm_key, amount_key, default_label in pairs:
            amount = self._parse_amount(item.get(amount_key))
            if amount is not None:
                label = item.get(nm_key) or default_label
                series.append({"label": label, "amount": amount})
        return series

    def _parse_amount(self, value) -> Optional[float]:
        """금액 문자열 파싱"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            cleaned = str(value).replace(",", "").strip()
            if not cleaned or cleaned == "-":
                return None
            return float(cleaned)
        except (ValueError, TypeError):
            return None


# ========================================
# 테스트
# ========================================

if __name__ == "__main__":
    print("=" * 60)
    print("DART 클라이언트 테스트")
    print("=" * 60)
    print()

    client = DartClient()

    # 1. Corp Code 조회
    ticker = "005930"
    print(f"[1] Corp Code 조회: {ticker}")
    print("-" * 40)
    corp_code = client.get_corp_code(ticker)
    print(f"  DART 고유번호: {corp_code}")
    print()

    # 2. 재무 요약
    print(f"[2] 재무 요약: {ticker}")
    print("-" * 40)
    summary = client.get_financial_summary(ticker)
    if "error" in summary:
        print(f"  에러: {summary['error']}")
    else:
        print(f"  보고서: {summary.get('report_label')}")
        print(f"  부채비율: {summary.get('debt_ratio')}%")
        print(f"  매출액: {summary.get('revenue'):,.0f}원" if summary.get('revenue') else "  매출액: N/A")
        print(f"  영업이익: {summary.get('operating_income'):,.0f}원" if summary.get('operating_income') else "  영업이익: N/A")
        print(f"  영업이익률: {summary.get('operating_margin')}%" if summary.get('operating_margin') else "  영업이익률: N/A")
        print(f"  영업활동 현금흐름: {summary.get('operating_cf'):,.0f}원" if summary.get('operating_cf') else "  영업활동 현금흐름: N/A")
        print(f"  투자활동 현금흐름: {summary.get('investing_cf'):,.0f}원" if summary.get('investing_cf') else "  투자활동 현금흐름: N/A")
        print(f"  재무활동 현금흐름: {summary.get('financing_cf'):,.0f}원" if summary.get('financing_cf') else "  재무활동 현금흐름: N/A")
    print()

    # 3. 다른 종목 테스트
    ticker2 = "000660"
    print(f"[3] 재무 요약: {ticker2} (SK하이닉스)")
    print("-" * 40)
    summary2 = client.get_financial_summary(ticker2)
    if "error" in summary2:
        print(f"  에러: {summary2['error']}")
    else:
        print(f"  보고서: {summary2.get('report_label')}")
        print(f"  부채비율: {summary2.get('debt_ratio')}%")
        print(f"  영업이익률: {summary2.get('operating_margin')}%" if summary2.get('operating_margin') else "  영업이익률: N/A")
    print()

    print("=" * 60)
    print("테스트 완료")
    print("=" * 60)
