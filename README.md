# Wallex Crypto Basket Bot (Rebalancing & Dip Catcher)

A sophisticated, automated Python bot for rebalancing a cryptocurrency portfolio on the Wallex (Iran) exchange. Designed for stability, low fees, and smart "dip catching" for small-to-medium portfolios.

## 🚀 Key Features

### 1. 🧠 Smart Threshold Rebalancing

Instead of trading on every minute price change, the bot only trades when an asset deviates significantly from its target.

- **Dynamic Thresholds**: Each coin has its own tolerance based on volatility (e.g., BTC 3%, SOL 7%).
- **Fee Optimization**: Prevents small "churn" trades that waste money on fees (especially for Gold/XAUT).

### 2. 📉 Falling Knife & Dip Catching

The bot includes advanced logic to handle market crashes safely:

- **Maker-First Execution**: Places Limit Orders at the Best Bid/Ask to save on fees.
- **Tiered Buying**:
  - **Minor Dip (-2% to -20%)**: Places buy orders slightly below market (-2%).
  - **Major Crash (-20% to -35%)**: Places aggressive "stink bids" significantly below market (-5%) to catch wicks.
- **Circuit Breaker**: If a coin drops more than **35%** in 24h, buying is suspended to prevent catching a falling knife until it stabilizes.

### 3. 🧪 Realistic Simulation Mode

Test your strategy risk-free with the advanced simulator:

- **Real-Time Data**: Fetches live prices from Wallex API.
- **Persistent State**: Saves your simulated wallet to `simulation_state.json` (resumes where allowed).
- **Order Book Simulation**: Limit orders only fill if the real market price actually crosses your limit price.
- **"Infinite Money Glitch" Free**: Accurately tracks balances across restarts.

## 📊 Target Allocation

The strategy targets a diversified basket with a 15% USDT cash reserve for opportunity buying.

| Asset    | Target % | Threshold | Notes                   |
| :------- | :------- | :-------- | :---------------------- |
| **BTC**  | 25%      | ±3%       | Core Holding            |
| **ETH**  | 15%      | ±3%       | Core Holding            |
| **XAUT** | 15%      | ±5%       | Tether Gold (XAUT)      |
| **USDT** | 15%      | ±2%       | Reserve for Buying Dips |
| **SOL**  | 10%      | ±7%       | High Volatility         |
| **BNB**  | 10%      | ±5%       | Exchange Coin           |
| **XRP**  | 10%      | ±7%       | High Volatility         |

## 🛠️ Usage

### 1. Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

### 2. Configuration

Ensure `config.ini` is set up with your credentials:

```ini
[wallex]
api_key = YOUR_WALLEX_API_KEY

[telegram]
# Optional: Get status updates on your phone
bot_token = YOUR_TELEGRAM_BOT_TOKEN
chat_id = YOUR_TELEGRAM_CHAT_ID
```

### 3. Running the Bot

#### A. 🧪 Simulation Mode (Recommended First)

Runs an infinite loop with a **Fake Wallet** (starts with $1,000) but **Real Market Data**.

```bash
python main_test.py
```

- **Log File**: `simulation_log.txt`
- **Reset**: Delete `simulation_state.json` to start fresh.

#### B. 💸 Live Trading Mode

Runs with your **Real Money**.

```bash
python main.py
```

- **Safety**: `MIN_TRADE_USDT` is set to $1.0 to accommodate small portfolios.

#### C. 🤖 AI Swing Trading Mode (Stateless Cron)

A stateless, 15-minute cron-based swing trading strategy for 10 target coins. It uses a central USDT bank, technical indicators (SMA, EMA, RSI, Bollinger Bands, ATR), and AI validation via Google Gemini to make high-probability trades with strict risk management.

**Configuration:**
- Provide `GEMINI_API_KEY` in your environment or GitHub Secrets.
- Run locally or deploy via the provided GitHub Action (`.github/workflows/swing.yml`).

```bash
python main_swing.py
```

## ⚠️ Risk Verification

- **Test First**: Always run the simulation for at least 24 hours before deploying real funds.
- **Network Issues**: The bot handles connection timeouts automatically.
- **Funds**: Ensure you have a small amount of USDT in your Wallex account before starting.

## 📄 License

Open Source. Use at your own risk.
