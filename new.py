from credentials import API_KEY, SECRET_API_KEY
import yfinance as yf
import requests
import logging
import time
import math
import json
import os
from datetime import datetime

# configuration and logging
BASE_URL = "https://demo.trading212.com/api/v0"

# Log all actions to a file to track bugs, executed trades, and stop-loss triggers
logging.basicConfig(
    filename="trading_bot.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Custom Session for Yahoo Finance
# Spoofs a standard web browser to prevent Yahoo from blocking our server IP (HTTP 429/403)
yf_session = requests.Session()
yf_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
})

# parameters
MAX_OPEN_POSITIONS = 6        # Limit portfolio exposure to 6 concurrent trades
RISK_PER_TRADE_PCT = 0.20     # Invest 20% of TOTAL equity per trade
MIN_CASH_REQUIRED = 50.0      # Minimum free cash required to execute a trade
EMERGENCY_STOP_PCT = 0.01     # -1.0% initial hard stop right after buying
BREAK_EVEN_PROFIT = 0.005     # Activation threshold: Stock reaches +0.5% profit
BREAK_EVEN_STOP = 0.001       # Move stop loss to +0.1% to guarantee a risk-free trade
TRAIL_DISTANCE = 0.003        # Once in profit, trail the stop 0.3% behind the peak price
STATE_FILE = "botMemory.json"

# watchlists
STOCKS_EU = ["SAP", "SIE", "MBG", "VOW3", "BMW", "ALV", "DTE", "BAYN", "BAS", "EON", "RWE"]
STOCKS_US = ["TSLA", "AAPL", "NVDA", "MSFT", "AMZN", "META", "GOOGL", "AMD", "NFLX"]
STOCKS_JP = ["SONY", "TM", "HMC", "MUFG"] 

WATCHLIST_TICKERS = STOCKS_EU + STOCKS_US + STOCKS_JP

# Market hours translated to total daily minutes (Based on German Time)
MARKET_HOURS = {
    "EU": {"open": 9 * 60, "close": 17 * 60 + 30},   
    "US": {"open": 15 * 60 + 30, "close": 22 * 60},  
    "JP": {"open": 2 * 60, "close": 8 * 60}          
}

def get_market_state(region, current_minutes, is_friday):
    """Determines if a market is safe to trade, closed, or approaching the weekend."""
    open_time = MARKET_HOURS[region]["open"]
    close_time = MARKET_HOURS[region]["close"]
    
    if current_minutes < open_time or current_minutes >= close_time: 
        return "CLOSED"
    if current_minutes < (open_time + 2): 
        return "WAIT_FOR_OPEN" # Skip the first 2 minutes due to extreme volatility/spreads
    if is_friday and current_minutes >= (close_time - 10): 
        return "FRIDAY_CLOSING" # Flag for weekend liquidation to avoid Monday gaps
        
    return "TRADING"

def get_region_for_ticker(ticker):
    """Maps a stock symbol to its geographical region for market hour checks."""
    if ticker in STOCKS_EU: return "EU"
    if ticker in STOCKS_US: return "US"
    if ticker in STOCKS_JP: return "JP"
    return "US" 

