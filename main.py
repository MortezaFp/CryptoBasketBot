"""
Wallex Crypto Basket Bot (Robust Long-Running Version)
Strategy: Threshold Rebalancing with Circuit Breaker
Platform: Wallex (Iran)
Based on original main.py script
"""

import requests
import time
import uuid
import sys
import os
import logging
import threading
import queue
import configparser
import ctypes
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple, Any

# --- Configuration & Constants ---

# Target Allocation (Total must sum to 1.0)
TARGET_ALLOCATION = {
    "BTC": 0.25,
    "ETH": 0.15,
    "XAUT": 0.15,  # Gold
    "USDT": 0.15,  # Base Currency
    "SOL": 0.10,
    "BNB": 0.10,
    "XRP": 0.10,
}

REBALANCE_THRESHOLD_DEFAULT = 0.05
# Dynamic Thresholds (Idea 3: Volatility Adjusted)
# Higher threshold = Less trading / Less fees
# Lower threshold = Tighter tracking / More fees
THRESHOLDS = {
    "BTC": 0.03,  # 3% (Stable)
    "ETH": 0.03,  # 3% (Stable)
    "XAUT": 0.02,  # 2% (Stable - Gold)
    "USDT": 0.02,  # 2% (Base)
    "SOL": 0.07,  # 7% (Volatile)
    "BNB": 0.05,  # 5% (Medium)
    "XRP": 0.07,  # 7% (Volatile)
}
MIN_TRADE_USDT = 1.0  # Minimum trade size in USDT
PANIC_DROP_THRESHOLD = -15.0  # -15% 24h change circuit breaker for BUYs
SLEEP_INTERVAL = 3600  # Run every 1 hour (3600 seconds)
LIMIT_ORDER_TIMEOUT_SEC = 60
ORDER_POLL_INTERVAL_SEC = 5

QUOTE_CURRENCY = "USDT"
WALLEX_API_BASE = "https://api.wallex.ir"

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("WallexBot")


