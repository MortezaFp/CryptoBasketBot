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
BASE_TRADE_USDT = Decimal("100.0")
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
        if resp.status_code == 400:
            return False

        resp.raise_for_status()
        data = resp.json()
        global_change_pct = float(data.get("priceChangePercent", 0.0))

        if global_change_pct < -3.0:
            logger.warning(
                f"VETO 🚨: {coin} Binance 24h drop is {global_change_pct:.2f}% (< -3%)"
            )
            return True

        if (local_change_pct - global_change_pct) > 1.5:
            logger.warning(
                f"VETO 🚨: {coin} Divergence! Local ({local_change_pct:.2f}%) minus Global ({global_change_pct:.2f}%) > 1.5%"
            )
            return True

        return False
    except Exception as e:
        logger.debug(f"Global Veto check failed or skipped for {coin}: {e}")
        return False


def get_ai_signal(coin: str, indicators: dict, market_regime: str) -> dict:
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

    Current Macro Market Regime: {market_regime}

    CRITICAL INSTRUCTION - THE PULLBACK STRATEGY: 
    Your goal is to buy "The Dip" in an established uptrend. The price is already above the 200 SMA. 
    Look for coins where the RSI has cooled off (between 35 and 55) or the price has pulled back to touch the Lower Bollinger Band or the 21 EMA. 
    DO NOT buy if the RSI is above 60. We want to enter on temporary weakness, not chase green candles.

    Is this a high-probability "buy the dip" setup with good support?
    Respond ONLY with a valid JSON object containing: 'signal' (BUY, SELL, or HOLD), 'confidence_score' (1-100), and 'reason' (string).
    """
    logger.info(f"🧠 Sending Pullback-Aware Prompt to AI for {coin}:\n{prompt}")

    models_to_try = [
        "gemini-3.1-flash-lite",
        "gemini-flash-latest",
        "gemini-2.5-flash",
        "gemma-4-31b-it",
    ]
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


def get_cio_summary(
    pl_pct: float,
    market_hodl_pct: float,
    alpha_pct: float,
    regime: str,
    resolved_ghosts: str,
    coin_reports: list,
) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "CIO Summary unavailable (No API Key)."

    client = genai.Client(api_key=api_key)
    prompt = f"""
    You are a blunt, senior Quant Fund Manager. Review this 15-minute cycle report.
    Bot P/L: {pl_pct:.2f}%
    Market HODL Benchmark: {market_hodl_pct:.2f}%
    Bot Alpha: {alpha_pct:.2f}%
    Regime: {regime}
    Resolved Ghost Trades (AI Audit): {resolved_ghosts}
    Coin Statuses: {json.dumps(coin_reports)}

    Provide a 3-sentence summary of the market vibe and the bot's current performance. Do not use formatting like bold or asterisks.
    """
    models_to_try = [
        "gemini-3.1-flash-lite",
        "gemini-flash-latest",
        "gemini-2.5-flash",
        "gemma-4-31b-it",
    ]
    for model_name in models_to_try:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                if response and response.text:
                    return response.text.strip().replace("*", "")
            except Exception:
                time.sleep(2)
    return "CIO is currently unreachable due to rate limits."


def fmt_price(p) -> str:
    return f"{float(p):.10f}".rstrip("0").rstrip(".")


def run_swing_cycle(api=None, allow_speculative=False, cycle_name=None):
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

    if not hasattr(api, "state_file"):
        api.state_file = os.path.join(get_app_path(), f"swing_state_live.json")

    bot_state = {
        "initial_benchmark_prices": {},
        "ghost_trades": [],
        "initial_bot_bank": 0.0,
    }
    if os.path.exists(api.state_file):
        try:
            with open(api.state_file, "r") as f:
                data = json.load(f)
                bot_state["initial_benchmark_prices"] = data.get(
                    "initial_benchmark_prices", {}
                )
                bot_state["ghost_trades"] = data.get("ghost_trades", [])
                bot_state["initial_bot_bank"] = data.get("initial_bot_bank", 0.0)
        except Exception:
            pass

    def save_bot_state():
        if hasattr(api, "save_state") and getattr(api, "_state_patched", False):
            api.save_state()
        else:
            try:
                data = {}
                if os.path.exists(api.state_file):
                    with open(api.state_file, "r") as f:
                        data = json.load(f)
                data["initial_benchmark_prices"] = bot_state["initial_benchmark_prices"]
                data["ghost_trades"] = bot_state["ghost_trades"]
                data["initial_bot_bank"] = bot_state["initial_bot_bank"]
                with open(api.state_file, "w") as f:
                    json.dump(data, f, indent=4)
            except Exception as e:
                logger.error(f"Failed to save bot state: {e}")

    if hasattr(api, "save_state") and not getattr(api, "_state_patched", False):
        original_save = api.save_state

        def patched_save_state():
            original_save()
            try:
                if os.path.exists(api.state_file):
                    with open(api.state_file, "r") as f:
                        data = json.load(f)
                    data["initial_benchmark_prices"] = bot_state[
                        "initial_benchmark_prices"
                    ]
                    data["ghost_trades"] = bot_state["ghost_trades"]
                    data["initial_bot_bank"] = bot_state["initial_bot_bank"]
                    with open(api.state_file, "w") as f:
                        json.dump(data, f, indent=4)
            except Exception:
                pass

        api.save_state = patched_save_state
        api._state_patched = True

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

    local_changes = {}
    current_prices = {}
    try:
        w_markets_resp = requests.get(
            "https://api.wallex.ir/hector/web/v1/markets", timeout=10
        ).json()
        w_markets = w_markets_resp.get("result", {}).get("markets", [])
        for m in w_markets:
            if m.get("quote_asset") == "USDT":
                base_asset = m.get("base_asset")
                local_changes[base_asset] = float(m.get("change_24h", 0.0) or 0.0)
                if base_asset in TARGET_COINS:
                    current_prices[base_asset] = float(
                        str(m.get("price", "0")).replace(",", "")
                    )
    except Exception as e:
        logger.error(f"Failed to fetch Wallex markets: {e}")

    if not bot_state["initial_benchmark_prices"] and current_prices:
        for coin in TARGET_COINS:
            if coin in current_prices:
                bot_state["initial_benchmark_prices"][coin] = current_prices[coin]
        initial_val = float(current_bank)
        for coin in TARGET_COINS:
            initial_val += float(balances.get(coin, Decimal("0"))) * current_prices.get(
                coin, 0
            )
        bot_state["initial_bot_bank"] = initial_val if initial_val > 0 else 1000.0

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
                    current_bank += b_value
            except Exception as e:
                logger.warning(
                    f"Failed to check/liquidate deprecated coin {b_coin}: {e}"
                )

    end_ts = int(time.time())
    start_ts = end_ts - (250 * 60 * 60)

    base_bank = bot_state.get("initial_bot_bank", 1000.0)
    market_hodl_pct = 0.0
    if bot_state["initial_benchmark_prices"]:
        num_coins = len(bot_state["initial_benchmark_prices"])
        if num_coins > 0:
            allocation_per_coin = base_bank / num_coins
            current_basket_value = 0.0
            for coin, initial_price in bot_state["initial_benchmark_prices"].items():
                c_price = current_prices.get(coin, initial_price)
                if initial_price > 0:
                    amount_bought = allocation_per_coin / initial_price
                    current_basket_value += amount_bought * c_price
            market_hodl_pct = ((current_basket_value - base_bank) / base_bank) * 100.0

    total_bot_value = float(current_bank)
    for coin in TARGET_COINS:
        bal = float(balances.get(coin, Decimal("0")))
        if bal > 0 and coin in current_prices:
            total_bot_value += bal * current_prices[coin]

    bot_pl_pct = ((total_bot_value - base_bank) / base_bank) * 100.0
    bot_alpha = bot_pl_pct - market_hodl_pct

    resolved_ghosts = []
    kept_ghosts = []
    current_time = int(time.time())

    for ghost in bot_state["ghost_trades"]:
        coin = ghost["coin"]
        entry_time = ghost["timestamp"]
        entry_price = ghost["entry_price"]

        if current_time - entry_time > 12 * 3600:
            c_price = current_prices.get(coin, entry_price)
            if c_price < entry_price:
                resolved_ghosts.append(
                    f"{coin}: Saved Capital (Dropped from {fmt_price(entry_price)} to {fmt_price(c_price)})"
                )
            else:
                resolved_ghosts.append(
                    f"{coin}: Missed Gains (Rose from {fmt_price(entry_price)} to {fmt_price(c_price)})"
                )
        else:
            kept_ghosts.append(ghost)

    bot_state["ghost_trades"] = kept_ghosts
    ghost_summary_text = (
        " | ".join(resolved_ghosts)
        if resolved_ghosts
        else "No resolved ghost trades this cycle."
    )

    # =====================================================================
    # PHASE 1: MACRO SCAN
    # =====================================================================
    logger.info("--- STARTING PHASE 1: MACRO SCAN ---")
    coin_data_map = {}
    coins_above_sma = 0
    total_rsi = 0.0
    valid_coins = 0

    for coin in TARGET_COINS:
        logger.info(f"Scanning {coin}...")
        coin_balance = balances.get(coin, Decimal("0"))

        try:
            current_price = api.get_market_price(f"{coin}USDT")
        except Exception:
            logger.warning(f"Could not fetch market price for {coin}. Skipping scan.")
            continue

        coin_value_usdt = coin_balance * current_price
        coin_start_ts = start_ts
        if coin_value_usdt >= Decimal("10.0"):
            last_order = api.get_last_filled_buy_order(coin)
            if last_order:
                order_time = int(last_order.get("time", 0))
                if order_time > 2000000000:
                    order_time = order_time // 1000
                if order_time > 0 and order_time < coin_start_ts:
                    coin_start_ts = order_time

        candles = api.get_candle_history(coin, coin_start_ts, end_ts)
        if len(candles) < 200:
            logger.warning(f"Not enough candles for {coin}. Skipping scan.")
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
            logger.warning(f"Bollinger Band columns missing for {coin}. Skipping scan.")
            continue

        df["bb_lower"] = bb[bb_lower_col]
        df["bb_upper"] = bb[bb_upper_col]
        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

        last_row = df.iloc[-1]
        if pd.isna(last_row["ema_9"]) or pd.isna(last_row["sma_200"]):
            logger.warning(f"Indicators not fully formed for {coin}. Skipping scan.")
            continue

        if float(current_price) > float(last_row["sma_200"]):
            coins_above_sma += 1
        total_rsi += float(last_row["rsi"])
        valid_coins += 1

        local_change_pct = local_changes.get(coin, 0.0)
        is_vetoed = check_global_veto(coin, local_change_pct)
        state = "IN" if coin_value_usdt >= Decimal("10.0") else "OUT"

        coin_data_map[coin] = {
            "current_price": current_price,
            "coin_value_usdt": coin_value_usdt,
            "coin_balance": coin_balance,
            "df": df,
            "last_row": last_row,
            "is_vetoed": is_vetoed,
            "state": state,
            "indicators": {
                "close": float(last_row["close"]),
                "sma_200": float(last_row["sma_200"]),
                "ema_9": float(last_row["ema_9"]),
                "ema_21": float(last_row["ema_21"]),
                "rsi": float(last_row["rsi"]),
                "bb_lower": float(last_row["bb_lower"]),
                "bb_upper": float(last_row["bb_upper"]),
                "atr": float(last_row["atr"]),
            },
        }

    if valid_coins > 0:
        trend_health = (coins_above_sma / valid_coins) * 100
        avg_rsi = total_rsi / valid_coins
    else:
        trend_health = 0
        avg_rsi = 50

    if avg_rsi > 65:
        market_regime = "Overbought / Chop"
    elif trend_health >= 60:
        market_regime = "Bullish Trend"
    elif trend_health <= 30:
        market_regime = "Bearish Bleed"
    else:
        market_regime = "Neutral Sideways"

    logger.info(
        f"Phase 1 Complete. Detected Regime: {market_regime} (Trend Health: {trend_health:.1f}%, Avg RSI: {avg_rsi:.1f})"
    )

    # =====================================================================
    # PHASE 2: REGIME-AWARE EXECUTION (PULLBACK STRATEGY)
    # =====================================================================
    logger.info("--- STARTING PHASE 2: REGIME-AWARE EXECUTION ---")
    summary_messages = []
    coin_reports = []

    for coin in TARGET_COINS:
        if coin not in coin_data_map:
            continue

        data = coin_data_map[coin]
        current_price = data["current_price"]
        coin_balance = data["coin_balance"]
        last_row = data["last_row"]
        indicators = data["indicators"]
        is_vetoed = data["is_vetoed"]
        state = data["state"]
        df = data["df"]

        if state == "OUT":
            if is_vetoed:
                reason = "Global Veto Triggered (Binance Crash/Lag)"
            elif last_row["close"] <= last_row["sma_200"]:
                reason = f"Price ({last_row['close']:.2f}) &lt;= SMA200 ({last_row['sma_200']:.2f}) [Not in Macro Uptrend]"
            elif last_row["rsi"] >= 65:
                reason = f"RSI ({last_row['rsi']:.2f}) &gt;= 65 [Too Overbought to buy the dip]"
            else:
                reason = None

            if reason:
                coin_reports.append(
                    f"➖ <b>{coin}</b> @ ${fmt_price(current_price)}: Skipped | {reason}"
                )
            else:
                logger.info(
                    f"Math conditions met for {coin}. Calling AI with Regime: {market_regime}"
                )
                time.sleep(5)

                ai_resp = get_ai_signal(coin, indicators, market_regime)

                if ai_resp:
                    ai_sig = ai_resp.get("signal", "NONE")
                    ai_conf = ai_resp.get("confidence_score", 0)
                    ai_reason = (
                        str(ai_resp.get("reason", "No reason provided"))
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                    )
                    coin_reports.append(
                        f"🤖 <b>{coin}</b> @ ${fmt_price(current_price)}: AI {ai_sig} ({ai_conf}%) | {ai_reason}"
                    )
                else:
                    coin_reports.append(
                        f"⚠️ <b>{coin}</b> @ ${fmt_price(current_price)}: Skipped | AI Request Failed"
                    )

                if ai_resp and ai_resp.get("signal") == "BUY":
                    conf = ai_resp.get("confidence_score", 0)
                    intended_size = Decimal("0")

                    if conf >= 90:
                        intended_size = BASE_TRADE_USDT * Decimal("1.5")
                    elif conf >= 80:
                        intended_size = BASE_TRADE_USDT
                    elif conf >= 70 and allow_speculative:
                        intended_size = BASE_TRADE_USDT * Decimal("0.5")
                    else:
                        intended_size = Decimal("0")

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
                                    f"{coin}USDT",
                                    "BUY",
                                    qty,
                                    type="MARKET",
                                    client_id=f"conf_{conf}",
                                )
                                current_bank -= actual_trade_size
                                summary_messages.append(
                                    f"✅ BUY {coin}: ${actual_trade_size:.2f} @ {fmt_price(current_price)} (AI Conf: {conf}) - {ai_resp.get('reason')}"
                                )
                            except Exception as e:
                                logger.error(f"Failed to buy {coin}: {e}")

                elif ai_resp and ai_resp.get("signal") != "BUY":
                    bot_state["ghost_trades"].append(
                        {
                            "coin": coin,
                            "entry_price": float(current_price),
                            "timestamp": current_time,
                        }
                    )

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

            client_id = last_order.get("clientOrderId", "")
            entry_conf = (
                client_id.split("_")[1] if client_id and "conf_" in client_id else "??"
            )
            current_atr = Decimal(str(last_row["atr"]))

            dynamic_stop_loss = entry_price - (Decimal("3") * current_atr)

            order_time = int(last_order.get("time", 0))
            if order_time > 2000000000:
                order_time = order_time // 1000

            df_since_buy = df[df["time"] >= order_time]
            if not df_since_buy.empty:
                highest_price = Decimal(str(df_since_buy["high"].max()))
            else:
                highest_price = current_price
            highest_price = max(highest_price, current_price)

            if highest_price >= entry_price * Decimal("1.08"):
                new_stop = highest_price - (Decimal("1.0") * current_atr)
                dynamic_stop_loss = max(dynamic_stop_loss, new_stop)

            sell_condition = is_vetoed or (current_price <= dynamic_stop_loss)

            if sell_condition:
                reason_str = (
                    "Global Veto (Emergency!)" if is_vetoed else "Stop Loss Triggered"
                )
                logger.info(
                    f"Executing SELL for {coin}. {reason_str}. Price: {current_price}, Stop: {dynamic_stop_loss}"
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
                        f"❌ {sell_type} {coin}: {qty} @ {fmt_price(current_price)} (Entry: {fmt_price(entry_price)} | Conf: {entry_conf})"
                    )
                    coin_reports.append(
                        f"❌ <b>{coin}</b> @ ${fmt_price(current_price)}: SOLD ({sell_type}) | Entry: ${fmt_price(entry_price)} (Conf: {entry_conf})"
                    )
                except Exception as e:
                    logger.error(f"Failed to sell {coin}: {e}")
            else:
                coin_reports.append(
                    f"💼 <b>{coin}</b> @ ${fmt_price(current_price)}: Holding | Entry: ${fmt_price(entry_price)} (Conf: {entry_conf})"
                )

        logger.info(f"Finished processing {coin}.")

    save_bot_state()

    cio_summary = get_cio_summary(
        bot_pl_pct,
        market_hodl_pct,
        bot_alpha,
        market_regime,
        ghost_summary_text,
        coin_reports,
    )

    if notifier:
        header_name = cycle_name or "Swing Bot"
        alpha_label = "🟢 Beating Market" if bot_alpha >= 0 else "🔴 Underperforming"

        msg = f"🔄 <b>{header_name} Cycle Complete</b>\n"
        msg += f"🏦 Starting Bank: ${initial_bank:.2f}\n"
        msg += f"🏦 Remaining Bank: ${current_bank:.2f}\n\n"

        msg += f"🤖 <b>CIO Briefing:</b> {cio_summary}\n\n"
        msg += f"🌐 <b>Market Regime:</b> {market_regime}\n"
        msg += f"📈 <b>Market HODL Benchmark:</b> {market_hodl_pct:+.2f}%\n"
        msg += f"🔥 <b>Bot Alpha (Edge):</b> {bot_alpha:+.2f}% ({alpha_label})\n"
        msg += f"👻 <b>AI Ghost Audit:</b> {ghost_summary_text}\n\n"

        if summary_messages:
            msg += "<b>Executions:</b>\n" + "\n".join(summary_messages) + "\n\n"

        msg += "<b>Coin Reports:</b>\n" + "\n".join(coin_reports)

        if len(msg) > 4000:
            parts = [msg[i : i + 4000] for i in range(0, len(msg), 4000)]
            for part in parts:
                notifier.send_message(part)
        else:
            notifier.send_message(msg)

        notifier.flush()


if __name__ == "__main__":
    run_swing_cycle()