def get_t212_ticker_map():
    """
    Fetches the broker's internal database.
    Strictly maps our short symbols to the correct exchange (e.g., prevents buying Canadian SAP instead of German SAP).
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
            # Force EU stocks to Xetra/DE exchanges, and US/JP to US exchanges
            if short_name in STOCKS_EU and ("d_EQ" in ticker_code or "_DE_EQ" in ticker_code):
                ticker_map[short_name] = ticker_code
            elif short_name in STOCKS_US and "_US_EQ" in ticker_code:
                ticker_map[short_name] = ticker_code
            elif short_name in STOCKS_JP and ("_US_EQ" in ticker_code or "d_EQ" in ticker_code):
                ticker_map[short_name] = ticker_code
                
    return ticker_map

def get_account_data():
    """Fetches total portfolio value (for dynamic sizing) and available free cash."""
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
    """Removes active stop-losses. This must be done to unlock shares before selling them."""
    endpoint = f"{BASE_URL}/equity/orders"
    try:
        response = requests.get(endpoint, auth=(API_KEY, SECRET_API_KEY))
        if response.status_code == 200:
            for order in response.json():
                if order.get("ticker") == t212_ticker and order.get("type") == "STOP":
                    requests.delete(f"{BASE_URL}/equity/orders/{order.get('id')}", auth=(API_KEY, SECRET_API_KEY))
    except: pass

def execute_order(t212_ticker, quantity, action):
    """Executes Market BUY/SELL orders. Trading212 requires negative quantities for selling."""
    if action == "SELL":
        cancel_all_stop_orders(t212_ticker) 
        api_qty = -quantity 
    else:
        api_qty = quantity
    payload = {"ticker": t212_ticker, "quantity": api_qty}
    response = requests.post(f"{BASE_URL}/equity/orders/market", json=payload, auth=(API_KEY, SECRET_API_KEY))
    return response.status_code == 200

def place_server_stop_loss(t212_ticker, quantity, stop_price):
    """Places a physical hard-stop on the broker's server to protect the account while offline."""
    payload = {
        "ticker": t212_ticker, 
        "quantity": -quantity, 
        "stopPrice": round(stop_price, 2)
    }
    response = requests.post(f"{BASE_URL}/equity/orders/stop", json=payload, auth=(API_KEY, SECRET_API_KEY))
    if response.status_code == 200:
        print(f"🔒 Server Stop-Loss updated at ${stop_price:.2f}")
        return True
    return False

def get_active_portfolio_snapshot():
    """
    Downloads active positions exactly once per loop to drastically reduce API calls.
    Returns None if the API crashes, acting as a fail-safe to prevent accidental position deletion.
    """
    try:
        response = requests.get(f"{BASE_URL}/equity/portfolio", auth=(API_KEY, SECRET_API_KEY))
        if response.status_code == 200:
            return {pos.get("ticker"): float(pos.get("averagePrice")) for pos in response.json()}
    except: pass
    return None 

def get_live_price(ticker):
    """Fetches real-time price using the spoofed session, with a 5-minute fallback if 1-minute data is missing."""
    yf_ticker = f"{ticker}.DE" if ticker in STOCKS_EU else ticker
    try: 
        stock = yf.Ticker(yf_ticker, session=yf_session)
        
        hist = stock.history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist['Close'].iloc[-1])
            
        hist_5m = stock.history(period="5d", interval="5m")
        if not hist_5m.empty:
            return float(hist_5m['Close'].iloc[-1])
            
    except: pass
    return None

def get_daily_high(ticker):
    """Fetches the highest price of the current day to calculate drop setups."""
    yf_ticker = f"{ticker}.DE" if ticker in STOCKS_EU else ticker
    try: 
        stock = yf.Ticker(yf_ticker, session=yf_session)
        
        hist = stock.history(period="1d", interval="1m")
        if not hist.empty:
            return float(hist['High'].max())
            
        hist_5m = stock.history(period="5d", interval="5m")
        if not hist_5m.empty:
            latest_day = hist_5m.index[-1].date()
            todays_data = hist_5m[hist_5m.index.date == latest_day]
            if not todays_data.empty:
                return float(todays_data['High'].max())
    except: pass
    return None


def load_state():
    """Loads open positions, if they exist"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                saved_positions = json.load(f)
                logging.info(f"💾 Memory loaded: {len(saved_positions)} positions found.")
                return saved_positions
        except Exception as e:
            logging.error(f"Error at loading memory: {e}")
    return {} # returns an empty dictionary, if none exists

def save_state(positions):
    """Saves current state of positions."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(positions, f, indent=4)
    except Exception as e:
        logging.error(f"Error at saving memory: {e}")