class WallexAPI:
    """Wrapper for Wallex API calls (Base Implementation)"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update(
            {"x-api-key": api_key, "Content-Type": "application/json"}
        )

    def get_account_balances(self) -> Dict:
        """Fetch all account balances"""
        try:
            response = self.session.get(
                f"{WALLEX_API_BASE}/v1/account/balances", timeout=15
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Error fetching balances: {e}")
            if hasattr(e, "response") and hasattr(e.response, "text"):
                logger.error(f"   Response: {e.response.text}")
            raise

    def get_all_markets(self) -> Dict:
        """Fetch all market data including current prices"""
        try:
            response = self.session.get(
                f"{WALLEX_API_BASE}/hector/web/v1/markets", timeout=15
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Error fetching markets: {e}")
            if hasattr(e, "response") and hasattr(e.response, "text"):
                logger.error(f"   Response: {e.response.text}")
            raise

    def get_market_price(self, symbol: str) -> Decimal:
        """
        Fetch current market price for a trading pair from all markets data
        Example: symbol = 'BTCUSDT'
        """
        try:
            markets_data = self.get_all_markets()

            # Parse response structure: result.markets is an array
            if not markets_data.get("success"):
                raise Exception(
                    f"API returned success=false: {markets_data.get('message')}"
                )

            result = markets_data.get("result", {})
            markets = result.get("markets", [])

            # Find the matching symbol
            for market in markets:
                if market.get("symbol") == symbol:
                    price = market.get("price", "0")
                    return Decimal(str(price).replace(",", ""))

            raise Exception(f"Symbol {symbol} not found in markets")

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Error fetching price for {symbol}: {e}")
            raise

    def create_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        type: str = "MARKET",
        price: Decimal = None,
        client_id: Optional[str] = None,
    ) -> Dict:
        """
        Create an order (MARKET or LIMIT)
        :param symbol: Trading pair (e.g., 'BTCUSDT')
        :param side: 'BUY' or 'SELL'
        :param quantity: Amount of base asset
        :param type: 'MARKET' or 'LIMIT'
        :param price: Required for LIMIT orders
        """
        try:
            # Note: Wallex API expects strings for decimals
            payload = {
                "symbol": symbol,
                "type": type.upper(),
                "side": side.upper(),
                "quantity": str(quantity),
            }

            if client_id:
                payload["client_id"] = client_id

            if type.upper() == "LIMIT":
                if price is None:
                    raise ValueError("Price is required for LIMIT orders")
                payload["price"] = str(price)

            logger.info(
                f"🚀 Sending {side} {type} Order: {quantity} {symbol} @ {price if price else 'MARKET'}"
            )

            # UNCOMMENT LINE BELOW TO ENABLE REAL TRADING
            response = self.session.post(
                f"{WALLEX_API_BASE}/v1/account/orders", json=payload, timeout=15
            )

            # For testing without real trades, you might comment out the above and return dummy data:
            # return {"success": True, "result": {"orderId": "test"}}

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Error creating {side} order for {symbol}: {e}")
            if hasattr(e, "response") and hasattr(e.response, "text"):
                logger.error(f"   Response: {e.response.text}")
            raise

    def get_order(self, client_id: str) -> Dict:
        """Fetch order details by client ID"""
        try:
            response = self.session.get(
                f"{WALLEX_API_BASE}/v1/account/orders/{client_id}", timeout=15
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Error fetching order {client_id}: {e}")
            if hasattr(e, "response") and hasattr(e.response, "text"):
                logger.error(f"   Response: {e.response.text}")
            raise

    def cancel_order(self, client_id: str) -> Dict:
        """Cancel order by client ID"""
        try:
            response = self.session.delete(
                f"{WALLEX_API_BASE}/v1/account/orders/{client_id}", timeout=15
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Error cancelling order {client_id}: {e}")
            if hasattr(e, "response") and hasattr(e.response, "text"):
                logger.error(f"   Response: {e.response.text}")
            raise

    def get_market_precision(self, symbol: str) -> int:
        """
        Get the amount precision (decimal places) for a specific trading pair
        """
        try:
            markets_data = self.get_all_markets()

            if not markets_data.get("success"):
                return 8  # Default

            result = markets_data.get("result", {})
            markets = result.get("markets", [])

            for market in markets:
                if market.get("symbol") == symbol:
                    return market.get("amount_precision", 8)

            return 8
        except Exception:
            return 8


# --- Helper Functions (Adapted for Robustness) ---


class TelegramNotifier:
    """Simple Telegram Notification Handler"""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.proxies = None
        self._queue: "queue.Queue[Tuple[str, int]]" = queue.Queue()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def send_message(self, message: str, retries=5):
        """Send a text message to the configured chat in-order"""
        if not self.bot_token or not self.chat_id:
            return
        self._queue.put((message, retries))

    def _worker_loop(self):
        """Process telegram messages sequentially to preserve order"""
        while True:
            message, retries = self._queue.get()
            try:
                url = f"{self.base_url}/sendMessage"
                payload = {
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                }

                for i in range(retries):
                    try:
                        response = requests.post(
                            url, json=payload, timeout=10, proxies=self.proxies
                        )
                        if response.status_code == 200:
                            break
                        logger.warning(
                            f"⚠️ Telegram send failed (Attempt {i+1}/{retries}): {response.text}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"⚠️ Telegram connection failed (Attempt {i+1}/{retries}): {e}"
                        )

                    if i < retries - 1:
                        time.sleep(2)
                else:
                    logger.error(
                        "❌ Telegram Notification Failed to send after multiple retries."
                    )
            finally:
                self._queue.task_done()

    def flush(self):
        """Block until all queued messages are sent"""
        self._queue.join()


def get_app_path():
    """Get the base path of the application, compatible with PyInstaller"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config() -> str:
    """Load API key from config.ini or environment variables"""
    api_key = os.environ.get("WALLEX_API_KEY")
    if api_key:
        logger.info("✓ API Key loaded from environment variable")
        return api_key

    config_path = os.path.join(get_app_path(), "config.ini")
    if os.path.exists(config_path):
        config = configparser.ConfigParser()
        config.read(config_path)
        api_key = config.get("wallex", "api_key", fallback=None)
        if api_key:
            logger.info("✓ API Key loaded from config.ini")
            return api_key

    logger.critical(f"❌ ERROR: API Key not found! Checked: {config_path}")
    sys.exit(1)


