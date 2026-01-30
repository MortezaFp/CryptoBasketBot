"""
Wallex Crypto Portfolio Rebalancer
A semi-automated tool to rebalance your cryptocurrency portfolio on Wallex exchange.
Run manually (e.g., monthly) to maintain target allocations.
"""

import requests
import time
import sys
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Tuple, Optional
import configparser
import os


# Target Portfolio Allocation (Balanced Growth Model)
TARGET_ALLOCATION = {
    "BTC": 0.35,  # 35%
    "ETH": 0.25,  # 25%
    "SOL": 0.15,  # 15%
    "BNB": 0.15,  # 15%
    "XRP": 0.10,  # 10%
}

QUOTE_CURRENCY = "USDT"
WALLEX_API_BASE = "https://api.wallex.ir"


class WallexAPI:
    """Wrapper for Wallex API calls"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update(
            {"x-api-key": api_key, "Content-Type": "application/json"}
        )

    def get_account_balances(self) -> Dict:
        """Fetch all account balances"""
        try:
            response = self.session.get(f"{WALLEX_API_BASE}/v1/account/balances")
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching balances: {e}")
            if hasattr(e.response, "text"):
                print(f"   Response: {e.response.text}")
            raise

    def get_all_markets(self) -> Dict:
        """Fetch all market data including current prices"""
        try:
            response = self.session.get(f"{WALLEX_API_BASE}/hector/web/v1/markets")
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching markets: {e}")
            if hasattr(e.response, "text"):
                print(f"   Response: {e.response.text}")
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
                    return Decimal(str(price))

            raise Exception(f"Symbol {symbol} not found in markets")

        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching price for {symbol}: {e}")
            raise

    def create_market_order(self, symbol: str, side: str, quantity: Decimal) -> Dict:
        """
        Create a market order
        :param symbol: Trading pair (e.g., 'BTCUSDT')
        :param side: 'BUY' or 'SELL'
        :param quantity: Amount of base asset to buy/sell
        """
        try:
            payload = {
                "symbol": symbol,
                "type": "MARKET",
                "side": side.upper(),
                "quantity": str(quantity),
            }

            response = self.session.post(
                f"{WALLEX_API_BASE}/v1/account/orders", json=payload
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"❌ Error creating {side} order for {symbol}: {e}")
            if hasattr(e.response, "text"):
                print(f"   Response: {e.response.text}")
            raise

    def get_market_precision(self, symbol: str) -> int:
        """
        Get the amount precision (decimal places) for a specific trading pair
        """
        try:
            markets_data = self.get_all_markets()

            if not markets_data.get("success"):
                return 8  # Default precision

            result = markets_data.get("result", {})
            markets = result.get("markets", [])

            # Find the matching symbol
            for market in markets:
                if market.get("symbol") == symbol:
                    precision = market.get("amount_precision", 8)
                    return precision

            return 8  # Default precision if not found

        except Exception:
            return 8  # Default precision on error


def load_config() -> str:
    """Load API key from config.ini or environment variables"""

    # Try environment variable first
    api_key = os.environ.get("WALLEX_API_KEY")
    if api_key:
        print("✓ API Key loaded from environment variable")
        return api_key

    # Try config file
    config_path = os.path.join(os.path.dirname(__file__), "config.ini")
    if os.path.exists(config_path):
        config = configparser.ConfigParser()
        config.read(config_path)
        api_key = config.get("wallex", "api_key", fallback=None)
        if api_key:
            print("✓ API Key loaded from config.ini")
            return api_key

    print("❌ ERROR: API Key not found!")
    print("Please either:")
    print("  1. Set the WALLEX_API_KEY environment variable, or")
    print("  2. Create a config.ini file with the following format:")
    print("\n[wallex]")
    print("api_key = YOUR_API_KEY_HERE\n")
    sys.exit(1)


def fetch_balances(api: WallexAPI, assets: List[str]) -> Dict[str, Decimal]:
    """Fetch current balances for specified assets"""
    print("\n📊 Fetching account balances...")

    try:
        balances_response = api.get_account_balances()

        # Check if API call was successful
        if not balances_response.get("success"):
            raise Exception(
                f"API returned success=false: {balances_response.get('message')}"
            )

        # Parse the response based on Wallex API structure
        # Response: { "result": { "balances": { "BTC": {...}, "ETH": {...} } } }
        balances = {}
        result = balances_response.get("result", {})
        balance_dict = result.get("balances", {})

        # Extract balances for our target assets
        for asset in assets:
            if asset in balance_dict:
                asset_data = balance_dict[asset]
                # Get 'value' field which represents available balance
                balance_value = asset_data.get("value", "0")
                balances[asset] = Decimal(str(balance_value))
            else:
                balances[asset] = Decimal("0")

        for asset, balance in balances.items():
            print(f"  {asset}: {balance}")

        return balances

    except Exception as e:
        print(f"❌ Failed to fetch balances: {e}")
        raise


def fetch_market_data(
    api: WallexAPI, assets: List[str], quote: str
) -> Tuple[Dict[str, Decimal], Dict[str, int]]:
    """Fetch current market prices and precision data for all assets"""
    print(f"\n💹 Fetching market data ({quote} pairs)...")

    prices = {}
    precisions = {}

    for asset in assets:
        if asset == quote:
            prices[asset] = Decimal("1")
            precisions[asset] = 8  # Default for quote currency
            continue

        try:
            symbol = f"{asset}{quote}"
            price = api.get_market_price(symbol)
            precision = api.get_market_precision(symbol)

            prices[asset] = price
            precisions[symbol] = precision

            print(f"  {asset}/{quote}: ${price:,.2f} (precision: {precision} decimals)")
        except Exception as e:
            print(f"  ⚠️  Could not fetch data for {asset}: {e}")
            prices[asset] = Decimal("0")
            precisions[f"{asset}{quote}"] = 8  # Default precision

    return prices, precisions


def calculate_portfolio_metrics(
    balances: Dict[str, Decimal],
    prices: Dict[str, Decimal],
    target_allocation: Dict[str, Decimal],
) -> Tuple[Decimal, Dict, Dict, Dict]:
    """
    Calculate portfolio metrics and rebalancing needs
    Returns: (total_value, current_values, target_values, deltas)
    """
    print("\n📈 Analyzing portfolio...")

    # Calculate current USD value for each asset
    current_values = {}
    for asset, balance in balances.items():
        if asset == QUOTE_CURRENCY:
            current_values[asset] = balance
        elif asset in prices:
            current_values[asset] = balance * prices[asset]
        else:
            current_values[asset] = Decimal("0")

    # Calculate total portfolio value
    total_value = sum(current_values.values())

    if total_value == 0:
        print("❌ Portfolio has zero value!")
        sys.exit(1)

    # Calculate target values based on allocation percentages
    target_values = {}
    for asset, percentage in target_allocation.items():
        target_values[asset] = total_value * Decimal(str(percentage))

    # Calculate deltas (positive = overweight/sell, negative = underweight/buy)
    deltas = {}
    for asset in target_allocation.keys():
        current = current_values.get(asset, Decimal("0"))
        target = target_values[asset]
        deltas[asset] = current - target

    return total_value, current_values, target_values, deltas


def display_rebalancing_plan(
    total_value: Decimal,
    current_values: Dict[str, Decimal],
    target_values: Dict[str, Decimal],
    deltas: Dict[str, Decimal],
    target_allocation: Dict[str, float],
) -> Tuple[List, List]:
    """
    Display the rebalancing plan and return lists of sell and buy actions
    Returns: (sell_actions, buy_actions)
    """
    print("\n" + "=" * 70)
    print("📋 REBALANCING PLAN")
    print("=" * 70)
    print(f"\n💰 Total Portfolio Value: ${total_value:,.2f} {QUOTE_CURRENCY}\n")

    print()

    # Minimum trade threshold (avoid tiny trades) - LOWERED from $10 to $5
    MIN_TRADE_VALUE = Decimal("5")  # Minimum $5 USDT trade

    sell_actions = []
    buy_actions = []

    for asset in sorted(target_allocation.keys()):
        current_value = current_values.get(asset, Decimal("0"))
        target_value = target_values[asset]
        delta = deltas[asset]

        current_pct = (current_value / total_value * 100) if total_value > 0 else 0
        target_pct = target_allocation[asset] * 100

        # Determine action
        if abs(delta) < MIN_TRADE_VALUE:
            action = "HOLD (within threshold)"
            symbol = "⚖️ "
        elif delta > 0:
            action = f"SELL ${abs(delta):,.2f} worth"
            symbol = "📉"
            sell_actions.append((asset, abs(delta)))
        else:
            action = f"BUY ${abs(delta):,.2f} worth"
            symbol = "📈"
            buy_actions.append((asset, abs(delta)))

        print(f"{symbol} {asset}:")
        print(f"   Current: ${current_value:,.2f} ({current_pct:.1f}%)")
        print(f"   Target:  ${target_value:,.2f} ({target_pct:.1f}%)")
        print(f"   Action:  {action}\n")

    total_to_sell = sum(amount for _, amount in sell_actions)
    total_to_buy = sum(amount for _, amount in buy_actions)

    print(f"{'─'*70}")
    print(f"📊 Total to Sell: ${total_to_sell:,.2f} {QUOTE_CURRENCY}")
    print(f"📊 Total to Buy:  ${total_to_buy:,.2f} {QUOTE_CURRENCY}")

    print(f"{'─'*70}\n")

    return sell_actions, buy_actions


def confirm_execution() -> bool:
    """Ask user for confirmation before executing trades"""
    print("⚠️  You are about to execute real trades on Wallex exchange!")
    print("⚠️  This will affect your actual portfolio.\n")

    response = (
        input("Do you want to execute this rebalancing plan? (yes/no): ")
        .strip()
        .lower()
    )

    if response == "yes":
        print("\n✓ Confirmation received. Proceeding with trade execution...\n")
        return True
    else:
        print("\n✗ Operation cancelled by user. No trades were executed.")
        return False


def execute_trades(
    api: WallexAPI,
    sell_actions: List[Tuple[str, Decimal]],
    buy_actions: List[Tuple[str, Decimal]],
    prices: Dict[str, Decimal],
    balances: Dict[str, Decimal],
    precisions: Dict[str, int],
) -> Dict:
    """
    Execute the rebalancing trades with proper precision handling
    1. Execute all SELL orders first
    2. Then execute all BUY orders
    """
    executed_trades = {"sells": [], "buys": []}

    print("=" * 70)
    print("🔄 EXECUTING TRADES")
    print("=" * 70)

    # Commission buffer - reduce all orders by 0.15% for fees
    COMMISSION_BUFFER = Decimal("0.9985")  # 0.15% buffer

    # Phase 1: Execute SELL orders
    if sell_actions:
        print("\n📉 Phase 1: Executing SELL orders...\n")

        for asset, usdt_amount in sell_actions:
            try:
                symbol = f"{asset}{QUOTE_CURRENCY}"
                price = prices[asset]
                precision = precisions.get(symbol, 8)

                # Calculate quantity to sell with commission buffer
                quantity = (usdt_amount / price) * COMMISSION_BUFFER

                # Check if we have enough balance
                available = balances.get(asset, Decimal("0"))
                if quantity > available:
                    quantity = available * Decimal(
                        "0.99"
                    )  # Leave small amount for dust

                # Round down to the correct precision for this market
                precision_str = (
                    "0." + "0" * (precision - 1) + "1" if precision > 0 else "1"
                )
                quantity = quantity.quantize(
                    Decimal(precision_str), rounding=ROUND_DOWN
                )

                print(
                    f"  Selling {quantity} {asset} (~${usdt_amount:,.2f} with 0.15% buffer)"
                )
                print(f"    Using {precision} decimal precision for {symbol}")

                order_result = api.create_market_order(symbol, "SELL", quantity)
                executed_trades["sells"].append(
                    {
                        "asset": asset,
                        "quantity": quantity,
                        "usdt_value": usdt_amount,
                        "order": order_result,
                    }
                )

                print(f"  ✓ SELL order completed for {asset}\n")
                time.sleep(1)  # Rate limiting

            except Exception as e:
                print(f"  ❌ Failed to sell {asset}: {e}\n")
                continue

    # Phase 2: Execute BUY orders with improved USDT utilization
    if buy_actions:
        print("\n📈 Phase 2: Executing BUY orders...\n")

        # Fetch current USDT balance before starting buy phase
        try:
            current_balances = fetch_balances(api, [QUOTE_CURRENCY])
            available_usdt = current_balances.get(QUOTE_CURRENCY, Decimal("0"))
            print(f"💰 Available USDT for purchases: ${available_usdt:,.2f}\n")
        except:
            available_usdt = balances.get(QUOTE_CURRENCY, Decimal("0"))

        # Calculate total planned buy amount
        total_planned_buy = sum(amount for _, amount in buy_actions)

        for asset, usdt_amount in buy_actions:
            try:
                symbol = f"{asset}{QUOTE_CURRENCY}"
                price = prices[asset]
                precision = precisions.get(symbol, 8)

                # Use the planned amount directly (don't do complex proportional logic)
                actual_usdt_to_use = usdt_amount

                # Only reduce if we don't have enough USDT left
                if actual_usdt_to_use > available_usdt:
                    actual_usdt_to_use = available_usdt * Decimal(
                        "0.99"
                    )  # Use almost all remaining

                # Calculate quantity to buy with commission buffer
                quantity = (actual_usdt_to_use / price) * COMMISSION_BUFFER

                # Round down to the correct precision for this market
                precision_str = (
                    "0." + "0" * (precision - 1) + "1" if precision > 0 else "1"
                )
                quantity = quantity.quantize(
                    Decimal(precision_str), rounding=ROUND_DOWN
                )

                print(
                    f"  Buying {quantity} {asset} (~${actual_usdt_to_use:,.2f} with 0.15% buffer)"
                )
                print(f"    Using {precision} decimal precision for {symbol}")

                order_result = api.create_market_order(symbol, "BUY", quantity)
                executed_trades["buys"].append(
                    {
                        "asset": asset,
                        "quantity": quantity,
                        "usdt_value": actual_usdt_to_use,
                        "order": order_result,
                    }
                )

                # Update available USDT for next iteration
                available_usdt -= actual_usdt_to_use

                print(f"  ✓ BUY order completed for {asset}")
                print(
                    f"  💰 Remaining USDT: ${max(Decimal('0'), available_usdt):,.2f}\n"
                )
                time.sleep(1)  # Rate limiting

            except Exception as e:
                print(f"  ❌ Failed to buy {asset}: {e}\n")
                continue

    return executed_trades


def display_final_report(executed_trades: Dict, api: WallexAPI):
    """Display final summary of executed trades"""
    print("\n" + "=" * 70)
    print("📊 FINAL REPORT")
    print("=" * 70)

    print("\n✅ Executed Trades:\n")

    if executed_trades["sells"]:
        print("📉 SELLS:")
        for trade in executed_trades["sells"]:
            print(
                f"   - Sold {trade['quantity']} {trade['asset']} (~${trade['usdt_value']:,.2f})"
            )

    if executed_trades["buys"]:
        print("\n📈 BUYS:")
        for trade in executed_trades["buys"]:
            print(
                f"   - Bought {trade['quantity']} {trade['asset']} (~${trade['usdt_value']:,.2f})"
            )

    if not executed_trades["sells"] and not executed_trades["buys"]:
        print("   No trades were executed (portfolio already balanced)")

    print("\n" + "=" * 70)
    print("✓ Rebalancing complete!")
    print("=" * 70 + "\n")

    # Optionally fetch and display new balances
    try:
        print("📊 Fetching updated balances...")
        assets = list(TARGET_ALLOCATION.keys()) + [QUOTE_CURRENCY]
        new_balances = fetch_balances(api, assets)
        print("\n✓ Portfolio rebalancing completed successfully!\n")
    except:
        print("⚠️  Could not fetch updated balances, but trades were executed.\n")


def main():
    """Main execution flow"""
    print("\n" + "=" * 70)
    print("🔄 WALLEX CRYPTO PORTFOLIO REBALANCER")
    print("=" * 70)
    print("\n📅 Target Allocation (Balanced Growth Model):")
    for asset, pct in TARGET_ALLOCATION.items():
        print(f"   {asset}: {pct*100:.0f}%")
    print()

    try:
        # 1. Load configuration
        api_key = load_config()
        api = WallexAPI(api_key)

        # 2. Fetch data with precision information
        assets = list(TARGET_ALLOCATION.keys()) + [QUOTE_CURRENCY]
        balances = fetch_balances(api, assets)
        prices, precisions = fetch_market_data(
            api, list(TARGET_ALLOCATION.keys()), QUOTE_CURRENCY
        )

        # 3. Calculate metrics
        total_value, current_values, target_values, deltas = (
            calculate_portfolio_metrics(balances, prices, TARGET_ALLOCATION)
        )

        # 4. Display plan
        sell_actions, buy_actions = display_rebalancing_plan(
            total_value,
            current_values,
            target_values,
            deltas,
            TARGET_ALLOCATION,
        )

        # 5. Check if rebalancing is needed
        if not sell_actions and not buy_actions:
            print("✓ Portfolio is already balanced! No action needed.\n")
            return

        # 6. Get user confirmation
        if not confirm_execution():
            return

        # 7. Execute trades with proper precision
        executed_trades = execute_trades(
            api, sell_actions, buy_actions, prices, balances, precisions
        )

        # 8. Display final report
        display_final_report(executed_trades, api)

    except KeyboardInterrupt:
        print("\n\n⚠️  Operation cancelled by user (Ctrl+C)")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ An error occurred: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
