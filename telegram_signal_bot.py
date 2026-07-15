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
import re
from html.parser import HTMLParser
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


class TelegramHTMLSanitizer(HTMLParser):
    """Parses and sanitizes HTML text to ensure it complies with Telegram's supported tag list,
    escapes raw characters, and fixes nesting/unclosed tags to prevent sendMessage parsing errors.
    """

    def __init__(self):
        super().__init__()
        self.result = []
        self.tag_stack = []
        self.supported_tags = {
            "b",
            "strong",
            "i",
            "em",
            "u",
            "ins",
            "s",
            "strike",
            "del",
            "span",
            "tg-spoiler",
            "a",
            "code",
            "pre",
            "blockquote",
        }

    def handle_starttag(self, tag, attrs):
        if tag in self.supported_tags:
            # Check for blockquote expandable
            if tag == "blockquote":
                is_expandable = any(attr[0] == "expandable" for attr in attrs)
                if is_expandable:
                    self.result.append("<blockquote expandable>")
                else:
                    self.result.append("<blockquote>")
            elif tag == "span":
                # Only keep tg-spoiler spans
                has_spoiler = any(
                    attr[0] == "class" and attr[1] == "tg-spoiler" for attr in attrs
                )
                if has_spoiler:
                    self.result.append('<span class="tg-spoiler">')
                else:
                    tag = None  # ignore
            elif tag == "a":
                href = next((attr[1] for attr in attrs if attr[0] == "href"), None)
                if href:
                    self.result.append(f'<a href="{href}">')
                else:
                    tag = None
            else:
                self.result.append(f"<{tag}>")

            if tag:
                self.tag_stack.append(tag)
        elif tag in ("br", "p", "div", "li"):
            self.result.append("\n")

    def handle_endtag(self, tag):
        if tag in self.supported_tags:
            if tag in self.tag_stack:
                while self.tag_stack:
                    top_tag = self.tag_stack.pop()
                    self.result.append(f"</{top_tag}>")
                    if top_tag == tag:
                        break
        elif tag in ("p", "div", "li"):
            self.result.append("\n")

    def handle_data(self, data):
        # Escape raw characters for Telegram HTML parser
        escaped = data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.result.append(escaped)

    def get_clean_html(self) -> str:
        while self.tag_stack:
            top_tag = self.tag_stack.pop()
            self.result.append(f"</{top_tag}>")
        cleaned = "".join(self.result)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()


def clean_telegram_html(html_text: str) -> str:
    """Helper function to clean HTML for Telegram Bot API"""
    try:
        sanitizer = TelegramHTMLSanitizer()
        sanitizer.feed(html_text)
        return sanitizer.get_clean_html()
    except Exception as e:
        logger.error(f"HTML sanitization error: {e}")
        # Basic fallback cleaning
        text = (
            html_text.replace("<br>", "\n")
            .replace("<br/>", "\n")
            .replace("<br />", "\n")
        )
        text = re.sub(r"</?(p|div|ul|ol|li)[^>]*>", "\n", text)
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return text.strip()


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
        # Apply HTML validation and cleaning to message
        cleaned_message = clean_telegram_html(message)
        self.queue.put((cleaned_message, retries))

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
    """Fetches 1-hour klines (candles) from Coinbase Exchange"""
    try:
        product_id = symbol.replace("USDT", "-USDT")
        url = f"https://api.exchange.coinbase.com/products/{product_id}/candles"
        # granularity=3600 is 1 hour
        params = {"granularity": "3600"}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()

        # Coinbase returns [time, low, high, open, close, volume] from newest to oldest
        # We need to reverse the list to have oldest to newest
        data = resp.json()[::-1]
        candles = []
        for item in data:
            candles.append(
                {
                    "time": int(item[0]),
                    "low": float(item[1]),
                    "high": float(item[2]),
                    "open": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                }
            )
        logger.info(f"[OK] Successfully fetched Coinbase candles for {symbol}")
        return candles
    except Exception as e:
        logger.error(f"Error fetching Coinbase candles for {symbol}: {e}")
        return []


