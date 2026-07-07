"""
Crypto Telegram Signal Bot
Analyzes BTC, ETH, SOL, ADA, XRP hourly.
Fetches Binance market data, queries CoinMarketCap and news via Gemini Search Grounding,
and sends beautiful Persian analysis signals to Telegram.
No real trading is executed.
"""

import os
import sys
import time
import logging
import argparse
import requests
import queue
import threading
import configparser
import pandas as pd
import pandas_ta as ta
from typing import Tuple, Optional
from google import genai
from google.genai import types

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("SignalBot")

TARGET_COINS = ["BTC", "ETH", "SOL", "ADA", "XRP"]
SLEEP_INTERVAL = 3600  # 1 hour in seconds


class TelegramNotifier:
    """Handles thread-safe, sequential Telegram notifications to preserve ordering for multiple chat IDs"""

    def __init__(self, token: str, chat_ids: list[str]):
        self.token = token
        self.chat_ids = chat_ids
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.queue = queue.Queue()
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def send_message(self, message: str, retries: int = 5):
        if not self.token or not self.chat_ids:
            return
        self.queue.put((message, retries))

    def _worker_loop(self):
        while True:
            message, retries = self.queue.get()
            try:
                for chat_id in self.chat_ids:
                    url = f"{self.base_url}/sendMessage"
                    payload = {
                        "chat_id": chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    }
                    for attempt in range(retries):
                        try:
                            resp = requests.post(url, json=payload, timeout=15)
                            if resp.status_code == 200:
                                break
                            logger.warning(
                                f"Telegram send failed to {chat_id} (Attempt {attempt+1}/{retries}): {resp.text}"
                            )
                        except Exception as e:
                            logger.warning(
                                f"Telegram connection error to {chat_id} (Attempt {attempt+1}/{retries}): {e}"
                            )
                        time.sleep(3)
            finally:
                self.queue.task_done()

    def flush(self):
        self.queue.join()