def load_telegram_config() -> Tuple[Optional[str], Optional[str]]:
    """Load Telegram bot token/chat id from environment or config.ini"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if token and chat_id:
        logger.info("✓ Telegram config loaded from environment variables")
        return token, chat_id

    config_path = os.path.join(get_app_path(), "config.ini")
    if os.path.exists(config_path):
        config = configparser.ConfigParser()
        config.read(config_path)
        if config.has_section("telegram"):
            token = config.get("telegram", "bot_token", fallback="")
            chat_id = config.get("telegram", "chat_id", fallback="")
            if token and chat_id:
                logger.info("✓ Telegram config loaded from config.ini")
                return token, chat_id

    return None, None


def get_all_market_info(api: WallexAPI) -> Dict[str, Dict]:
    """Helper to get a map of symbol -> market_info for quick lookup"""
    markets_data = api.get_all_markets()
    if not markets_data.get("success"):
        raise Exception("Failed to fetch market data")

    market_map = {}
    for m in markets_data.get("result", {}).get("markets", []):
        market_map[m["symbol"]] = m
    return market_map


def _to_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value).replace(",", ""))
    except Exception:
        return Decimal(default)


def _is_order_filled(order: Dict) -> bool:
    status = str(order.get("status", "")).upper()
    if status in {"FILLED", "DONE", "COMPLETED", "CLOSED"}:
        return True

    executed_percent = order.get("executedPercent")
    try:
        if executed_percent is not None and float(executed_percent) >= 100:
            return True
    except Exception:
        pass

    orig_qty = _to_decimal(order.get("origQty", "0"))
    exec_qty = _to_decimal(order.get("executedQty", "0"))
    if orig_qty > 0 and exec_qty >= orig_qty:
        return True

    return False


def place_limit_then_market(
    api: WallexAPI,
    symbol: str,
    side: str,
    quantity: Decimal,
    limit_price: Decimal,
    notifier: Optional[TelegramNotifier] = None,
    timeout_sec: int = LIMIT_ORDER_TIMEOUT_SEC,
    poll_interval_sec: int = ORDER_POLL_INTERVAL_SEC,
):
    """Place a limit order and, if not filled within timeout, cancel and market the remainder."""
    client_id = f"bot-{uuid.uuid4().hex[:16]}"
    order_resp = api.create_order(
        symbol, side, quantity, type="LIMIT", price=limit_price, client_id=client_id
    )

    if not order_resp.get("success"):
        raise Exception(f"Limit order failed: {order_resp.get('message')}")

    order_client_id = order_resp.get("result", {}).get("clientOrderId") or client_id

    start = time.monotonic()
    last_status = None

    while time.monotonic() - start < timeout_sec:
        try:
            status_resp = api.get_order(order_client_id)
            if status_resp.get("success"):
                order = status_resp.get("result", {})
                last_status = order
                if _is_order_filled(order):
                    logger.info(f"✅ Limit order filled: {order_client_id}")
                    return
                status = str(order.get("status", "")).upper()
                if status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                    break
        except Exception as e:
            logger.warning(f"⚠️ Order status check failed for {order_client_id}: {e}")

        time.sleep(poll_interval_sec)

    logger.warning(
        f"⏳ Limit order timeout ({timeout_sec}s). Cancelling {order_client_id} and placing market order."
    )
    if notifier:
        notifier.send_message(
            f"⏳ <b>LIMIT TIMEOUT</b>\n{side} {symbol} not filled in {timeout_sec}s. Switching to MARKET."
        )

    try:
        api.cancel_order(order_client_id)
    except Exception as e:
        logger.warning(f"⚠️ Failed to cancel order {order_client_id}: {e}")

    if not last_status:
        try:
            status_resp = api.get_order(order_client_id)
            if status_resp.get("success"):
                last_status = status_resp.get("result", {})
        except Exception:
            last_status = None

    orig_qty = _to_decimal(
        (last_status or {}).get("origQty", str(quantity)), default=str(quantity)
    )
    exec_qty = _to_decimal((last_status or {}).get("executedQty", "0"))
    remaining_qty = orig_qty - exec_qty
    qty_exp = quantity.as_tuple().exponent
    quant = Decimal("1") if qty_exp >= 0 else Decimal("1").scaleb(qty_exp)
    remaining_qty = remaining_qty.quantize(quant, rounding=ROUND_DOWN)
    if remaining_qty <= 0:
        logger.info(f"✅ Order already fully executed before cancel: {order_client_id}")
        return

    logger.info(
        f"⚡ Placing MARKET {side} for remaining {remaining_qty} {symbol} (after partial fill)."
    )
    api.create_order(symbol, side, remaining_qty, type="MARKET")


def run_rebalance_cycle(api: WallexAPI, notifier: TelegramNotifier = None):
    """Main logic for a single rebalancing cycle"""
    logger.info("--- Starting Rebalance Cycle ---")

    # 1. Fetch Balances & Markets
    balances_resp = api.get_account_balances()
    if not balances_resp.get("success"):
        logger.error("Failed to get balances")
        return

    balance_map = {}
    for coin, data in balances_resp.get("result", {}).get("balances", {}).items():
        balance_map[coin] = Decimal(str(data.get("value", 0)))

    market_map = get_all_market_info(api)

    # 2. Calculate Portfolio State
    asset_values = {}  # coin -> value_in_usdt
    prices = {}  # coin -> price_usdt
    precisions = {}  # coin -> precision_int
    changes_24h = {}  # coin -> percent_change
    changes_7d = {}  # coin -> percent_change_7d

    portfolio_value_usdt = Decimal("0")

    # Handle USDT Base
    stock_usdt = balance_map.get("USDT", Decimal("0"))
    asset_values["USDT"] = stock_usdt
    prices["USDT"] = Decimal("1")
    portfolio_value_usdt += stock_usdt

    # Handle Other Coins
    for coin in TARGET_ALLOCATION:
        if coin == "USDT":
            continue

        pair = f"{coin}USDT"
        market = market_map.get(pair)

        if not market:
            logger.warning(f"⚠️ No market data for {pair}. Skipping.")
            continue

        try:
            price = Decimal(str(market.get("price", 0)).replace(",", ""))
        except:
            logger.warning(f"⚠️ Invalid price for {pair}. Skipping.")
            continue

        change = float(market.get("change_24h", 0))
        change_week = (
            float(market.get("change_7D", 0)) if "change_7D" in market else 0.0
        )

        precision = market.get("amount_precision", 8)

        balance = balance_map.get(coin, Decimal("0"))
        value = balance * price

        prices[coin] = price
        asset_values[coin] = value
        changes_24h[coin] = change
        changes_7d[coin] = change_week
        precisions[coin] = precision

        portfolio_value_usdt += value

    logger.info(f"💰 Total Portfolio Value: ${portfolio_value_usdt:,.2f} USDT")

    if notifier:
        notifier.send_message("➖➖➖➖➖➖➖➖➖➖")
        # Build a detailed status message for Telegram
        msg = (
            f"⏱ <b>CYCLE REPORT</b>\n💰 Total: <b>${portfolio_value_usdt:,.2f}</b>\n\n"
        )

        # Sort by coin name or value
        for coin in sorted(TARGET_ALLOCATION.keys()):
            if coin not in asset_values:
                continue

            p = prices.get(coin, 0)
            cur = asset_values[coin]
            target_pct = TARGET_ALLOCATION[coin]
            deviation = (
                (cur - (portfolio_value_usdt * Decimal(str(target_pct))))
                / (portfolio_value_usdt * Decimal(str(target_pct)))
                if portfolio_value_usdt > 0
                else 0
            )

            threshold = THRESHOLDS.get(coin, REBALANCE_THRESHOLD_DEFAULT)
            # Check for volatility
            week_change = abs(changes_7d.get(coin, 0))
            if week_change > 15:
                threshold += 0.02

            icon = "✅"
            if abs(deviation) > threshold:
                icon = "⚠️"  # Rebalance needed

            msg += f"{icon} <b>{coin}</b>: ${p:,.2f} | ${cur:.1f} ({deviation*100:+.1f}%)\n"

        notifier.send_message(msg)

    if portfolio_value_usdt < 10:
        logger.warning("Portfolio too small to rebalance.")
        return

    # 3. Calculate Thresholds & Deviations
    trades = []  # List of dicts

    for coin, target_pct in TARGET_ALLOCATION.items():
        if coin not in asset_values:
            continue

        current_val = asset_values[coin]
        target_val = portfolio_value_usdt * Decimal(str(target_pct))

        deviation_val = current_val - target_val
        deviation_pct = deviation_val / target_val if target_val > 0 else Decimal("0")

        # Determine display price (1.00 for USDT)
        current_price = prices.get(
            coin, Decimal("1") if coin == "USDT" else Decimal("0")
        )

        # [Strategy #5: Dynamic Thresholds]
        # Use specific threshold for coin, otherwise default
        threshold = THRESHOLDS.get(coin, REBALANCE_THRESHOLD_DEFAULT)

        # [Idea: Volatility Adjustment] - If coin is moving crazy fast (e.g. 20% in week), widen threshold
        week_change = abs(changes_7d.get(coin, 0))
        if week_change > 15:
            threshold += 0.02  # Add 2% buffer if highly volatile
            logger.info(
                f"   Note: High Volatility on {coin} ({week_change:.1f}%), widened threshold to {threshold*100:.1f}%"
            )

        logger.info(
            f"   {coin}: Price=${current_price:,.2f} | Cur=${current_val:.2f} | Tgt=${target_val:.2f} | Dev={deviation_pct*100:+.2f}% | Thr={threshold*100:.1f}%"
        )

        # Skip trade logic for USDT itself (it balances implicitly via other trades)
        if coin == "USDT":
            continue

        if abs(deviation_pct) > threshold:
            # Action Needed
            trade_val_usdt = abs(deviation_val)
            amount = trade_val_usdt / prices[coin]

            side = "SELL" if deviation_val > 0 else "BUY"

            if trade_val_usdt < MIN_TRADE_USDT:
                logger.info(
                    f"      -> {side} signal but size ${trade_val_usdt:.2f} < ${MIN_TRADE_USDT}. Ignoring."
                )
                continue

            trades.append(
                {
                    "coin": coin,
                    "side": side,
                    "amount": amount,
                    "usdt_val": trade_val_usdt,
                    "precision": precisions[coin],
                    "limit_price": prices[coin],  # Base price, adjusted later
                }
            )

    # 4. Execute Trades (SELLs then BUYs)
    sells = [t for t in trades if t["side"] == "SELL"]
    buys = [t for t in trades if t["side"] == "BUY"]

    # Execute Sells
    for t in sells:
        coin = t["coin"]
        limit_p = t["limit_price"]

        # Quantize amount
        precision_str = (
            "0." + "0" * (t["precision"] - 1) + "1" if t["precision"] > 0 else "1"
        )
        qty = t["amount"].quantize(Decimal(precision_str), rounding=ROUND_DOWN)

        logger.info(
            f"      -> Executing LIMIT SELL for {coin}: {qty} @ ${limit_p:,.2f}"
        )
        if notifier:
            notifier.send_message(
                f"📉 <b>SELL EXECUTION</b>\nSelling {qty} #{coin} @ ${limit_p:,.2f}"
            )

        place_limit_then_market(
            api,
            f"{t['coin']}USDT",
            "SELL",
            qty,
            limit_p,
            notifier=notifier,
        )

    # Execute Buys (with Circuit Breaker & Trend Filter)
    for t in buys:
        coin = t["coin"]
        change = changes_24h.get(coin, 0)

        if change < PANIC_DROP_THRESHOLD:
            logger.warning(
                f"🛑 CIRCUIT BREAKER: Skipping BUY for {coin}. Drop {change:.2f}% < {PANIC_DROP_THRESHOLD}%"
            )
            if notifier:
                notifier.send_message(
                    f"🛑 <b>CIRCUIT BREAKER</b>\nSkipping BUY for #{coin}. Drop {change:.2f}%"
                )
            continue

        # [Strategy #3: Don't catch falling knife]
        # If dropping moderately (-8% to -15%), place limit order LOWER
        limit_p = t["limit_price"]
        if -15 < change < -8:
            discount = Decimal("0.98")  # Buy 2% lower
            limit_p = limit_p * discount
            logger.info(
                f"      -> 'Falling Knife' check: {coin} down {change:.1f}%. Placing limit buy 2% lower @ ${limit_p:.2f}"
            )

        # Quantize amount
        precision_str = (
            "0." + "0" * (t["precision"] - 1) + "1" if t["precision"] > 0 else "1"
        )
        qty = t["amount"].quantize(Decimal(precision_str), rounding=ROUND_DOWN)

        logger.info(f"      -> Executing LIMIT BUY for {coin}: {qty} @ ${limit_p:,.2f}")
        if notifier:
            notifier.send_message(
                f"📈 <b>BUY EXECUTION</b>\nBuying {qty} #{coin} @ ${limit_p:,.2f}"
            )

        place_limit_then_market(
            api,
            f"{coin}USDT",
            "BUY",
            qty,
            limit_p,
            notifier=notifier,
        )

    # Send Cycle Report after completing orders
    if (sells or buys) and notifier:
        # Re-fetch balances to get updated portfolio state
        balances_resp = api.get_account_balances()
        if balances_resp.get("success"):
            balance_map_updated = {}
            for coin, data in (
                balances_resp.get("result", {}).get("balances", {}).items()
            ):
                balance_map_updated[coin] = Decimal(str(data.get("value", 0)))

            # Recalculate portfolio values
            asset_values_updated = {}
            portfolio_value_updated = Decimal("0")

            # Handle USDT
            stock_usdt_updated = balance_map_updated.get("USDT", Decimal("0"))
            asset_values_updated["USDT"] = stock_usdt_updated
            portfolio_value_updated += stock_usdt_updated

            # Handle other coins
            for coin in TARGET_ALLOCATION:
                if coin == "USDT":
                    continue
                balance_updated = balance_map_updated.get(coin, Decimal("0"))
                value_updated = balance_updated * prices[coin]
                asset_values_updated[coin] = value_updated
                portfolio_value_updated += value_updated

            # Build and send the cycle report
            msg = f"⏱ <b>POST-TRADE REPORT</b>\n💰 Total: <b>${portfolio_value_updated:,.2f}</b>\n\n"

            for coin in sorted(TARGET_ALLOCATION.keys()):
                if coin not in asset_values_updated:
                    continue

                p = prices.get(coin, 0)
                cur = asset_values_updated[coin]
                target_pct = TARGET_ALLOCATION[coin]
                deviation = (
                    (cur - (portfolio_value_updated * Decimal(str(target_pct))))
                    / (portfolio_value_updated * Decimal(str(target_pct)))
                    if portfolio_value_updated > 0
                    else 0
                )

                threshold = THRESHOLDS.get(coin, REBALANCE_THRESHOLD_DEFAULT)
                # Check for volatility
                week_change = abs(changes_7d.get(coin, 0))
                if week_change > 15:
                    threshold += 0.02

                icon = "✅"
                if abs(deviation) > threshold:
                    icon = "⚠️"  # Rebalance needed

                msg += f"{icon} <b>{coin}</b>: ${p:,.2f} | ${cur:.1f} ({deviation*100:+.1f}%)\n"

            notifier.send_message(msg)

    logger.info("--- Cycle Complete ---")


def main():
    """Application Entry Point - Infinite Loop"""
    print("==========================================")
    print("   Wallex Crypto Rebalancing Bot V2.0     ")
    print("   Running in Long-Processing Mode        ")
    print("==========================================\n")

    api_key = load_config()

    # Init Telegram
    notifier = None
    token, chat_id = load_telegram_config()
    if token and chat_id:
        try:
            notifier = TelegramNotifier(token, chat_id)
            notifier.send_message(
                "🤖 <b>Bot Started</b>\nListening for opportunities..."
            )
            logger.info(f"✓ Telegram Notification Enabled")
        except Exception as e:
            logger.warning(f"⚠️ Failed to init Telegram: {e}")

    api = WallexAPI(api_key)

    run_once = os.environ.get("RUN_ONCE", "").lower() == "true"
    in_github_actions = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

    if run_once or in_github_actions:
        try:
            run_rebalance_cycle(api, notifier)
        except Exception as e:
            logger.error(f"Critical Error in main loop: {e}", exc_info=True)
        if notifier:
            notifier.flush()
        return

    while True:
        try:
            run_rebalance_cycle(api, notifier)
        except Exception as e:
            logger.error(f"Critical Error in main loop: {e}", exc_info=True)
            # Logic to handle persistent errors could go here (e.g. backoff)

        logger.info(f"Sleeping for {SLEEP_INTERVAL} seconds...")
        time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    # Set Console Title for standalone window
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW("Wallex Crypto Basket Bot - LIVE")
    main()