def _safe_float(val, default=0.0):
    """Safely convert a value to float, returning default if NaN or None"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def calculate_indicators(candles: list) -> dict:
    """Computes comprehensive technical analysis indicators from raw candles.
    Returns both current values and trend context for richer LLM analysis."""
    df = pd.DataFrame(candles)
    df["close"] = pd.to_numeric(df["close"])
    df["high"] = pd.to_numeric(df["high"])
    df["low"] = pd.to_numeric(df["low"])
    df["open"] = pd.to_numeric(df["open"])
    df["volume"] = pd.to_numeric(df["volume"])

    # ===== CORE INDICATORS (existing) =====
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
        df["bb_lower"] = df["close"] * 0.95
        df["bb_upper"] = df["close"] * 1.05
        df["bb_mid"] = df["close"]

    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # ===== NEW: MACD (12, 26, 9) =====
    macd_result = ta.macd(df["close"], fast=12, slow=26, signal=9)
    macd_col = next((c for c in macd_result.columns if c.startswith("MACD_")), None)
    macd_signal_col = next((c for c in macd_result.columns if c.startswith("MACDs_")), None)
    macd_hist_col = next((c for c in macd_result.columns if c.startswith("MACDh_")), None)
    if macd_col:
        df["macd"] = macd_result[macd_col]
    if macd_signal_col:
        df["macd_signal"] = macd_result[macd_signal_col]
    if macd_hist_col:
        df["macd_hist"] = macd_result[macd_hist_col]

    # ===== NEW: Stochastic RSI =====
    stoch_rsi = ta.stochrsi(df["close"], length=14, rsi_length=14, k=3, d=3)
    stochrsi_k_col = next((c for c in stoch_rsi.columns if c.startswith("STOCHRSIk_")), None)
    stochrsi_d_col = next((c for c in stoch_rsi.columns if c.startswith("STOCHRSId_")), None)
    if stochrsi_k_col:
        df["stoch_rsi_k"] = stoch_rsi[stochrsi_k_col]
    if stochrsi_d_col:
        df["stoch_rsi_d"] = stoch_rsi[stochrsi_d_col]

    # ===== NEW: ADX + Directional Indicators =====
    adx_result = ta.adx(df["high"], df["low"], df["close"], length=14)
    adx_col = next((c for c in adx_result.columns if c.startswith("ADX_")), None)
    dmp_col = next((c for c in adx_result.columns if c.startswith("DMP_")), None)
    dmn_col = next((c for c in adx_result.columns if c.startswith("DMN_")), None)
    if adx_col:
        df["adx"] = adx_result[adx_col]
    if dmp_col:
        df["di_plus"] = adx_result[dmp_col]
    if dmn_col:
        df["di_minus"] = adx_result[dmn_col]

    # ===== NEW: Volume SMA (20-period) =====
    df["vol_sma_20"] = ta.sma(df["volume"], length=20)

    # ===== Extract last row values =====
    last = df.iloc[-1]
    close = float(last["close"])
    bb_lower = float(last["bb_lower"])
    bb_upper = float(last["bb_upper"])

    # ===== NEW: Multi-candle trend context =====
    # Price changes over different lookbacks
    price_change_4h = 0.0
    price_change_12h = 0.0
    price_change_24h = 0.0
    if len(df) >= 5:
        price_change_4h = ((close / float(df.iloc[-5]["close"])) - 1) * 100
    if len(df) >= 13:
        price_change_12h = ((close / float(df.iloc[-13]["close"])) - 1) * 100
    if len(df) >= 25:
        price_change_24h = ((close / float(df.iloc[-25]["close"])) - 1) * 100

    # RSI trajectory over last 5 candles
    rsi_trajectory = "stable"
    if len(df) >= 6 and "rsi" in df.columns:
        rsi_recent = df["rsi"].iloc[-5:].dropna()
        if len(rsi_recent) >= 3:
            rsi_diff = float(rsi_recent.iloc[-1]) - float(rsi_recent.iloc[0])
            if rsi_diff > 5:
                rsi_trajectory = "rising"
            elif rsi_diff < -5:
                rsi_trajectory = "falling"

    # EMA crossover detection - when did EMA9 last cross EMA21?
    ema_cross_type = "none"
    ema_cross_candles_ago = -1
    if "ema_9" in df.columns and "ema_21" in df.columns:
        ema_diff = df["ema_9"] - df["ema_21"]
        ema_diff_clean = ema_diff.dropna()
        if len(ema_diff_clean) >= 2:
            for i in range(len(ema_diff_clean) - 1, 0, -1):
                curr = float(ema_diff_clean.iloc[i])
                prev = float(ema_diff_clean.iloc[i - 1])
                if curr > 0 and prev <= 0:
                    ema_cross_type = "bullish"
                    ema_cross_candles_ago = len(ema_diff_clean) - 1 - i
                    break
                elif curr < 0 and prev >= 0:
                    ema_cross_type = "bearish"
                    ema_cross_candles_ago = len(ema_diff_clean) - 1 - i
                    break

    # MACD histogram trend (last 3 candles)
    macd_hist_trend = "stable"
    if "macd_hist" in df.columns:
        mh = df["macd_hist"].iloc[-3:].dropna()
        if len(mh) >= 3:
            if float(mh.iloc[-1]) > float(mh.iloc[-2]) > float(mh.iloc[-3]):
                macd_hist_trend = "rising"
            elif float(mh.iloc[-1]) < float(mh.iloc[-2]) < float(mh.iloc[-3]):
                macd_hist_trend = "falling"

    # Bollinger Band position (0% = at lower, 100% = at upper)
    bb_range = bb_upper - bb_lower
    bb_position_pct = ((close - bb_lower) / bb_range * 100) if bb_range > 0 else 50.0

    # Volume vs average
    vol_current = float(last["volume"])
    vol_sma = _safe_float(last.get("vol_sma_20"), vol_current)
    vol_ratio = (vol_current / vol_sma) if vol_sma > 0 else 1.0

    # ===== NEW: Support & Resistance from recent swing highs/lows =====
    lookback = min(48, len(df))  # last 48 candles (2 days)
    recent = df.iloc[-lookback:]
    support_level = float(recent["low"].min())
    resistance_level = float(recent["high"].max())

    return {
        # Core price data
        "close": close,
        "open": float(last["open"]),
        "high": float(last["high"]),
        "low": float(last["low"]),
        "volume": vol_current,
        # Moving averages
        "ema_9": _safe_float(last.get("ema_9")),
        "ema_21": _safe_float(last.get("ema_21")),
        "sma_200": _safe_float(last.get("sma_200")),
        # RSI
        "rsi": _safe_float(last.get("rsi"), 50.0),
        # Bollinger Bands
        "bb_lower": bb_lower,
        "bb_upper": bb_upper,
        "bb_mid": float(last["bb_mid"]),
        "bb_position_pct": round(bb_position_pct, 1),
        # ATR
        "atr": _safe_float(last.get("atr")),
        # NEW: MACD
        "macd": _safe_float(last.get("macd")),
        "macd_signal": _safe_float(last.get("macd_signal")),
        "macd_hist": _safe_float(last.get("macd_hist")),
        "macd_hist_trend": macd_hist_trend,
        # NEW: Stochastic RSI
        "stoch_rsi_k": _safe_float(last.get("stoch_rsi_k"), 50.0),
        "stoch_rsi_d": _safe_float(last.get("stoch_rsi_d"), 50.0),
        # NEW: ADX
        "adx": _safe_float(last.get("adx")),
        "di_plus": _safe_float(last.get("di_plus")),
        "di_minus": _safe_float(last.get("di_minus")),
        # NEW: Volume context
        "vol_sma_20": vol_sma,
        "vol_ratio": round(vol_ratio, 2),
        # NEW: Trend context
        "price_change_4h": round(price_change_4h, 2),
        "price_change_12h": round(price_change_12h, 2),
        "price_change_24h": round(price_change_24h, 2),
        "rsi_trajectory": rsi_trajectory,
        "ema_cross_type": ema_cross_type,
        "ema_cross_candles_ago": ema_cross_candles_ago,
        # NEW: Support & Resistance
        "support_48h": support_level,
        "resistance_48h": resistance_level,
    }


def fetch_orderbook(symbol: str) -> dict:
    """Fetches orderbook depth from Coinbase Exchange"""
    try:
        product_id = symbol.replace("USDT", "-USDT")
        url = f"https://api.exchange.coinbase.com/products/{product_id}/book"
        params = {"level": "2"}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        bids_data = data.get("bids", [])
        asks_data = data.get("asks", [])

        # Coinbase level 2 returns bids/asks as lists of [price, size, num-orders]
        bids = [[float(item[0]), float(item[1])] for item in bids_data]
        asks = [[float(item[0]), float(item[1])] for item in asks_data]

        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        spread = best_ask - best_bid
        spread_pct = (spread / best_bid * 100) if best_bid > 0 else 0.0

        bid_vol_5 = sum(q for p, q in bids[:5])
        ask_vol_5 = sum(q for p, q in asks[:5])
        bid_ask_ratio_5 = bid_vol_5 / ask_vol_5 if ask_vol_5 > 0 else 1.0

        total_bid_vol = sum(q for p, q in bids[:20])
        total_ask_vol = sum(q for p, q in asks[:20])
        total_bid_ask_ratio = (
            total_bid_vol / total_ask_vol if total_ask_vol > 0 else 1.0
        )

        logger.info(f"[OK] Successfully fetched Coinbase orderbook for {symbol}")
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_pct": spread_pct,
            "bid_ask_ratio_5": bid_ask_ratio_5,
            "total_bid_ask_ratio": total_bid_ask_ratio,
        }
    except Exception as e:
        logger.error(f"Error fetching Coinbase orderbook for {symbol}: {e}")
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
        "XRP": "xrp",
    }
    slug = slugs.get(coin, coin.lower())
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    result = {
        "rank": "N/A",
        "market_cap": 0.0,
        "volume_24h": 0.0,
        "price_change_24h": 0.0,
        "news": [],
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
                    for item in news_data[:3]:  # top 3 news items
                        meta = item.get("meta", {})
                        title = meta.get("title")
                        subtitle = meta.get("subtitle")
                        if title:
                            news_list.append(
                                {"title": title, "subtitle": subtitle or ""}
                            )
                    result["news"] = news_list
        logger.info(f"[OK] Successfully fetched CoinMarketCap data for {coin}")
        return result
    except Exception as e:
        logger.error(f"Error fetching CoinMarketCap data for {coin}: {e}")
        return result


def compute_technical_analysis(indicators: dict, orderbook: dict, cmc_data: dict) -> dict:
    """Pre-computes a structured technical analysis with bullish/bearish evidence lists.
    This gives the LLM clear, pre-digested context so it doesn't default to optimism."""

    bullish_evidence = []
    bearish_evidence = []
    neutral_notes = []

    close = indicators["close"]

    # --- EMA Crossover ---
    if indicators["ema_9"] > indicators["ema_21"]:
        msg = "EMA 9 is above EMA 21 (short-term bullish momentum)"
        if indicators["ema_cross_type"] == "bullish" and indicators["ema_cross_candles_ago"] <= 5:
            msg += f" — FRESH bullish cross {indicators['ema_cross_candles_ago']} candles ago"
        bullish_evidence.append(msg)
    elif indicators["ema_9"] < indicators["ema_21"]:
        msg = "EMA 9 is below EMA 21 (short-term bearish momentum)"
        if indicators["ema_cross_type"] == "bearish" and indicators["ema_cross_candles_ago"] <= 5:
            msg += f" — FRESH bearish cross {indicators['ema_cross_candles_ago']} candles ago"
        bearish_evidence.append(msg)

    # --- Price vs SMA 200 (major trend) ---
    sma_200 = indicators["sma_200"]
    if sma_200 > 0:
        pct_from_sma = ((close - sma_200) / sma_200) * 100
        if close > sma_200:
            bullish_evidence.append(f"Price is {pct_from_sma:+.1f}% above SMA 200 (long-term uptrend)")
        else:
            bearish_evidence.append(f"Price is {pct_from_sma:+.1f}% below SMA 200 (long-term downtrend)")

    # --- RSI ---
    rsi = indicators["rsi"]
    rsi_traj = indicators["rsi_trajectory"]
    if rsi > 70:
        bearish_evidence.append(f"RSI at {rsi:.1f} — OVERBOUGHT territory (trajectory: {rsi_traj})")
    elif rsi > 60:
        note = f"RSI at {rsi:.1f} — approaching overbought (trajectory: {rsi_traj})"
        if rsi_traj == "rising":
            bearish_evidence.append(note)
        else:
            neutral_notes.append(note)
    elif rsi < 30:
        bullish_evidence.append(f"RSI at {rsi:.1f} — OVERSOLD territory (trajectory: {rsi_traj})")
    elif rsi < 40:
        note = f"RSI at {rsi:.1f} — approaching oversold (trajectory: {rsi_traj})"
        if rsi_traj == "falling":
            bullish_evidence.append(note)
        else:
            neutral_notes.append(note)
    else:
        neutral_notes.append(f"RSI at {rsi:.1f} — neutral zone (trajectory: {rsi_traj})")

    # --- MACD ---
    macd_hist = indicators["macd_hist"]
    macd_trend = indicators["macd_hist_trend"]
    if macd_hist > 0:
        msg = f"MACD histogram positive ({macd_hist:.4f})"
        if macd_trend == "rising":
            msg += " and RISING — strengthening bullish momentum"
            bullish_evidence.append(msg)
        elif macd_trend == "falling":
            msg += " but FALLING — bullish momentum weakening"
            neutral_notes.append(msg)
        else:
            bullish_evidence.append(msg)
    elif macd_hist < 0:
        msg = f"MACD histogram negative ({macd_hist:.4f})"
        if macd_trend == "falling":
            msg += " and FALLING — strengthening bearish momentum"
            bearish_evidence.append(msg)
        elif macd_trend == "rising":
            msg += " but RISING — bearish momentum weakening"
            neutral_notes.append(msg)
        else:
            bearish_evidence.append(msg)

    # --- Stochastic RSI ---
    stoch_k = indicators["stoch_rsi_k"]
    stoch_d = indicators["stoch_rsi_d"]
    if stoch_k > 80:
        bearish_evidence.append(f"Stochastic RSI K={stoch_k:.1f} — OVERBOUGHT, potential reversal down")
    elif stoch_k < 20:
        bullish_evidence.append(f"Stochastic RSI K={stoch_k:.1f} — OVERSOLD, potential reversal up")
    if stoch_k > stoch_d and stoch_k < 50:
        bullish_evidence.append(f"StochRSI K ({stoch_k:.1f}) crossed above D ({stoch_d:.1f}) in lower zone — bullish crossover")
    elif stoch_k < stoch_d and stoch_k > 50:
        bearish_evidence.append(f"StochRSI K ({stoch_k:.1f}) crossed below D ({stoch_d:.1f}) in upper zone — bearish crossover")

    # --- ADX (trend strength) ---
    adx = indicators["adx"]
    di_plus = indicators["di_plus"]
    di_minus = indicators["di_minus"]
    if adx > 25:
        if di_plus > di_minus:
            bullish_evidence.append(f"ADX={adx:.1f} (strong trend), +DI ({di_plus:.1f}) > -DI ({di_minus:.1f}) — confirmed bullish trend")
        else:
            bearish_evidence.append(f"ADX={adx:.1f} (strong trend), -DI ({di_minus:.1f}) > +DI ({di_plus:.1f}) — confirmed bearish trend")
    elif adx < 20:
        neutral_notes.append(f"ADX={adx:.1f} — weak/no trend (ranging market, signals less reliable)")

    # --- Bollinger Band position ---
    bb_pos = indicators["bb_position_pct"]
    if bb_pos > 90:
        bearish_evidence.append(f"Price at {bb_pos:.0f}% of Bollinger Band range — near upper band, overbought")
    elif bb_pos < 10:
        bullish_evidence.append(f"Price at {bb_pos:.0f}% of Bollinger Band range — near lower band, oversold")

    # --- Volume context ---
    vol_ratio = indicators["vol_ratio"]
    if vol_ratio > 1.5:
        neutral_notes.append(f"Volume is {vol_ratio:.1f}x above 20-period average — HIGH volume, significant move")
    elif vol_ratio < 0.5:
        neutral_notes.append(f"Volume is {vol_ratio:.1f}x below 20-period average — LOW volume, weak conviction")

    # --- Multi-timeframe price action ---
    pc_4h = indicators["price_change_4h"]
    pc_12h = indicators["price_change_12h"]
    pc_24h = indicators["price_change_24h"]
    if pc_24h > 3:
        bullish_evidence.append(f"Price up {pc_24h:+.2f}% over 24h (strong upward momentum)")
    elif pc_24h < -3:
        bearish_evidence.append(f"Price down {pc_24h:+.2f}% over 24h (strong downward momentum)")

    # Check for short-term divergence from longer trend
    if pc_4h < -1 and pc_24h > 2:
        neutral_notes.append(f"Short-term pullback ({pc_4h:+.2f}% in 4h) within a 24h uptrend ({pc_24h:+.2f}%)")
    elif pc_4h > 1 and pc_24h < -2:
        neutral_notes.append(f"Short-term bounce ({pc_4h:+.2f}% in 4h) within a 24h downtrend ({pc_24h:+.2f}%)")

    # --- Orderbook ---
    bid_ask_5 = orderbook["bid_ask_ratio_5"]
    bid_ask_20 = orderbook["total_bid_ask_ratio"]
    if bid_ask_5 > 1.5:
        bullish_evidence.append(f"Orderbook bid/ask ratio (top 5) = {bid_ask_5:.2f} — strong buy pressure")
    elif bid_ask_5 < 0.7:
        bearish_evidence.append(f"Orderbook bid/ask ratio (top 5) = {bid_ask_5:.2f} — strong sell pressure")
    if bid_ask_20 > 1.3:
        bullish_evidence.append(f"Orderbook bid/ask ratio (top 20) = {bid_ask_20:.2f} — deep buy support")
    elif bid_ask_20 < 0.75:
        bearish_evidence.append(f"Orderbook bid/ask ratio (top 20) = {bid_ask_20:.2f} — deep sell pressure")

    # --- CMC 24h change ---
    cmc_change = float(cmc_data.get("price_change_24h", 0.0) or 0.0)
    if cmc_change > 5:
        bullish_evidence.append(f"CMC 24h change: {cmc_change:+.2f}% — significant positive momentum")
    elif cmc_change < -5:
        bearish_evidence.append(f"CMC 24h change: {cmc_change:+.2f}% — significant negative momentum")

    # --- Compute overall lean ---
    bull_count = len(bullish_evidence)
    bear_count = len(bearish_evidence)
    total = bull_count + bear_count

    if total == 0:
        lean = "NEUTRAL"
        confidence_hint = "low"
    elif bull_count >= bear_count + 3:
        lean = "BULLISH"
        confidence_hint = "high" if bull_count >= bear_count + 5 else "moderate"
    elif bear_count >= bull_count + 3:
        lean = "BEARISH"
        confidence_hint = "high" if bear_count >= bull_count + 5 else "moderate"
    elif bull_count > bear_count:
        lean = "SLIGHTLY BULLISH"
        confidence_hint = "low"
    elif bear_count > bull_count:
        lean = "SLIGHTLY BEARISH"
        confidence_hint = "low"
    else:
        lean = "NEUTRAL"
        confidence_hint = "low"

    # --- Compute suggested levels ---
    atr = indicators["atr"]
    support = indicators["support_48h"]
    resistance = indicators["resistance_48h"]

    if lean in ("BULLISH", "SLIGHTLY BULLISH"):
        entry_low = close - (0.5 * atr) if atr > 0 else close * 0.995
        entry_high = close + (0.3 * atr) if atr > 0 else close * 1.002
        target_1 = close + (1.5 * atr) if atr > 0 else resistance
        target_2 = close + (2.5 * atr) if atr > 0 else resistance * 1.02
        stop_loss = close - (1.5 * atr) if atr > 0 else support
    elif lean in ("BEARISH", "SLIGHTLY BEARISH"):
        entry_low = close - (0.3 * atr) if atr > 0 else close * 0.998
        entry_high = close + (0.5 * atr) if atr > 0 else close * 1.005
        target_1 = close - (1.5 * atr) if atr > 0 else support
        target_2 = close - (2.5 * atr) if atr > 0 else support * 0.98
        stop_loss = close + (1.5 * atr) if atr > 0 else resistance
    else:
        entry_low = support
        entry_high = resistance
        target_1 = close
        target_2 = close
        stop_loss = close - (1.5 * atr) if atr > 0 else support

    return {
        "lean": lean,
        "confidence_hint": confidence_hint,
        "bullish_evidence": bullish_evidence,
        "bearish_evidence": bearish_evidence,
        "neutral_notes": neutral_notes,
        "bull_count": bull_count,
        "bear_count": bear_count,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "target_1": target_1,
        "target_2": target_2,
        "stop_loss": stop_loss,
        "support": support,
        "resistance": resistance,
    }