def load_bot_config() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Loads Telegram and Gemini credentials from environment or config.ini"""
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")

    # Resolve paths relative to python file directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, "config.ini")

    if os.path.exists(config_path):
        config = configparser.ConfigParser()
        config.read(config_path)

        if not telegram_token and config.has_section("telegram"):
            telegram_token = config.get("telegram", "bot_token", fallback=None)
            telegram_chat_id = config.get("telegram", "chat_id", fallback=None)

        if not gemini_api_key:
            if config.has_section("gemini"):
                gemini_api_key = config.get("gemini", "api_key", fallback=None)
            elif config.has_section("google"):
                gemini_api_key = config.get("google", "api_key", fallback=None)
            elif config.has_section("wallex"):
                # Fallback to wallex section check, just in case they added api_key there
                gemini_api_key = config.get("wallex", "gemini_api_key", fallback=None)

    return telegram_token, telegram_chat_id, gemini_api_key


def fetch_candles(symbol: str) -> list:
    """Fetches 1-hour klines (candles) from Wallex"""
    try:
        url = "https://api.wallex.ir/v1/udf/history"
        end_ts = int(time.time())
        start_ts = end_ts - (300 * 60 * 60) # 300 hours ago
        params = {
            "symbol": symbol,
            "resolution": "60",
            "from": start_ts,
            "to": end_ts,
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("s") == "ok":
            candles = []
            for i in range(len(data["t"])):
                candles.append(
                    {
                        "time": int(data["t"][i]),
                        "open": float(data["o"][i]),
                        "high": float(data["h"][i]),
                        "low": float(data["l"][i]),
                        "close": float(data["c"][i]),
                        "volume": float(data["v"][i]),
                    }
                )
            logger.info(f"[OK] Successfully fetched Wallex candles for {symbol}")
            return candles
        return []
    except Exception as e:
        logger.error(f"Error fetching Wallex candles for {symbol}: {e}")
        return []


def calculate_indicators(candles: list) -> dict:
    """Computes technical analysis indicators from raw candles"""
    df = pd.DataFrame(candles)
    df["close"] = pd.to_numeric(df["close"])
    df["high"] = pd.to_numeric(df["high"])
    df["low"] = pd.to_numeric(df["low"])
    df["open"] = pd.to_numeric(df["open"])
    df["volume"] = pd.to_numeric(df["volume"])

    # Technical indicator calculations
    df["ema_9"] = ta.ema(df["close"], length=9)
    df["ema_21"] = ta.ema(df["close"], length=21)
    df["sma_200"] = ta.sma(df["close"], length=200)
    df["rsi"] = ta.rsi(df["close"], length=14)

    bb = ta.bbands(df["close"], length=20)
    bb_lower_col = next((col for col in bb.columns if col.startswith("BBL_20_")), None)
    bb_upper_col = next((col for col in bb.columns if col.startswith("BBU_20_")), None)
    bb_mid_col = next((col for col in bb.columns if col.startswith("BBM_20_")), None)

    if bb_lower_col and bb_upper_col and bb_mid_col:
        df["bb_lower"] = bb[bb_lower_col]
        df["bb_upper"] = bb[bb_upper_col]
        df["bb_mid"] = bb[bb_mid_col]
    else:
        # Fallbacks
        df["bb_lower"] = df["close"] * 0.95
        df["bb_upper"] = df["close"] * 1.05
        df["bb_mid"] = df["close"]

    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    last_row = df.iloc[-1]
    return {
        "close": float(last_row["close"]),
        "open": float(last_row["open"]),
        "high": float(last_row["high"]),
        "low": float(last_row["low"]),
        "volume": float(last_row["volume"]),
        "ema_9": (float(last_row["ema_9"]) if not pd.isna(last_row["ema_9"]) else 0.0),
        "ema_21": (
            float(last_row["ema_21"]) if not pd.isna(last_row["ema_21"]) else 0.0
        ),
        "sma_200": (
            float(last_row["sma_200"]) if not pd.isna(last_row["sma_200"]) else 0.0
        ),
        "rsi": float(last_row["rsi"]) if not pd.isna(last_row["rsi"]) else 50.0,
        "bb_lower": float(last_row["bb_lower"]),
        "bb_upper": float(last_row["bb_upper"]),
        "bb_mid": float(last_row["bb_mid"]),
        "atr": float(last_row["atr"]) if not pd.isna(last_row["atr"]) else 0.0,
    }


def fetch_orderbook(symbol: str) -> dict:
    """Fetches orderbook depth from Wallex"""
    try:
        url = "https://api.wallex.ir/v1/depth"
        params = {"symbol": symbol}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        result = data.get("result", {})
        bids_data = result.get("bid", [])
        asks_data = result.get("ask", [])

        bids = [[float(item["price"]), float(item["quantity"])] for item in bids_data]
        asks = [[float(item["price"]), float(item["quantity"])] for item in asks_data]

        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        spread = best_ask - best_bid
        spread_pct = (spread / best_bid * 100) if best_bid > 0 else 0.0

        bid_vol_5 = sum(q for p, q in bids[:5])
        ask_vol_5 = sum(q for p, q in asks[:5])
        bid_ask_ratio_5 = bid_vol_5 / ask_vol_5 if ask_vol_5 > 0 else 1.0

        total_bid_vol = sum(q for p, q in bids)
        total_ask_vol = sum(q for p, q in asks)
        total_bid_ask_ratio = total_bid_vol / total_ask_vol if total_ask_vol > 0 else 1.0

        logger.info(f"[OK] Successfully fetched Wallex orderbook for {symbol}")
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_pct": spread_pct,
            "bid_ask_ratio_5": bid_ask_ratio_5,
            "total_bid_ask_ratio": total_bid_ask_ratio,
        }
    except Exception as e:
        logger.error(f"Error fetching Wallex orderbook for {symbol}: {e}")
        return {
            "best_bid": 0.0,
            "best_ask": 0.0,
            "spread": 0.0,
            "spread_pct": 0.0,
            "bid_ask_ratio_5": 1.0,
            "total_bid_ask_ratio": 1.0,
        }


def fetch_cmc_data_and_news(coin: str) -> dict:
    """Fetches CoinMarketCap metrics and latest news directly using public APIs"""
    slugs = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "ADA": "cardano",
        "XRP": "xrp"
    }
    slug = slugs.get(coin, coin.lower())
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    result = {
        "rank": "N/A",
        "market_cap": 0.0,
        "volume_24h": 0.0,
        "price_change_24h": 0.0,
        "news": []
    }
    
    try:
        # 1. Fetch coin detail metrics
        url = f"https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail?slug={slug}"
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            coin_id = data.get("id")
            stats = data.get("statistics", {})
            
            result["rank"] = stats.get("rank", "N/A")
            result["market_cap"] = stats.get("marketCap", 0.0)
            result["volume_24h"] = stats.get("volume24h", 0.0)
            result["price_change_24h"] = stats.get("priceChangePercentage24h", 0.0)
            
            # 2. Fetch latest news for this coin using the retrieved coin_id
            if coin_id:
                news_url = f"https://api.coinmarketcap.com/content/v3/news?coins={coin_id}&page=1&size=5"
                news_resp = requests.get(news_url, headers=headers, timeout=12)
                if news_resp.status_code == 200:
                    news_data = news_resp.json().get("data", [])
                    news_list = []
                    for item in news_data[:3]: # top 3 news items
                        meta = item.get("meta", {})
                        title = meta.get("title")
                        subtitle = meta.get("subtitle")
                        if title:
                            news_list.append({
                                "title": title,
                                "subtitle": subtitle or ""
                            })
                    result["news"] = news_list
        logger.info(f"[OK] Successfully fetched CoinMarketCap data for {coin}")
        return result
    except Exception as e:
        logger.error(f"Error fetching CoinMarketCap data for {coin}: {e}")
        return result


def get_crypto_signal(
    client: genai.Client, coin: str, indicators: dict, orderbook: dict, cmc_data: dict
) -> str:
    """Calls Gemini LLM to analyze coin metrics and generate an HTML-formatted Persian Telegram signal"""
    
    # Format the news items into a string for the prompt
    news_str = ""
    if cmc_data.get("news"):
        for i, item in enumerate(cmc_data["news"], 1):
            news_str += f"{i}. {item['title']}\n"
            if item.get("subtitle"):
                news_str += f"   - {item['subtitle']}\n"
    else:
        news_str = "No recent news found.\n"
        
    prompt = f"""
