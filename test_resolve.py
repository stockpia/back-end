import requests
import urllib.parse
from bs4 import BeautifulSoup
from pykrx import stock as pystock
from datetime import datetime
from dateutil.relativedelta import relativedelta

def resolve_symbol(symbol_or_name: str) -> str:
    target = symbol_or_name.replace(" ", "").upper()
    debug_logs = []

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://finance.naver.com/'
    }

    # 1. 네이버 금융 자동완성 API 활용
    url = "https://ac.finance.naver.com/ac"
    params = {
        'q': symbol_or_name,
        'q_enc': 'utf-8',
        'st': '111',
        'r_format': 'json',
        'r_enc': 'utf-8'
    }
    
    try:
        res = requests.get(url, params=params, headers=headers, timeout=5)
        debug_logs.append(f"AC Status: {res.status_code}")
        if res.status_code == 200:
            data = res.json()
            items = data.get('items', [])
            
            # 1순위: 정확히 일치하는 종목 찾기
            for category in items:
                for item in category:
                    if len(item) >= 2 and item[1].isdigit():
                        if item[0] == symbol_or_name:
                            return str(item[1])
                            
            # 2순위: 첫 번째 종목 반환
            for category in items:
                for item in category:
                    if len(item) >= 2 and item[1].isdigit():
                        return str(item[1])
            debug_logs.append("AC No items found")
    except Exception as e:
        debug_logs.append(f"AC Error: {str(e)}")

    # 2. 네이버 금융 검색 스크래핑
    try:
        encoded_query = urllib.parse.quote(symbol_or_name.encode('euc-kr'))
        search_url = f"https://finance.naver.com/search/search.naver?query={encoded_query}"
        
        res = requests.get(search_url, headers=headers, timeout=5)
        debug_logs.append(f"Search Status: {res.status_code}")
        
        if 'code=' in res.url:
            return res.url.split('code=')[1].split('&')[0]
            
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        a_tags = soup.select("td.tit a")
        if a_tags:
            href = a_tags[0].get('href', '')
            if 'code=' in href:
                return href.split('code=')[1].split('&')[0]
        debug_logs.append("Search No tags found")
    except Exception as e:
        debug_logs.append(f"Search Error: {str(e)}")

    return f"ERROR: {' | '.join(debug_logs)}"

for name in ["카카오", "한화", "두산", "두산로보틱스"]:
    print(f"{name}: {resolve_symbol(name)}")