# Automated Algorithmic Scalping Bot

A robust, production-ready algorithmic trading bot designed for the **Trading 212 (Demo/Live) REST API**. The script scans a custom watchlist across global markets (EU, US, JP), detects price dips from daily highs, executes automated market orders, and applies tight server-side risk management.

## Features

- **Dynamic Position Sizing:** Automatically allocates 20% of total portfolio equity per trade. If free cash is limited, it dynamically switches to an "All-In" mode using the remaining balance.
- **Advanced Risk Control:** - Immediate physical **-1.0% hard stop-loss** placed directly on the broker's server upon execution.
  - Dynamic **Break-Even locking (+0.1%)** once a position hits +0.5% profit.
  - Automatic **Trailing Stop (0.3% distance)** to let winning trades run while securing profits.
- **Persistent State Management:** Saves the state of open positions to a local `botMemory.json` file every minute. This allows the bot to be stopped, restarted, or updated without losing track of active trades.
- **Anti-Scraping Protection:** Implements a custom HTTP session with browser spoofing (User-Agent headers) and a multi-interval fallback (1m to 5m) to ensure uninterrupted market data streams from Yahoo Finance.
- **Weekend Risk Mitigation:** Automatically detects Fridays and liquidates all open positions 10 minutes before market close to eliminate overnight gap risks over the weekend.

## Architecture

The bot runs on a continuous infinite loop executing two synchronized phases every 60 seconds:
1. **Phase 1 (Portfolio Management):** Fetches a single portfolio snapshot to respect API rate limits, verifies active positions, tracks peak values, and dynamically updates trailing stops.
2. **Phase 2 (Market Scanning):** Monitors target watchlists during native market hours, calculates technical dip thresholds (-0.5% from daily high), and evaluates capital availability before executing entry orders.

## Project Structure

```text
├── new.py                  # Main execution script containing core logic
├── credentials.py          # Private API keys (Excluded from version control)
├── botMemory.json          # Local state database tracking active positions
└── trading_bot.log         # Persistent runtime log file