You are a highly experienced quantitative cryptocurrency trader and senior market analyst.
Your task is to analyze the provided market and sentiment data for {coin} (against USDT) and output a beautifully formatted, professional Telegram signal message in Persian (Farsi).

--- MARKET DATA FOR {coin} ---
Current Price: {indicators['close']:.4f}
Open/High/Low: Open={indicators['open']:.4f}, High={indicators['high']:.4f}, Low={indicators['low']:.4f}
24h Volume (Wallex): {indicators['volume']:.2f}

Technical Indicators (1h chart):
- SMA 200: {indicators['sma_200']:.4f}
- EMA 9: {indicators['ema_9']:.4f}
- EMA 21: {indicators['ema_21']:.4f}
- RSI (14): {indicators['rsi']:.2f}
- Bollinger Bands: Upper={indicators['bb_upper']:.4f}, Mid={indicators['bb_mid']:.4f}, Lower={indicators['bb_lower']:.4f}
- ATR (14): {indicators['atr']:.4f}

Order Book Metrics:
- Best Bid: {orderbook['best_bid']:.4f} | Best Ask: {orderbook['best_ask']:.4f}
- Bid-Ask Spread: {orderbook['spread']:.4f} ({orderbook['spread_pct']:.4f}%)
- Bid/Ask Volume Ratio (Top 5 levels): {orderbook['bid_ask_ratio_5']:.2f}
- Bid/Ask Volume Ratio (Top 20 levels): {orderbook['total_bid_ask_ratio']:.2f}

