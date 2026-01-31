"""
Wallex Crypto Basket Bot - SIMULATION MODE
Backtesting/Paper Loading script using main.py logic but with simulated balances.
"""

import time
import logging
import sys
import ctypes
from decimal import Decimal, ROUND_DOWN
import main  # Import the real bot logic

import json
import os

# Constants
INITIAL_USDT = Decimal("1000")
SIMULATION_LOG_FILE = os.path.join(main.get_app_path(), "simulation_log.txt")
SIMULATION_STATE_FILE = os.path.join(main.get_app_path(), "simulation_state.json")
SLEEP_INTERVAL = 3600  # Keep same as main, or lower for faster testing if desired

# Setup Simulation Logging
# We want to capture both the bot's logs and our simulation stats
# Configure the root logger to write to file
# Note: main.py already runs logging.basicConfig() on import, which adds a StreamHandler to root.
# We just need to add the FileHandler to root. Messages from main.logger and sim_logger will propagate up.
file_handler = logging.FileHandler(SIMULATION_LOG_FILE, mode="a", encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

root_logger = logging.getLogger()
root_logger.addHandler(file_handler)

sim_logger = logging.getLogger("Simulation")
sim_logger.setLevel(logging.INFO)


class SimulationWallexAPI(main.WallexAPI):
    """
    Simulates Wallex API behavior for testing.
    KEY FEATURES:
    1. USES REAL MARKET DATA: Calls the actual Wallex API for prices/markets.
    2. SIMULATES WALLET: Uses a fake local balance (starts with 1000 USDT).
    3. SIMULATES TRADES: 'Executes' orders mathematically without spending real money.
    """

    def __init__(self):
        # Initialize with dummy key (not used for market data usually, but good to have)
        super().__init__(api_key="SIMULATION_KEY")

        # Try to load state
        loaded_state = self.load_state()

        if loaded_state:
            self.balances = loaded_state["balances"]
            self.initial_value = loaded_state["initial_value"]
            sim_logger.info(f"--- SIMULATION RESUMED ---")
            sim_logger.info(f"loaded from {SIMULATION_STATE_FILE}")
        else:
            # Initialize FAKE Balances directly
            self.balances = {
                "USDT": INITIAL_USDT,
                "BTC": Decimal("0"),
                "ETH": Decimal("0"),
                "SOL": Decimal("0"),
                "BNB": Decimal("0"),
                "XRP": Decimal("0"),
            }
            self.initial_value = INITIAL_USDT
            sim_logger.info(f"--- SIMULATION STARTED (FRESH) ---")

        sim_logger.info(
            f"Balance: {self.balances['USDT']:.2f} USDT (Total assets tracking)"
        )
        sim_logger.info(f"Market Data Source: REAL Wallex API ({main.WALLEX_API_BASE})")

    def load_state(self):
        if not os.path.exists(SIMULATION_STATE_FILE):
            return None
        try:
            with open(SIMULATION_STATE_FILE, "r") as f:
                data = json.load(f)
            # Convert strings back to Decimals
            return {
                "balances": {k: Decimal(v) for k, v in data["balances"].items()},
                "initial_value": Decimal(data["initial_value"]),
            }
        except Exception as e:
            sim_logger.error(f"Failed to load state: {e}")
            return None

    def save_state(self):
        try:
            # Convert Decimals to strings for JSON
            data = {
                "balances": {k: str(v) for k, v in self.balances.items()},
                "initial_value": str(self.initial_value),
            }
            with open(SIMULATION_STATE_FILE, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            sim_logger.error(f"Failed to save state: {e}")

    def get_account_balances(self) -> dict:
        """Return simulated balances in the format expected by the bot"""
        # API Response format: { "success": True, "result": { "balances": { "BTC": {"value": "0.1"}, ... } } }
        formatted_balances = {}
        for coin, amount in self.balances.items():
            formatted_balances[coin] = {"value": str(amount)}

        return {"success": True, "result": {"balances": formatted_balances}}

    def create_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        type: str = "MARKET",
        price: Decimal = None,
    ) -> dict:
        """
        Execute a simulated trade.
        If MARKET: Fetches REAL current market prices.
        If LIMIT: Uses the limit price provided (Assuming immediate fill for simulation simplicity,
                  or we could check vs market price if we wanted to be strict).
        """
        # Symbol is like "BTCUSDT"
        coin = symbol.replace("USDT", "")

        trade_price = price

        # 1. Determine Execution Price
        if type == "MARKET" or trade_price is None:
            try:
                # This calls the PARENT class method which hits the real API
                trade_price = self.get_market_price(symbol)
                sim_logger.info(
                    f"🔎 Fetched REAL price for {symbol}: ${trade_price:,.2f}"
                )
            except Exception as e:
                sim_logger.error(
                    f"Could not fetch price for {symbol} during trade: {e}"
                )
                raise e
        else:
            sim_logger.info(f"⚓ Simulating LIMIT order fill at ${trade_price:,.2f}")

        # 2. Calculate Values
        trade_value_usdt = quantity * trade_price
        fee_rate = Decimal("0.0035")  # Approximate fee (0.35%) - Adjust if needed
        # Taking fee from the received asset usually

        sim_logger.info(
            f"⚡ SIM TRADE ({type}): {side} {quantity} {coin} @ ${trade_price:,.2f} (Val: ${trade_value_usdt:,.2f})"
        )

        if side.upper() == "BUY":
            # Buying Coin with USDT
            cost_usdt = trade_value_usdt

            if self.balances["USDT"] < cost_usdt:
                msg = f"Insufficient USDT for trade. Have {self.balances['USDT']}, need {cost_usdt}"
                sim_logger.error(msg)
                raise Exception(msg)

            # Update Balances
            self.balances["USDT"] -= cost_usdt
            # Fee is taken from the bought asset
            received_coin = quantity * (Decimal("1") - fee_rate)
            self.balances[coin] = self.balances.get(coin, Decimal("0")) + received_coin

            self.save_state()

        elif side.upper() == "SELL":
            # Selling Coin for USDT
            if self.balances.get(coin, Decimal("0")) < quantity:
                msg = f"Insufficient {coin} for trade. Have {self.balances.get(coin, 0)}, need {quantity}"
                sim_logger.error(msg)
                raise Exception(msg)

            # Update Balances
            self.balances[coin] -= quantity
            # Fee is taken from the received USDT
            received_usdt = trade_value_usdt * (Decimal("1") - fee_rate)
            self.balances["USDT"] += received_usdt

            self.save_state()

        # Return success response
        return {
            "success": True,
            "result": {
                "orderId": f"SIM-{int(time.time())}",
                "symbol": symbol,
                "origQty": str(quantity),
                "executedQty": str(quantity),
                "price": str(trade_price),
                "type": type,
            },
        }

    def print_profit_report(self):
        """Calculate and print current profit/loss"""
        total_value_usdt = self.balances["USDT"]

        # Calculate value of crypto holdings
        market_data = self.get_all_markets()  # Get real prices
        if market_data.get("success"):
            markets = market_data.get("result", {}).get("markets", [])
            market_map = {}
            for m in markets:
                try:
                    p = m.get("price")
                    if p is None:
                        continue
                    market_map[m["symbol"]] = Decimal(str(p).replace(",", ""))
                except:
                    continue

            for coin, amount in self.balances.items():
                if coin == "USDT" or amount == 0:
                    continue

                pair = f"{coin}USDT"
                if pair in market_map:
                    value = amount * market_map[pair]
                    total_value_usdt += value

        profit = total_value_usdt - self.initial_value
        profit_pct = (profit / self.initial_value) * 100

        log_msg = (
            f"\n📊 SIMULATION STATS:\n"
            f"   Initial Value: ${self.initial_value:,.2f}\n"
            f"   Current Value: ${total_value_usdt:,.2f}\n"
            f"   P/L: ${profit:,.2f} ({profit_pct:+.2f}%)\n"
            f"   Holdings: { {k: f'{v:.4f}' for k, v in self.balances.items() if v > 0} }\n"
        )
        sim_logger.info(log_msg)
        return {
            "total_value": total_value_usdt,
            "profit": profit,
            "profit_pct": profit_pct,
            "holdings": self.balances,
        }


def run_simulation():
    print(f"Starting Simulation Bot... Logging to {SIMULATION_LOG_FILE}")

    # 1. Init Simulation
    sim_api = SimulationWallexAPI()

    # 2. Init Telegram (Optional)
    notifier = None
    config_path = os.path.join(main.get_app_path(), "config.ini")
    if os.path.exists(config_path):
        import configparser

        config = configparser.ConfigParser()
        config.read(config_path)
        if config.has_section("telegram"):
            token = config.get("telegram", "bot_token", fallback="")
            chat_id = config.get("telegram", "chat_id", fallback="")
            if token and chat_id:
                try:
                    notifier = main.TelegramNotifier(token, chat_id)
                    notifier.send_message(
                        "🧪 <b>Simulation Started</b>\nTesting mode active."
                    )
                    sim_logger.info("✓ Telegram Notification Linked")
                except Exception as e:
                    sim_logger.warning(f"⚠️ Failed to init Telegram: {e}")

    # 3. Infinite Loop
    while True:
        try:
            # Run the rebalance logic using our Sim API
            # This will fetch REAL prices, check our FAKE balances, and execute FAKE trades
            main.run_rebalance_cycle(sim_api, notifier)

            # Print value report after every cycle
            stats = sim_api.print_profit_report()

            # Send Telegram Status Update
            if notifier:
                msg = (
                    f"📊 <b>SIMULATION STATUS</b>\n"
                    f"💰 Value: <b>${stats['total_value']:,.2f}</b>\n"
                    f"📈 P/L: <b>${stats['profit']:,.2f} ({stats['profit_pct']:+.2f}%)</b>\n"
                    f"👜 Holdings:\n"
                )
                for coin, amt in stats["holdings"].items():
                    if amt > 0:
                        msg += f"• {coin}: {amt:.4f}\n"

                notifier.send_message(msg)

        except KeyboardInterrupt:
            print("\nSimulation stopped by user.")
            break
        except Exception as e:
            sim_logger.error(f"Critical Simulation Error: {e}", exc_info=True)

        sim_logger.info(f"Sleeping for {SLEEP_INTERVAL} seconds...")
        time.sleep(SLEEP_INTERVAL)


if __name__ == "__main__":
    # Set Console Title for standalone window
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW(
            "Wallex Bot SIMULATION - [Testing Mode]"
        )
    run_simulation()
