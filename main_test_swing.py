import os
import sys
import time
import logging
import json
from decimal import Decimal
import main_swing
import ctypes

# Constants
INITIAL_USDT = Decimal("1000")
SIMULATION_LOG_FILE = "simulation_log_swing.txt"
SIMULATION_STATE_FILE = "simulation_state_swing.json"

# Setup Simulation Logging
file_handler = logging.FileHandler(SIMULATION_LOG_FILE, mode="a", encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
root_logger = logging.getLogger()
root_logger.addHandler(file_handler)

sim_logger = logging.getLogger("SwingSimulation")
sim_logger.setLevel(logging.INFO)

class SimulationSwingWallexAPI(main_swing.SwingWallexAPI):
    def __init__(self):
        super().__init__(api_key="SIMULATION_KEY")
        
        loaded_state = self.load_state()
        if loaded_state:
            self.balances = loaded_state["balances"]
            self.orders = loaded_state["orders"]
            self.initial_value = loaded_state["initial_value"]
            sim_logger.info("--- SIMULATION RESUMED ---")
        else:
            self.balances = {"USDT": INITIAL_USDT}
            self.orders = []
            self.initial_value = INITIAL_USDT
            sim_logger.info("--- SIMULATION STARTED (FRESH) ---")
            
    def load_state(self):
        if not os.path.exists(SIMULATION_STATE_FILE):
            return None
        try:
            with open(SIMULATION_STATE_FILE, "r") as f:
                data = json.load(f)
            return {
                "balances": {k: Decimal(str(v)) for k, v in data["balances"].items()},
                "orders": data.get("orders", []),
                "initial_value": Decimal(str(data.get("initial_value", "1000")))
            }
        except Exception as e:
            sim_logger.error(f"Failed to load state: {e}")
            return None

    def save_state(self):
        try:
            data = {
                "balances": {k: str(v) for k, v in self.balances.items()},
                "orders": self.orders,
                "initial_value": str(self.initial_value)
            }
            with open(SIMULATION_STATE_FILE, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            sim_logger.error(f"Failed to save state: {e}")

    def get_account_balances(self) -> dict:
        formatted = {k: {"value": str(v)} for k, v in self.balances.items()}
        return {"success": True, "result": {"balances": formatted}}

    def create_order(self, symbol: str, side: str, quantity: Decimal, type: str = "MARKET", price: Decimal = None, client_id: str = None) -> dict:
        try:
            market_price = self.get_market_price(symbol)
        except Exception as e:
            sim_logger.error(f"Could not fetch price for {symbol}: {e}")
            raise e

        coin = symbol.replace("USDT", "")
        trade_value_usdt = Decimal(str(quantity)) * market_price
        fee_rate = Decimal("0.0035")

        if side.upper() == "BUY":
            if self.balances.get("USDT", Decimal("0")) < trade_value_usdt:
                return {"success": False, "message": "Insufficient funds"}
            self.balances["USDT"] -= trade_value_usdt
            received = Decimal(str(quantity)) * (Decimal("1") - fee_rate)
            self.balances[coin] = self.balances.get(coin, Decimal("0")) + received
        else: # SELL
            if self.balances.get(coin, Decimal("0")) < Decimal(str(quantity)):
                return {"success": False, "message": "Insufficient funds"}
            self.balances[coin] -= Decimal(str(quantity))
            received = trade_value_usdt * (Decimal("1") - fee_rate)
            self.balances["USDT"] += received

        client_order_id = client_id or f"SIM-{int(time.time()*1000)}"
        order_record = {
            "clientOrderId": client_order_id,
            "market": symbol,
            "side": side.upper(),
            "status": "FILLED",
            "price": str(market_price),
            "origQty": str(quantity),
            "executedQty": str(quantity),
            "type": type,
            "time": int(time.time() * 1000)
        }
        self.orders.append(order_record)
        self.save_state()
        
        sim_logger.info(f"⚡ SIM TRADE EXECUTION: {side} {quantity} {coin} @ ${market_price:,.2f} (Val: ${trade_value_usdt:,.2f})")
        return {"success": True, "result": order_record}

    def get_last_filled_buy_order(self, symbol: str) -> dict:
        filled_buys = [o for o in self.orders if o.get("market") == f"{symbol}USDT" and o.get("side") == "BUY"]
        if filled_buys:
            filled_buys.sort(key=lambda x: x.get("time", 0), reverse=True)
            return filled_buys[0]
        return None

def run_simulation():
    print(f"Starting Swing Simulation Bot... Logging to {SIMULATION_LOG_FILE}")
    sim_api = SimulationSwingWallexAPI()
    
    # Run standard logic via dependency injection
    main_swing.run_swing_cycle(api=sim_api)

    # Print Profit Report
    total_value = sim_api.balances.get("USDT", Decimal("0"))
    market_map = {}
    market_data = sim_api.get_all_markets()
    if market_data.get("success"):
        for m in market_data.get("result", {}).get("markets", []):
            try:
                market_map[m["symbol"]] = Decimal(str(m.get("price", "0")).replace(",", ""))
            except:
                pass

    for coin, amt in sim_api.balances.items():
        if coin != "USDT" and amt > 0:
            pair = f"{coin}USDT"
            if pair in market_map:
                total_value += amt * market_map[pair]

    profit = total_value - sim_api.initial_value
    profit_pct = (profit / sim_api.initial_value) * 100

    log_msg = (
        f"\n📊 SWING SIMULATION STATS:\n"
        f"   Initial Value: ${sim_api.initial_value:,.2f}\n"
        f"   Current Value: ${total_value:,.2f}\n"
        f"   P/L: ${profit:,.2f} ({profit_pct:+.2f}%)\n"
        f"   Holdings: { {k: f'{v:.4f}' for k, v in sim_api.balances.items() if v > 0} }\n"
    )
    sim_logger.info(log_msg)

if __name__ == "__main__":
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW("Wallex Swing Bot SIMULATION")
    run_simulation()
