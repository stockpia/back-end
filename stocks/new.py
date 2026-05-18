from stocks.services.web_order import WebOrder

try:
    service = WebOrder()
    account_info = service.get_account_info(symbol="005930")  # 테스트용 Ticker
    if "error" in account_info:
        print(f"--- TEST FAILED: Account Balance ---")
        print(f"Error: {account_info['error']}")
    else:
        print(f"--- TEST SUCCESS: Account Balance ---")
        print(f"Available Cash: {account_info.get('available_cash')}")
        print(f"Samsung Electronics Holdings: {account_info.get('holding_quantity')} shares")

except Exception as e:
    print(f"--- TEST FAILED: Account Balance (Exception) ---")
    print(f"Exception: {e}")