CoinMarketCap Statistics:
- Rank: {cmc_data.get('rank', 'N/A')}
- Market Cap: ${cmc_data.get('market_cap', 0.0):,.0f}
- 24h Trading Volume: ${cmc_data.get('volume_24h', 0.0):,.0f}
- 24h Price Change Percentage: {cmc_data.get('price_change_24h', 0.0):+.2f}%

Latest News Headlines:
{news_str}

--- TELEGRAM FORMATTING INSTRUCTIONS ---
You must write a beautifully structured message in Persian (Farsi) using Telegram HTML tags.
Follow these guidelines to create a modern, sleek interface layout:
1. Identify the coin, signal, and current price in the header. Use an emoji based on the signal (🟢 for BUY, 🔴 for SELL, 🟡 for HOLD).
2. The main signal recommendation, confidence percentage, and trade coordinates (Entry range, Target price levels, Stop Loss) MUST be visible in the main message body.
3. ALL detailed sub-analyses MUST be placed inside Telegram's modern **expandable blockquotes** so the message is extremely clean, compact, and readers can tap to expand details. To do this, wrap the sections in `<blockquote expandable>...</blockquote>` tags.
4. IMPORTANT: Do NOT use `<br>` or `<br/>` tags for line breaks as Telegram Bot API throws parsing errors for them. Simply use standard newline characters (`\n`) in your output.
5. Use monospaced font `<code>` tags for prices, numbers, and targets to make them easy to read.

Message structure in Persian:
- Header: e.g. 📊 **سیگنال خرید #{coin}** (🟢/🔴/🟡)
- 💵 **قیمت فعلی**: <code>${indicators['close']:,}</code>
- 🎯 **سیگنال نهایی**:
  - **پیشنهاد**: [خرید / فروش / نگهداری]
  - **درصد اطمینان**: <code>[X%]</code>
- 🚀 **مراحل ورود پیشنهادی** (Only if BUY/HOLD):
  - **محدوده خرید پیشنهادی**: <code>[Price Range]</code>
  - **اهداف قیمتی (Take Profit)**:
    - هدف اول: <code>[Target 1]</code>
    - هدف دوم: <code>[Target 2]</code>
  - 🛑 **حد ضرر (Stop Loss)**: <code>[Stop Loss Price]</code>
  
- Expandable Block 1:
<blockquote expandable>📊 <b>آمار کوین‌مارکت‌کپ و اخبار اخیر:</b>
رتبه کوین‌مارکت‌کپ: <code>#{cmc_data.get('rank', 'N/A')}</code>
ارزش بازار: <code>${cmc_data.get('market_cap', 0.0):,.0f} دلار</code>
تغییرات ۲۴ ساعته: <code>{cmc_data.get('price_change_24h', 0.0):+.2f}%</code>

<b>اخبار و رویدادهای مهم:</b>
[Summarize/translate news items here in Persian]</blockquote>

- Expandable Block 2:
<blockquote expandable>⚙️ <b>تحلیل تکنیکال (تحلیل اندیکاتورها):</b>
RSI: <code>{indicators['rsi']:.2f}</code>
SMA 200: <code>{indicators['sma_200']:.4f}</code>
وضعیت باندهای بولینگر و میانگین‌های متحرک... [Short technical vibe summary]</blockquote>

- Expandable Block 3:
<blockquote expandable>⚖️ <b>تحلیل دفتر سفارشات (Orderbook):</b>
اسپرد قیمت: <code>{orderbook['spread_pct']:.4f}%</code>
نسبت خریداران به فروشندگان (۵ سطح): <code>{orderbook['bid_ask_ratio_5']:.2f}</code>
نسبت کل عمق: <code>{orderbook['total_bid_ask_ratio']:.2f}</code>
[Short volume flow summary]</blockquote>