def get_crypto_signal(
    client: genai.Client, coin: str, indicators: dict, orderbook: dict, cmc_data: dict
) -> str:
    """Calls Gemini LLM to analyze coin metrics and generate an HTML-formatted Persian Telegram signal.
    Uses pre-computed technical analysis to prevent LLM buy-bias."""

    # === Step 1: Compute technical pre-analysis ===
    analysis = compute_technical_analysis(indicators, orderbook, cmc_data)
    logger.info(
        f"[{coin}] Technical lean: {analysis['lean']} "
        f"(bull={analysis['bull_count']}, bear={analysis['bear_count']}, confidence={analysis['confidence_hint']})"
    )

    # Format evidence lists
    bull_str = "\n".join(f"  ✅ {e}" for e in analysis["bullish_evidence"]) or "  (none)"
    bear_str = "\n".join(f"  ❌ {e}" for e in analysis["bearish_evidence"]) or "  (none)"
    neutral_str = "\n".join(f"  ⚪ {n}" for n in analysis["neutral_notes"]) or "  (none)"

    # Format news
    news_str = ""
    if cmc_data.get("news"):
        for i, item in enumerate(cmc_data["news"], 1):
            news_str += f"{i}. {item['title']}\n"
            if item.get("subtitle"):
                news_str += f"   - {item['subtitle']}\n"
    else:
        news_str = "No recent news found.\n"

    # === Step 2: Build system prompt with strong anti-bias instructions ===
    system_instruction = """
You are a senior quantitative cryptocurrency analyst. You generate OBJECTIVE, UNBIASED market signals.

CRITICAL ANTI-BIAS RULES — FOLLOW THESE EXACTLY:
1. You are given a pre-computed TECHNICAL LEAN with bullish and bearish evidence. This lean is your PRIMARY guide for the signal direction. You MUST NOT override it with a more bullish interpretation unless the news fundamentally contradicts it.
2. If the technical lean is BEARISH or SLIGHTLY BEARISH, you MUST output a SELL (فروش) or HOLD (نگهداری) signal. You MUST NOT output BUY in this case.
3. If the technical lean is NEUTRAL, you MUST output HOLD (نگهداری). Do NOT default to BUY.
4. If the technical lean is SLIGHTLY BULLISH, carefully consider whether to suggest cautious BUY or HOLD — do NOT give a confident buy signal on weak evidence.
5. Only output a confident BUY (خرید پله‌ای) when the lean is BULLISH with moderate/high confidence.
6. Before deciding your final signal, you MUST mentally review: "Am I defaulting to BUY out of habit? Do the bearish factors outweigh the bullish?" If bearish count >= bullish count, DO NOT output BUY.

FORMATTING RULES:
1. You MUST use HTML tags for formatting. Do NOT use markdown bold/italic. Use <b>, <i>, <code>, <pre>, and <blockquote expandable>.
2. Do NOT use HTML or markdown tables. Present metrics as clean bulleted lists using <b> and <code>.
3. All prices, percentages, ranks, and targets MUST be wrapped in <code>...</code> tags.
"""

    # === Step 3: Build the enriched prompt ===
    prompt = f"""
=== PRE-COMPUTED TECHNICAL ANALYSIS FOR {coin} ===

TECHNICAL LEAN: {analysis['lean']} (confidence: {analysis['confidence_hint']})
Bullish evidence count: {analysis['bull_count']}
Bearish evidence count: {analysis['bear_count']}

BULLISH EVIDENCE:
{bull_str}

BEARISH EVIDENCE:
{bear_str}

NEUTRAL OBSERVATIONS:
{neutral_str}

COMPUTED LEVELS (ATR-based):
  Entry zone: ${analysis['entry_low']:.4f} – ${analysis['entry_high']:.4f}
  Target 1: ${analysis['target_1']:.4f}
  Target 2: ${analysis['target_2']:.4f}
  Stop Loss: ${analysis['stop_loss']:.4f}
  48h Support: ${analysis['support']:.4f}
  48h Resistance: ${analysis['resistance']:.4f}

=== YOUR TASK ===
Based on the technical lean above, generate the Telegram signal report in Persian.

SIGNAL MAPPING:
- BULLISH → خرید پله‌ای 🟢
- SLIGHTLY BULLISH → خرید محتاطانه 🟢 (with lower confidence)
- NEUTRAL → نگهداری / خنثی 🟡
- SLIGHTLY BEARISH → خروج / نگهداری 🟡
- BEARISH → فروش 🔴

If signal is Sell, targets should be BELOW current price and stop loss ABOVE.
If signal is Hold, state no entry is recommended and explain why.

--- FILL THIS TEMPLATE ---
📊 <b>سیگنال نهایی #{coin}</b>

💵 <b>قیمت فعلی:</b> <code>${indicators['close']:.4f}</code>

🎯 <b>وضعیت سیستم:</b>
• <b>تمایل تکنیکال:</b> <code>{analysis['lean']}</code>
• <b>پیشنهاد:</b> [خرید پله‌ای / خرید محتاطانه / فروش / نگهداری] [🟢/🔴/🟡]
• <b>درصد اطمینان:</b> <code>[X%]</code>

🚀 <b>محدوده ورود/اقدام پیشنهادی:</b>
• <b>محدوده قیمتی مورد نظر:</b> <code>${analysis['entry_low']:.4f} – ${analysis['entry_high']:.4f}</code>

🎯 <b>سطوح هدف و مدیریت ریسک:</b>
• <b>اهداف قیمتی (Targets):</b>
  ۱. هدف اول: <code>${analysis['target_1']:.4f}</code>
  ۲. هدف دوم: <code>${analysis['target_2']:.4f}</code>
• 🛑 <b>حد ضرر (Stop Loss):</b> <code>${analysis['stop_loss']:.4f}</code>
• <b>حمایت ۴۸ ساعته:</b> <code>${analysis['support']:.4f}</code>
• <b>مقاومت ۴۸ ساعته:</b> <code>${analysis['resistance']:.4f}</code>

<blockquote expandable>📊 <b>آمار کوین‌مارکت‌کپ و اخبار اخیر:</b>
• رتبه کوین‌مارکت‌کپ: <code>#{cmc_data.get('rank', 'N/A')}</code>
• ارزش بازار: <code>${cmc_data.get('market_cap', 0.0):,.0f} دلار</code>
• تغییرات ۲۴ ساعته: <code>{cmc_data.get('price_change_24h', 0.0):+.2f}%</code>

<b>اخبار و رویدادهای مهم:</b>
[خلاصه یا ترجمه کوتاه فارسی اخبار زیر در ۳ بند]:
{news_str}</blockquote>

<blockquote expandable>⚙️ <b>تحلیل تکنیکال (کوین‌بیس - جهانی):</b>
• <b>شاخص RSI (14):</b> <code>{indicators['rsi']:.2f}</code> (روند: {indicators['rsi_trajectory']})
• <b>میانگین SMA 200:</b> <code>{indicators['sma_200']:.4f}</code>
• <b>میانگین EMA 9 / 21:</b> <code>{indicators['ema_9']:.4f} / {indicators['ema_21']:.4f}</code> (تقاطع: {indicators['ema_cross_type']})
• <b>MACD:</b> <code>{indicators['macd']:.4f}</code> | سیگنال: <code>{indicators['macd_signal']:.4f}</code> | هیستوگرام: <code>{indicators['macd_hist']:.4f}</code> ({indicators['macd_hist_trend']})
• <b>Stochastic RSI:</b> K=<code>{indicators['stoch_rsi_k']:.1f}</code> D=<code>{indicators['stoch_rsi_d']:.1f}</code>
• <b>ADX:</b> <code>{indicators['adx']:.1f}</code> | +DI: <code>{indicators['di_plus']:.1f}</code> | -DI: <code>{indicators['di_minus']:.1f}</code>
• <b>باندهای بولینگر:</b> <code>{indicators['bb_lower']:.4f} - {indicators['bb_upper']:.4f}</code> (موقعیت: <code>{indicators['bb_position_pct']:.0f}%</code>)
• <b>شاخص ATR (14):</b> <code>{indicators['atr']:.4f}</code>
• <b>نسبت حجم:</b> <code>{indicators['vol_ratio']:.2f}x</code> نسبت به میانگین ۲۰ دوره

<b>تغییرات قیمت:</b>
• ۴ ساعت: <code>{indicators['price_change_4h']:+.2f}%</code> | ۱۲ ساعت: <code>{indicators['price_change_12h']:+.2f}%</code> | ۲۴ ساعت: <code>{indicators['price_change_24h']:+.2f}%</code>

<b>توضیح تکنیکال:</b>
[۲ الی ۳ خط تحلیل روند تکنیکال به فارسی — ابتدا نکات صعودی سپس نزولی را بررسی کنید]</blockquote>

<blockquote expandable>⚖️ <b>تحلیل دفتر سفارشات (کوین‌بیس):</b>
• <b>اسپرد قیمت (Spread):</b> <code>{orderbook['spread']:.4f} ({orderbook['spread_pct']:.4f}%)</code>
• <b>نسبت حجم (۵ لایه اول):</b> <code>{orderbook['bid_ask_ratio_5']:.2f}</code>
• <b>نسبت حجم (۲۰ لایه اول):</b> <code>{orderbook['total_bid_ask_ratio']:.2f}</code>

<b>توضیح دفتر سفارشات:</b>
[۱ الی ۲ خط تحلیل عمق بازار و خریدار/فروشنده به فارسی]</blockquote>
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
                    system_instruction=system_instruction,
                    temperature=0.1,
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
                res_text = (
                    res_text.replace("<br>", "\n")
                    .replace("<br/>", "\n")
                    .replace("<br />", "\n")
                )
                return res_text.strip()
        except Exception as e:
            logger.error(f"Error generating signal on {model} for {coin}: {e}")
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
