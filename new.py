from credentials import API_KEY, SECRET_API_KEY
import yfinance as yf
import requests
import logging
import time
import math
from datetime import datetime

# --- CONFIGURATION & LOGGING ---
BASE_URL = "https://demo.trading212.com/api/v0"

# Logs all actions to a file so we can trace bugs and analyze trade history later
logging.basicConfig(
    filename="trading_bot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# --- STRATEGY PARAMETERS ---
MAX_OPEN_POSITIONS = 5        # Limit to exactly 5 concurrent trades
RISK_PER_TRADE_PCT = 0.20     # Target: Invest 20% of TOTAL equity per trade
MIN_CASH_REQUIRED = 50.0      # Absolute minimum cash needed to bother making a trade
EMERGENCY_STOP_PCT = 0.01     # -1.0% Hard Stop right after buying
BREAK_EVEN_PROFIT = 0.005     # When stock is +0.5% in profit...
BREAK_EVEN_STOP = 0.001       # ...move stop loss to +0.1% (Lock in guaranteed profit)
TRAIL_DISTANCE = 0.003        # Once in profit, trail 0.3% behind the highest price

# --- WATCHLISTS & REGIONS ---
STOCKS_EU = ["SAP", "SIE", "MBG", "VOW3", "BMW", "ALV", "DTE", "BAYN", "BAS", "EON", "RWE"]
STOCKS_US = ["TSLA", "AAPL", "NVDA", "MSFT", "AMZN", "META", "GOOGL", "AMD", "NFLX"]
STOCKS_JP = ["SONY", "TM", "HMC", "MUFG"] 

WATCHLIST_TICKERS = STOCKS_EU + STOCKS_US + STOCKS_JP

# Market hours in minutes (German Time)
MARKET_HOURS = {
    "EU": {"open": 9 * 60, "close": 17 * 60 + 30},   
    "US": {"open": 15 * 60 + 30, "close": 22 * 60},  
    "JP": {"open": 2 * 60, "close": 8 * 60}          
}

def get_market_state(region, current_minutes, is_friday):
    """Evaluates if we are allowed to trade, hold, or if we must panic-sell for the weekend."""
    open_time = MARKET_HOURS[region]["open"]
    close_time = MARKET_HOURS[region]["close"]
    
    if current_minutes < open_time or current_minutes >= close_time: 
        return "CLOSED"
    if current_minutes < (open_time + 2): 
        return "WAIT_FOR_OPEN" # Avoid high spreads at the opening bell
    if is_friday and current_minutes >= (close_time - 10): 
        return "FRIDAY_CLOSING" # Weekend risk mitigation
        
    return "TRADING"

def get_region_for_ticker(ticker):
    if ticker in STOCKS_EU: return "EU"
    if ticker in STOCKS_US: return "US"
    if ticker in STOCKS_JP: return "JP"
    return "US" 

def get_t212_ticker_map():
    """
    STRICT MAPPING: Prevents buying Canadian "Saputo" instead of German "SAP".
    Forces EU stocks to EU exchanges and US stocks to US exchanges.
    """
    print("Mapping EU, US & JP tickers securely...")
    endpoint = f"{BASE_URL}/equity/metadata/instruments"
    response = requests.get(endpoint, auth=(API_KEY, SECRET_API_KEY))
    if response.status_code != 200: return {}
    
    ticker_map = {}
    for item in response.json():
        short_name = item.get("shortName")
        ticker_code = item.get("ticker", "")
        
        if short_name in WATCHLIST_TICKERS and item.get("type") == "STOCK":
            # Strict assignment to prevent wrong exchanges
            if short_name in STOCKS_EU and ("d_EQ" in ticker_code or "_DE_EQ" in ticker_code):
                ticker_map[short_name] = ticker_code
            elif short_name in STOCKS_US and "_US_EQ" in ticker_code:
                ticker_map[short_name] = ticker_code
            elif short_name in STOCKS_JP and ("_US_EQ" in ticker_code or "d_EQ" in ticker_code):
                ticker_map[short_name] = ticker_code
                
    return ticker_map

def get_account_data():
    """Returns Total Equity (Value of all stocks + cash) AND Free uninvested Cash."""
    endpoint = f"{BASE_URL}/equity/account/cash"
    try:
        response = requests.get(endpoint, auth=(API_KEY, SECRET_API_KEY))
        if response.status_code == 200:
            return {
                "total": float(response.json().get("total", 0.0)), 
                "free": float(response.json().get("free", 0.0))
            }
    except: pass
    return {"total": 0.0, "free": 0.0}

def cancel_all_stop_orders(t212_ticker):
    """Deletes active stop-losses. MUST be done before selling to unlock the shares."""
    endpoint = f"{BASE_URL}/equity/orders"
    try:
        response = requests.get(endpoint, auth=(API_KEY, SECRET_API_KEY))
        if response.status_code == 200:
            for order in response.json():
                if order.get("ticker") == t212_ticker and order.get("type") == "STOP":
                    requests.delete(f"{BASE_URL}/equity/orders/{order.get('id')}", auth=(API_KEY, SECRET_API_KEY))
    except: pass

def execute_order(t212_ticker, quantity, action):
    """Sends Market Orders. Sells require a negative quantity in T212 API."""
    if action == "SELL":
        cancel_all_stop_orders(t212_ticker) 
        api_qty = -quantity 
    else:
        api_qty = quantity
    payload = {"ticker": t212_ticker, "quantity": api_qty}
    response = requests.post(f"{BASE_URL}/equity/orders/market", json=payload, auth=(API_KEY, SECRET_API_KEY))
    return response.status_code == 200

def place_server_stop_loss(t212_ticker, quantity, stop_price):
    """Sets the physical hard-stop on the broker server."""
    payload = {
        "ticker": t212_ticker, 
        "quantity": -quantity, # Requires negative qty
        "stopPrice": round(stop_price, 2)
    }
    response = requests.post(f"{BASE_URL}/equity/orders/stop", json=payload, auth=(API_KEY, SECRET_API_KEY))
    if response.status_code == 200:
        print(f"🔒 Server Stop-Loss updated at ${stop_price:.2f}")
        return True
    return False

def get_active_portfolio_snapshot():
    """
    RATE LIMIT PROTECTOR: Fetches all positions exactly ONCE per loop.
    Returns a dictionary of {ticker: average_buy_price}.
    """
    try:
        response = requests.get(f"{BASE_URL}/equity/portfolio", auth=(API_KEY, SECRET_API_KEY))
        if response.status_code == 200:
            return {pos.get("ticker"): float(pos.get("averagePrice")) for pos in response.json()}
    except: pass
    return None # Returns None if API crashes (Triggers Fail-Safe)

def get_live_price(ticker):
    yf_ticker = f"{ticker}.DE" if ticker in STOCKS_EU else ticker
    try: return float(yf.Ticker(yf_ticker).history(period="1d", interval="1m")['Close'].iloc[-1])
    except: return None

def get_daily_high(ticker):
    yf_ticker = f"{ticker}.DE" if ticker in STOCKS_EU else ticker
    try: return float(yf.Ticker(yf_ticker).history(period="1d", interval="1m")['High'].max())
    except: return None

def run_scalping_bot():
    ticker_map = get_t212_ticker_map()
    open_positions = {}
    print(f"=== BOT READY - WATCHING {len(ticker_map)} STOCKS (EU/US/JP) ===")

    while True:
        now_dt = datetime.now()
        now = now_dt.strftime("%H:%M:%S")
        current_minutes = now_dt.hour * 60 + now_dt.minute
        is_friday = now_dt.weekday() == 4 
        
        # 1. Fetch truth from server ONCE to save API limits
        portfolio_snapshot = get_active_portfolio_snapshot()
        
        # --- PHASE 1: MANAGE OPEN POSITIONS ---
        for name in list(open_positions.keys()):
            trade = open_positions[name]
            region = get_region_for_ticker(name)
            market_state = get_market_state(region, current_minutes, is_friday)
            
            # Fail-Safe check: Did the server sell our stock?
            if portfolio_snapshot is not None:
                if trade["ticker"] not in portfolio_snapshot:
                    print(f"[{now}] ℹ️ Server Stop-Loss was triggered for {name}. Position closed.")
                    del open_positions[name]
                    continue
            
            # Weekend Panic-Sell
            if market_state == "FRIDAY_CLOSING":
                print(f"[{now}] ⏰ WEEKEND RISK: Liquidating {name} before Friday close...")
                if execute_order(trade["ticker"], trade["quantity"], "SELL"):
                    del open_positions[name]
                continue
                
            if market_state == "CLOSED":
                continue # Do nothing overnight, let the server stop-loss protect us
                
            price = get_live_price(name)
            if not price: continue
            
            # Track peak price for trailing stops
            if price > trade["highest"]: 
                trade["highest"] = price
            
            profit_pct = (price - trade["entry"]) / trade["entry"]
            target_stop = 0.0
            
            # Break-Even & Trailing Logic
            if profit_pct >= BREAK_EVEN_PROFIT:
                break_even_price = trade["entry"] * (1 + BREAK_EVEN_STOP)
                trailing_price = trade["highest"] * (1 - TRAIL_DISTANCE)
                target_stop = max(break_even_price, trailing_price)
            else:
                target_stop = trade["entry"] * (1 - EMERGENCY_STOP_PCT)
            
            # Update Server only if needed (saves API calls)
            if target_stop > trade["current_stop"] * 1.001:
                print(f"[{now}] 📈 Updating Trailing Stop for {name} to {target_stop:.2f}")
                cancel_all_stop_orders(trade["ticker"])
                time.sleep(1) 
                if place_server_stop_loss(trade["ticker"], trade["quantity"], target_stop):
                    trade["current_stop"] = target_stop

        # --- PHASE 2: SCAN MARKET FOR NEW DIPS ---
        acc = get_account_data()
        
        for name, t212_code in ticker_map.items():
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break # Stop scanning if we hit our 5-position limit
                
            if name in open_positions: continue
            
            region = get_region_for_ticker(name)
            market_state = get_market_state(region, current_minutes, is_friday)
            
            if market_state != "TRADING":
                continue
            
            # Check price structure
            price = get_live_price(name)
            high = get_daily_high(name)
            if not price or not high: continue
            
            # Trigger: Dropped 0.5% from daily high
            if ((high - price) / high) >= 0.005:
                
                # --- DYNAMIC POSITION SIZING (20% or Rest) ---
                target_budget = acc["total"] * RISK_PER_TRADE_PCT
                
                # If we have less cash than 20%, go "All-In" with the remaining cash
                budget = target_budget if acc["free"] >= target_budget else acc["free"]
                
                # Absolute minimum limit (Don't buy 1 share for 2 EUR)
                if budget < MIN_CASH_REQUIRED: 
                    continue
                
                qty = math.floor(budget / price)
                
                if qty > 0:
                    actual_cost = qty * price
                    print(f"[{now}] 🚨 Setup for {name}! Budget allocated: {actual_cost:.2f}€. Buying {qty} shares...")
                    
                    if execute_order(t212_code, qty, "BUY"):
                        
                        time.sleep(3) # Wait for T212 to settle the trade
                        
                        # Fetch the exact entry execution price to avoid spread issues
                        fresh_portfolio = get_active_portfolio_snapshot()
                        exact_entry = price 
                        
                        if fresh_portfolio and t212_code in fresh_portfolio:
                            exact_entry = fresh_portfolio[t212_code]
                            print(f"📊 Exact entry price confirmed for {name}: {exact_entry:.2f}")

                        # Place initial physical -1% hard stop on the server based on EXACT entry
                        stop_price = exact_entry * (1 - EMERGENCY_STOP_PCT)
                        place_server_stop_loss(t212_code, qty, stop_price)
                        
                        open_positions[name] = {
                            "ticker": t212_code, 
                            "quantity": qty, 
                            "entry": exact_entry,
                            "highest": exact_entry,
                            "current_stop": stop_price
                        }
                        
                        time.sleep(2) # Protect API 
                        acc = get_account_data() # Update account balance for the next iteration
        
        time.sleep(60)

if __name__ == "__main__":
    run_scalping_bot()