def run_scalping_bot():
    ticker_map = get_t212_ticker_map()
    open_positions = load_state()
    print(f"=== BOT READY - WATCHING {len(ticker_map)} STOCKS (EU/US/JP) ===")
    print(f"=== Current positions: {len(open_positions)} ===")

    while True:
        now_dt = datetime.now()
        now = now_dt.strftime("%H:%M:%S")
        current_minutes = now_dt.hour * 60 + now_dt.minute
        is_friday = now_dt.weekday() == 4 
        
        # Pull single source of truth from broker
        portfolio_snapshot = get_active_portfolio_snapshot()
        
        # phase 1 manage open positions
        for name in list(open_positions.keys()):
            trade = open_positions[name]
            region = get_region_for_ticker(name)
            market_state = get_market_state(region, current_minutes, is_friday)
            
            # Fail-Safe check: If the API worked but our stock is gone, the broker's stop-loss was triggered
            if portfolio_snapshot is not None:
                if trade["ticker"] not in portfolio_snapshot:
                    print(f"[{now}] ℹ️ Server Stop-Loss was triggered for {name}. Position closed.")
                    del open_positions[name]
                    continue
            
            # Friday Liquidation: Sell everything to avoid holding over the weekend
            if market_state == "FRIDAY_CLOSING":
                print(f"[{now}] ⏰ WEEKEND RISK: Liquidating {name} before Friday close...")
                if execute_order(trade["ticker"], trade["quantity"], "SELL"):
                    del open_positions[name]
                continue
                
            # Do nothing overnight, the server stop-loss keeps us safe
            if market_state == "CLOSED":
                continue 
                
            price = get_live_price(name)
            if not price: continue
            
            # Track the highest achieved price for our trailing stop logic
            if price > trade["highest"]: 
                trade["highest"] = price
            
            profit_pct = (price - trade["entry"]) / trade["entry"]
            target_stop = 0.0
            
            # Trailing Logic: Secure break-even if target reached, else maintain emergency stop
            if profit_pct >= BREAK_EVEN_PROFIT:
                break_even_price = trade["entry"] * (1 + BREAK_EVEN_STOP)
                trailing_price = trade["highest"] * (1 - TRAIL_DISTANCE)
                target_stop = max(break_even_price, trailing_price)
            else:
                target_stop = trade["entry"] * (1 - EMERGENCY_STOP_PCT)
            
            # Send API update only if the new stop is meaningfully higher (prevents rate limit bans)
            if target_stop > trade["current_stop"] * 1.001:
                print(f"[{now}] 📈 Updating Trailing Stop for {name} to {target_stop:.2f}")
                cancel_all_stop_orders(trade["ticker"])
                time.sleep(1) 
                if place_server_stop_loss(trade["ticker"], trade["quantity"], target_stop):
                    trade["current_stop"] = target_stop

        # phase 2 scan for new market dips
        acc = get_account_data()
        
        for name, t212_code in ticker_map.items():
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break # Enforce portfolio limit
                
            if name in open_positions: continue
            
            region = get_region_for_ticker(name)
            market_state = get_market_state(region, current_minutes, is_friday)
            
            if market_state != "TRADING":
                continue
            
            price = get_live_price(name)
            high = get_daily_high(name)
            if not price or not high: continue
            
            # Buy Trigger: Price dropped 0.5% from today's peak
            if ((high - price) / high) >= 0.005:
                
                # Dynamic Sizing: Use 20% of total portfolio, or go "All-In" with the rest
                target_budget = acc["total"] * RISK_PER_TRADE_PCT
                budget = target_budget if acc["free"] >= target_budget else acc["free"]
                
                # Minimum requirement guardrail
                if budget < MIN_CASH_REQUIRED: 
                    continue
                
                qty = math.floor(budget / price)
                
                if qty > 0:
                    actual_cost = qty * price
                    print(f"[{now}] 🚨 Setup for {name}! Budget allocated: {actual_cost:.2f}€. Buying {qty} shares...")
                    
                    if execute_order(t212_code, qty, "BUY"):
                        
                        time.sleep(3) # Let the broker settle the trade into the portfolio
                        
                        # Fetch the exact entry execution price to avoid spread miscalculations
                        fresh_portfolio = get_active_portfolio_snapshot()
                        exact_entry = price 
                        
                        if fresh_portfolio and t212_code in fresh_portfolio:
                            exact_entry = fresh_portfolio[t212_code]
                            print(f"📊 Exact entry price confirmed for {name}: {exact_entry:.2f}")

                        # Set the initial physical hard-stop immediately after confirmation
                        stop_price = exact_entry * (1 - EMERGENCY_STOP_PCT)
                        place_server_stop_loss(t212_code, qty, stop_price)
                        
                        open_positions[name] = {
                            "ticker": t212_code, 
                            "quantity": qty, 
                            "entry": exact_entry,
                            "highest": exact_entry,
                            "current_stop": stop_price
                        }
                        
                        time.sleep(2) # Protect API from rapid requests
                        acc = get_account_data() # Update cash variables before next loop
        save_state(open_positions)

        time.sleep(60)

if __name__ == "__main__":
    run_scalping_bot()