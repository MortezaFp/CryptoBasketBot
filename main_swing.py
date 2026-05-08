import os
import sys
import time
import logging
import json
import requests
from decimal import Decimal, ROUND_DOWN
import pandas as pd
import pandas_ta as ta
from google import genai
from pydantic import BaseModel

# Import base classes from main script
from main import WallexAPI, TelegramNotifier, load_telegram_config, WALLEX_API_BASE

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("SwingBot")

TARGET_COINS = [
    "BTC",
    "ETH",
    "TON",
    "XAUT",
    "DOGE",
    "SLVON",
    "SOL",
    "PEPE",
    "DOGS",
    "XRP",
    "NOT",
    "ADA",
    "DASH",
    "TRX",
    "ARB",
]
BASE_TRADE_USDT = Decimal("50.0")
MAX_BANK_ALLOCATION_PCT = Decimal("0.15")


def get_app_path():
    """Get the base path of the application, compatible with PyInstaller"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


class SwingWallexAPI(WallexAPI):
    def get_candle_history(self, symbol: str, start_ts: int, end_ts: int) -> list:
        try:
            url = f"{WALLEX_API_BASE}/v1/udf/history"
            params = {
                "symbol": f"{symbol}USDT",
                "resolution": "60",
                "from": start_ts,
                "to": end_ts,
            }
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("s") == "ok":
                candles = []
                for i in range(len(data["t"])):
                    candles.append(
                        {
                            "time": data["t"][i],
                            "open": data["o"][i],
                            "high": data["h"][i],
                            "low": data["l"][i],
                            "close": data["c"][i],
                            "volume": data["v"][i],
                        }
                    )
                return candles
            return []
        except Exception as e:
            logger.error(f"Error fetching candles for {symbol}: {e}")
            return []

    def get_last_filled_buy_order(self, symbol: str) -> dict:
        try:
            url = f"{WALLEX_API_BASE}/v1/account/orders"
            params = {"market": f"{symbol}USDT", "side": "BUY"}
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("success"):
                orders = data.get("result", {}).get("orders", [])
                filled = [o for o in orders if o.get("status") == "FILLED"]
                if filled:
                    filled.sort(key=lambda x: int(x.get("time", 0)), reverse=True)
                    return filled[0]
            return None
        except Exception as e:
            logger.error(f"Error fetching orders for {symbol}: {e}")
            return None


class AISignal(BaseModel):
    signal: str
    confidence_score: int
    reason: str


def check_global_veto(coin: str, local_change_pct: float) -> bool:
    try:
        binance_url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={coin}USDT"
        resp = requests.get(binance_url, timeout=5)
        # If the coin doesn't exist on Binance (like SLVON or XAUT), don't veto.
        if resp.status_code == 400:
            return False

        resp.raise_for_status()
        data = resp.json()
        global_change_pct = float(data.get("priceChangePercent", 0.0))

        # 2A. Global Crash
        if global_change_pct < -3.0:
            logger.warning(
                f"VETO 🚨: {coin} Binance 24h drop is {global_change_pct:.2f}% (< -3%)"
            )
            return True

        # 2B. Divergence (Lag)
        if (local_change_pct - global_change_pct) > 1.5:
            logger.warning(
                f"VETO 🚨: {coin} Divergence! Local ({local_change_pct:.2f}%) minus Global ({global_change_pct:.2f}%) > 1.5%"
            )
            return True

        return False
    except Exception as e:
        logger.debug(f"Global Veto check failed or skipped for {coin}: {e}")
        return False


def get_ai_signal(coin: str, indicators: dict) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY not set")
        return None

    client = genai.Client(api_key=api_key)
    prompt = f"""
    Analyze this technical data for {coin}:
    Price: {indicators['close']}
    200 SMA: {indicators['sma_200']}
    9 EMA: {indicators['ema_9']}
    21 EMA: {indicators['ema_21']}
    RSI: {indicators['rsi']}
    BB Lower: {indicators['bb_lower']}
    BB Upper: {indicators['bb_upper']}
    ATR: {indicators['atr']}

    Is this a confirmed high-probability swing trade entry, or a fake-out? 
    Respond ONLY with a valid JSON object containing: 'signal' (BUY, SELL, or HOLD), 'confidence_score' (1-100), and 'reason' (string).

    """
    logger.info(f"🧠 Sending Prompt to AI for {coin}:\n{prompt}")

    models_to_try = ["gemini-flash-latest", "gemini-2.5-flash"]
    for model_name in models_to_try:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": AISignal,
                    },
                )
                logger.info(
                    f"🤖 AI Response for {coin} using {model_name}:\n{response.text}"
                )
                return json.loads(response.text)
            except Exception as e:
                logger.error(
                    f"Gemini API error with {model_name} (Attempt {attempt + 1}/2): {e}"
                )
                time.sleep(2)

    logger.error(f"All Gemini AI requests failed for {coin}. Skipping.")
    return None


def run_swing_cycle(api=None):
    if api is None:
        api_key = os.environ.get("WALLEX_API_KEY")
        if not api_key:
            logger.error("WALLEX_API_KEY not set")
            return
        api = SwingWallexAPI(api_key)

    telegram_token, chat_id = load_telegram_config()
    notifier = (
        TelegramNotifier(telegram_token, chat_id)
        if telegram_token and chat_id
        else None
    )

    # Pre-Loop Bank Check
    balances_resp = api.get_account_balances()
    if not balances_resp.get("success"):
        logger.error("Failed to get balances")
        return

    balances = {
        k: Decimal(str(v.get("value", 0)))
        for k, v in balances_resp.get("result", {}).get("balances", {}).items()
    }
    current_bank = balances.get("USDT", Decimal("0"))
    initial_bank = current_bank

    logger.info(f"Starting Bank: {current_bank} USDT")

    # Clean up deprecated coins
    for b_coin, amt in balances.items():
        if b_coin == "USDT" or b_coin in TARGET_COINS:
            continue
        if amt > 0:
            try:
                c_price = api.get_market_price(f"{b_coin}USDT")
                b_value = amt * c_price
                if b_value >= Decimal("5.0"):
                    logger.info(
                        f"Liquidating removed coin {b_coin} (Value: ~${b_value:.2f})"
                    )
                    precision = api.get_market_precision(f"{b_coin}USDT")
                    precision_str = (
                        "0." + "0" * (precision - 1) + "1" if precision > 0 else "1"
                    )
                    qty = amt.quantize(Decimal(precision_str), rounding=ROUND_DOWN)
                    api.create_order(f"{b_coin}USDT", "SELL", qty, type="MARKET")
                    current_bank += (
                        b_value  # Roughly update bank based on estimated value
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to check/liquidate deprecated coin {b_coin}: {e}"
                )

    end_ts = int(time.time())
    start_ts = end_ts - (250 * 60 * 60)

    summary_messages = []
    coin_reports = []

    # Fetch Wallex markets to get local 24h change percent for the Veto check
    local_changes = {}
    try:
        w_markets_resp = requests.get(
            "https://api.wallex.ir/hector/web/v1/markets", timeout=10
        ).json()
        w_markets = w_markets_resp.get("result", {}).get("markets", [])
        for m in w_markets:
            if m.get("quote_asset") == "USDT":
                local_changes[m.get("base_asset")] = float(
                    m.get("change_24h", 0.0) or 0.0
                )
    except Exception as e:
        logger.error(f"Failed to fetch Wallex markets for local changes: {e}")

    for coin in TARGET_COINS:
        logger.info(f"Processing {coin}...")
        coin_balance = balances.get(coin, Decimal("0"))

        try:
            current_price = api.get_market_price(f"{coin}USDT")
        except Exception:
            logger.warning(f"Could not fetch market price for {coin}. Skipping.")
            continue

        coin_value_usdt = coin_balance * current_price

        candles = api.get_candle_history(coin, start_ts, end_ts)
        if len(candles) < 200:
            logger.warning(f"Not enough candles for {coin}. Skipping.")
            continue

        df = pd.DataFrame(candles)
        df["close"] = pd.to_numeric(df["close"])
        df["high"] = pd.to_numeric(df["high"])
        df["low"] = pd.to_numeric(df["low"])

        df["ema_9"] = ta.ema(df["close"], length=9)
        df["ema_21"] = ta.ema(df["close"], length=21)
        df["sma_200"] = ta.sma(df["close"], length=200)
        df["rsi"] = ta.rsi(df["close"], length=14)

        bb = ta.bbands(df["close"], length=20)
        bb_lower_col = next(
            (col for col in bb.columns if col.startswith("BBL_20_")), None
        )
        bb_upper_col = next(
            (col for col in bb.columns if col.startswith("BBU_20_")), None
        )
        if not bb_lower_col or not bb_upper_col:
            logger.warning(f"Bollinger Band columns missing for {coin}. Skipping.")
            continue
        df["bb_lower"] = bb[bb_lower_col]
        df["bb_upper"] = bb[bb_upper_col]

        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

        last_row = df.iloc[-1]
        if pd.isna(last_row["ema_9"]) or pd.isna(last_row["sma_200"]):
            logger.warning(f"Indicators not fully formed for {coin}. Skipping.")
            continue

        indicators = {
            "close": float(last_row["close"]),
            "sma_200": float(last_row["sma_200"]),
            "ema_9": float(last_row["ema_9"]),
            "ema_21": float(last_row["ema_21"]),
            "rsi": float(last_row["rsi"]),
            "bb_lower": float(last_row["bb_lower"]),
            "bb_upper": float(last_row["bb_upper"]),
            "atr": float(last_row["atr"]),
        }

        local_change_pct = local_changes.get(coin, 0.0)
        is_vetoed = check_global_veto(coin, local_change_pct)

        state = "IN" if coin_value_usdt >= Decimal("10.0") else "OUT"

        if state == "OUT":
            if is_vetoed:
                reason = "Global Veto Triggered (Binance Crash/Lag)"
            elif last_row["ema_9"] <= last_row["ema_21"]:
                reason = f"EMA9 ({last_row['ema_9']:.2f}) &lt;= EMA21 ({last_row['ema_21']:.2f})"
            elif last_row["rsi"] <= 50:
                reason = f"RSI ({last_row['rsi']:.2f}) &lt;= 50"
            elif last_row["close"] <= last_row["sma_200"]:
                reason = f"Price ({last_row['close']:.2f}) &lt;= SMA200 ({last_row['sma_200']:.2f})"
            else:
                reason = None

            if reason:
                coin_reports.append(
                    f"➖ <b>{coin}</b> @ ${current_price:.10f}: Skipped | {reason}"
                )
            else:
                logger.info(
                    f"Math conditions met for {coin}. Sleeping for 5 seconds before AI call to avoid rate limits."
                )
                time.sleep(5)
                ai_resp = get_ai_signal(coin, indicators)

                if ai_resp:
                    ai_sig = ai_resp.get("signal", "NONE")
                    ai_conf = ai_resp.get("confidence_score", 0)
                    ai_reason = (
                        str(ai_resp.get("reason", "No reason provided"))
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                    )
                    coin_reports.append(
                        f"🤖 <b>{coin}</b> @ ${current_price:.10f}: AI {ai_sig} ({ai_conf}%) | {ai_reason}"
                    )
                else:
                    coin_reports.append(
                        f"⚠️ <b>{coin}</b> @ ${current_price:.10f}: Skipped | AI Request Failed"
                    )

                if ai_resp and ai_resp.get("signal") == "BUY":
                    conf = ai_resp.get("confidence_score", 0)
                    intended_size = Decimal("0")
                    if 80 <= conf <= 89:
                        intended_size = BASE_TRADE_USDT
                    elif conf >= 90:
                        intended_size = BASE_TRADE_USDT * Decimal("1.5")

                    if intended_size > 0:
                        actual_trade_size = min(
                            intended_size,
                            current_bank * MAX_BANK_ALLOCATION_PCT,
                            current_bank,
                        )
                        if actual_trade_size >= Decimal("10.0"):
                            logger.info(
                                f"Executing BUY for {coin}. Size: {actual_trade_size}"
                            )
                            amount_to_buy = actual_trade_size / current_price

                            try:
                                precision = api.get_market_precision(f"{coin}USDT")
                                precision_str = (
                                    "0." + "0" * (precision - 1) + "1"
                                    if precision > 0
                                    else "1"
                                )
                                qty = amount_to_buy.quantize(
                                    Decimal(precision_str), rounding=ROUND_DOWN
                                )

                                api.create_order(
                                    f"{coin}USDT", "BUY", qty, type="MARKET"
                                )
                                current_bank -= actual_trade_size
                                summary_messages.append(
                                    f"✅ BUY {coin}: ${actual_trade_size:.2f} @ {current_price:.10f} (AI Conf: {conf}) - {ai_resp.get('reason')}"
                                )
                            except Exception as e:
                                logger.error(f"Failed to buy {coin}: {e}")

        elif state == "IN":
            last_order = api.get_last_filled_buy_order(coin)
            if not last_order:
                logger.warning(
                    f"Could not find entry price for {coin}. Skipping IN evaluation."
                )
                continue

            entry_price = Decimal(str(last_order.get("price", "0")))
            if entry_price == 0:
                logger.warning(f"Entry price is 0 for {coin}. Skipping.")
                continue

            current_atr = Decimal(str(last_row["atr"]))
            dynamic_stop_loss = entry_price - (Decimal("1.5") * current_atr)
            take_profit = entry_price * Decimal("1.08")

            sell_condition = (
                is_vetoed
                or (current_price <= dynamic_stop_loss)
                or (current_price >= take_profit)
                or (last_row["ema_9"] < last_row["ema_21"])
            )

            if sell_condition:
                reason_str = (
                    "Global Veto (Emergency!)" if is_vetoed else "Condition met"
                )
                logger.info(
                    f"Executing SELL for {coin}. {reason_str}. Price: {current_price}, Stop: {dynamic_stop_loss}, TP: {take_profit}"
                )
                try:
                    precision = api.get_market_precision(f"{coin}USDT")
                    precision_str = (
                        "0." + "0" * (precision - 1) + "1" if precision > 0 else "1"
                    )
                    qty = coin_balance.quantize(
                        Decimal(precision_str), rounding=ROUND_DOWN
                    )

                    api.create_order(f"{coin}USDT", "SELL", qty, type="MARKET")
                    sell_type = "EMERGENCY VETO SELL" if is_vetoed else "SELL"
                    summary_messages.append(
                        f"❌ {sell_type} {coin}: {qty} @ {current_price:.10f} (Entry: {entry_price:.10f})"
                    )
                    coin_reports.append(
                        f"❌ <b>{coin}</b> @ ${current_price:.10f}: SOLD ({sell_type}) | Entry: ${entry_price:.10f}"
                    )
                except Exception as e:
                    logger.error(f"Failed to sell {coin}: {e}")
            else:
                coin_reports.append(
                    f"💼 <b>{coin}</b> @ ${current_price:.10f}: Holding | No exit conditions met"
                )

        logger.info(f"Finished processing {coin}.")

    if notifier:
        msg = f"🔄 <b>Swing Bot Cycle Complete</b>\n"
        msg += f"🏦 Starting Bank: ${initial_bank:.2f}\n"
        msg += f"🏦 Remaining Bank: ${current_bank:.2f}\n\n"

        if summary_messages:
            msg += "<b>Executions:</b>\n" + "\n".join(summary_messages) + "\n\n"

        msg += "<b>Coin Reports:</b>\n" + "\n".join(coin_reports)

        # Split message if it's too long for Telegram (max 4096 chars)
        if len(msg) > 4000:
            parts = [msg[i : i + 4000] for i in range(0, len(msg), 4000)]
            for part in parts:
                notifier.send_message(part)
        else:
            notifier.send_message(msg)

        notifier.flush()


if __name__ == "__main__":
    run_swing_cycle()