Output ONLY the raw HTML message in Persian, with no markdown code block formatting (like ```html ... ```). Keep it clean.
"""

    models_to_try = [
        "gemini-flash-latest",
        "gemini-flash-lite-latest",
        "gemini-2.5-flash",
        "gemma-4-31b-it",
    ]

    for model in models_to_try:
        try:
            logger.info(f"Requesting analysis from {model} for {coin}...")
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                ),
            )
            if response and response.text:
                res_text = response.text.strip()
                # Clean up any potential markdown wrap if the model ignored instructions
                if res_text.startswith("```"):
                    res_text = res_text.split("\n", 1)[1]
                if res_text.endswith("```"):
                    res_text = res_text.rsplit("\n", 1)[0]
                
                # Sanitize any <br> tags the model might output
                res_text = res_text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
                return res_text.strip()
        except Exception as e:
            logger.error(
                f"Error generating signal on {model} for {coin}: {e}"
            )
            time.sleep(1)

    return f"❌ <b>خطا در تحلیل {coin}</b>\nمتأسفانه ارتباط با هوش مصنوعی برقرار نشد."


def run_signal_cycle(notifier: Optional[TelegramNotifier], client: genai.Client):
    """Executes the analysis cycle for all targets and sends messages to Telegram"""
    logger.info("==========================================")
    logger.info("       STARTING HOUR ANALYSIS CYCLE       ")
    logger.info("==========================================")

    for coin in TARGET_COINS:
        symbol = f"{coin}USDT"
        logger.info(f"Analyzing {coin}...")

        # 1. Fetch candles
        candles = fetch_candles(symbol)
        if len(candles) < 200:
            logger.warning(
                f"Insufficient candles fetched for {coin}. Skipping analysis."
            )
            continue

        # 2. Compute Indicators
        indicators = calculate_indicators(candles)

        # 3. Fetch Orderbook
        orderbook = fetch_orderbook(symbol)

        # 4. Fetch CoinMarketCap and news data directly
        cmc_data = fetch_cmc_data_and_news(coin)

        # 5. Request AI Signal
        report = get_crypto_signal(client, coin, indicators, orderbook, cmc_data)

        # 6. Notify via Telegram
        if notifier:
            logger.info(f"Queueing Telegram notification for {coin}...")
            # Limit check (Telegram's message limit is 4096 characters)
            if len(report) > 4000:
                parts = [report[i : i + 4000] for i in range(0, len(report), 4000)]
                for part in parts:
                    notifier.send_message(part)
            else:
                notifier.send_message(report)
        else:
            logger.info(f"Signal Report for {coin}:\n{report}\n")

        # Sleep to avoid rate limits
        time.sleep(5)

    if notifier:
        logger.info("Flushing Telegram notifications queue...")
        notifier.flush()

    logger.info("Cycle completed successfully.")


def main():
    # Load credentials
    tg_token, tg_chat_id, gemini_key = load_bot_config()

    if not gemini_key:
        logger.critical(
            "CRITICAL: GEMINI_API_KEY is not set in environment or config.ini!"
        )
        sys.exit(1)

    # Initialize Gemini SDK Client
    try:
        gemini_client = genai.Client(api_key=gemini_key)
    except Exception as e:
        logger.critical(f"Failed to initialize Gemini Client: {e}")
        sys.exit(1)

    # Initialize Telegram Notifier
    notifier = None
    if tg_token and tg_chat_id:
        chat_ids = [cid.strip() for cid in tg_chat_id.split(",") if cid.strip()]
        if chat_ids:
            notifier = TelegramNotifier(tg_token, chat_ids)
            logger.info(
                f"[OK] Telegram notifications successfully initialized for {len(chat_ids)} chats"
            )
        else:
            logger.warning("Telegram chat_id was empty or invalid.")
    else:
        logger.warning(
            "Telegram credentials not found. Messages will be outputted to console only."
        )

    logger.info("Executing analysis cycle...")
    run_signal_cycle(notifier, gemini_client)


if __name__ == "__main__":
    main()
