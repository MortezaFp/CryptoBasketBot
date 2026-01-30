# Wallex Crypto Basket Bot

A robust, automated Python bot for rebalancing a cryptocurrency portfolio on the Wallex (Iran) exchange. Designed to run 24/7 as a background terminal application.

## 🚀 Features

- **Strategy**: "Threshold Rebalancing". Only trades when an asset deviates by more than **5%** from its target allocation.
- **Circuit Breaker**: "Panic Protection". Will **SKIP buying** any coin that has dropped more than **15%** in the last 24 hours.
- **Persistent Process**: Runs in an infinite loop (e.g., every 1 hour) with error handling to recover from network disconnects automatically.
- **Simulation Mode**: Includes a full paper-trading simulator that uses **Real-Time Market Data** with a fake wallet to test the strategy risk-free.

## 📊 Target Allocation

The current strategy targets a balanced basket with a 20% USDT cash reserve for buying dips.

| Asset    | Target %           |
| :------- | :----------------- |
| **BTC**  | 30%                |
| **ETH**  | 20%                |
| **USDT** | 20% (Base/Reserve) |
| **SOL**  | 10%                |
| **BNB**  | 10%                |
| **XRP**  | 10%                |

## 🛠️ Usage

### 1. Setup

Install requirements:

```bash
pip install -r requirements.txt
```

### 2. Configuration

Create a `config.ini` file in the project folder with your Wallex API key:

```ini
[wallex]
api_key = YOUR_API_KEY_HERE
```

### 3. Modes

#### A. 🧪 Simulation Mode (Safe Testing)

Runs with a fake wallet (starts with 1000 USDT) but fetches **REAL live prices**. It saves the simulation state to `simulation_state.json` so you can stop and resume anytime.

- **Command**: `python main_test.py`
- **Logs**: Writes detailed trade info to `simulation_log.txt`.

#### B. 💸 Live Trading Mode (Real Money)

Runs with your **REAL Wallex account**. Executes actual BUY/SELL orders when thresholds are met.

- **Command**: `python main.py`
- **Safety**: Logs all actions to the console with timestamps.

## ⚠️ Disclaimer

This bot executes real trades using your API key. Cryptocurrency trading involves significant risk. Use this software at your own risk. The authors are not responsible for financial losses.
