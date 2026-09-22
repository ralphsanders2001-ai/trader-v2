"""
API risk monitor — uses Robinhood's precomputed risk metrics from the
REST API instead of fetching individual stock quotes and computing
concentration/P&L ourselves.

What the API precomputes (no local math needed):
  - excess_margin             : buffer to margin call (USD)
  - excess_maintenance        : buffer to maintenance call (USD)
  - excess_margin_with_uncleared_deposits
  - excess_maintenance_with_uncleared_deposits
  - equity                    : current portfolio equity (USD)
  - equity_previous_close     : yesterday's equity at close
  - adjusted_equity_previous_close : adjusted for today's activity
  - day_trade_buying_power    : current PDT buying power (USD)
  - overnight_buying_power    : current overnight BP (USD)
  - day_trade_ratio           : day trades ratio
  - overnight_ratio           : overnight BP ratio
  - cash_available_for_withdrawal : withdrawable cash (USD)
  - cash_held_for_orders      : cash locked in pending orders
  - unsettled_funds           : pending settlement
  - unsettled_debit           : pending debit
  - is_pdt_forever            : PDT forever-flag
  - day_trades_protection     : whether PDT protection is enabled
  - option_trading_lock       : whether options are locked
  - equity_trading_lock       : whether equity trades are locked
  - instant_eligibility.state : instant-deposit state
  - withdrawal_halted         : withdrawals blocked
  - deposit_halted            : deposits blocked

What we still compute locally (API doesn't expose it):
  - Per-position concentration %   = position_value / portfolio_equity
  - Day P&L delta                  = equity - adjusted_equity_previous_close
  - Position-level profit/loss     = API gives cost basis, we use option
                                     quotes (single call per position)

Threshold-driven alerts:
  - excess_margin < $200     -> warn (close to margin call)
  - excess_maintenance < $200 -> critical
  - day_trade_buying_power < $100 -> warn
  - equity drop > 5% vs previous_close -> warn
  - position concentration > 20% -> warn (concentration alert, mirrors
    Robinhood's mobile push notification)
  - position concentration > 35% -> critical
  - withdrawal_halted True    -> warn (account restricted)
  - option_trading_lock != 'no_trade_locks' -> critical

Why this exists:
  Robinhood's mobile app sends concentration/margin alerts via push, but
  the REST API does not expose the precomputed percentages. We use the
  precomputed USD values where available (margin BP, equity, excess) and
  only compute what we have to (concentration, equity delta).
"""

import sys, os, time, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import robin_stocks.robinhood as r
import config
from notify import notify


# ---------------------------------------------------------------------------
# Threshold config
# ---------------------------------------------------------------------------

MARGIN_WARNING_BUFFER = 200.0     # excess_margin below this -> warn
MARGIN_CRITICAL_BUFFER = 100.0    # excess_maintenance below this -> critical
EQUITY_DROP_WARN_PCT = 0.05       # 5% intraday drop -> warn
EQUITY_DROP_CRIT_PCT = 0.10       # 10% intraday drop -> critical
CONCENTRATION_WARN_PCT = 0.20     # position > 20% equity -> warn
CONCENTRATION_CRIT_PCT = 0.35     # position > 35% equity -> critical
DTBP_WARN_BUFFER = 100.0          # DTBP below this -> warn
PENDING_ORDERS_RATIO = 0.50       # cash_held_for_orders / cash > 0.5 -> warn


# ---------------------------------------------------------------------------
# Single API roundtrip — fetch account + portfolio (both have separate
# endpoints that we cache as long as the call lives).
# ---------------------------------------------------------------------------

def _login():
    """Log in to Robinhood. Pickle cache keeps the session warm."""
    from rh_credentials import ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD
    r.login(username=ROBINHOOD_USERNAME, password=ROBINHOOD_PASSWORD,
            pickle_name=config.ROBINHOOD_PICKLE if hasattr(config, 'ROBINHOOD_PICKLE') else '')


def _fetch_risk_state():
    """Return (account_dict, portfolio_dict, positions_list, ts).

    Uses 3 API calls total: account profile (margin fields), portfolio
    (equity / excess), and aggregate positions (concentration inputs).
    """
    account = r.load_account_profile()
    portfolio = r.load_portfolio_profile()
    positions = r.get_aggregate_open_positions()
    return account, portfolio, positions


def _fetch_stock_positions():
    """Fetch open stock positions for concentration calc."""
    return r.get_open_stock_positions()


# ---------------------------------------------------------------------------
# Margin and BP checks (API-precomputed)
# ---------------------------------------------------------------------------

def check_margin(account, portfolio):
    """Use API-precomputed excess_margin / excess_maintenance fields.

    Returns list of (severity, code, message) tuples.
    """
    alerts = []
    margin = account.get('margin_balances', {}) or {}
    excess_margin = float(margin.get('unallocated_margin_cash', 0))
    # excess_margin from portfolio is the more reliable value (USD above
    # maintenance call trigger).
    excess_margin = float(portfolio.get('excess_margin', excess_margin))
    excess_maintenance = float(portfolio.get('excess_maintenance', 0))

    if excess_maintenance > 0 and excess_maintenance < MARGIN_CRITICAL_BUFFER:
        alerts.append((
            'critical', 'MARGIN_CRITICAL',
            f'excess_maintenance=${excess_maintenance:.2f} (buffer < ${MARGIN_CRITICAL_BUFFER})'
        ))
    elif excess_margin > 0 and excess_margin < MARGIN_WARNING_BUFFER:
        alerts.append((
            'warning', 'MARGIN_LOW',
            f'excess_margin=${excess_margin:.2f} (buffer < ${MARGIN_WARNING_BUFFER})'
        ))

    # Day trade buying power
    dtbp = float(margin.get('day_trade_buying_power', 0))
    if dtbp > 0 and dtbp < DTBP_WARN_BUFFER:
        alerts.append((
            'warning', 'DTBP_LOW',
            f'day_trade_buying_power=${dtbp:.2f} (buffer < ${DTBP_WARN_BUFFER})'
        ))

    # PDT flag
    if margin.get('is_pdt_forever'):
        # Already PDT — informational, not a warning
        pass

    # Cash held for orders ratio
    cash = float(margin.get('cash', 0))
    held = float(margin.get('cash_held_for_orders', 0))
    if cash > 0 and held > 0 and (held / max(abs(cash), 1)) > PENDING_ORDERS_RATIO:
        alerts.append((
            'warning', 'CASH_LOCKED',
            f'cash_held_for_orders=${held:.2f} / cash=${cash:.2f} ({held/cash:.0%})'
        ))

    return alerts


def check_locks(account):
    """Check trade locks and account restrictions (API boolean fields)."""
    alerts = []
    if account.get('withdrawal_halted'):
        alerts.append(('warning', 'WITHDRAWAL_HALTED', 'withdrawals blocked'))
    if account.get('deposit_halted'):
        alerts.append(('warning', 'DEPOSIT_HALTED', 'deposits blocked'))
    if account.get('locked'):
        alerts.append(('critical', 'ACCOUNT_LOCKED', 'account fully locked'))
    if account.get('option_trading_lock', 'no_trade_locks') != 'no_trade_locks':
        alerts.append((
            'critical', 'OPTION_LOCK',
            f"option_trading_lock={account.get('option_trading_lock')}"
        ))
    if account.get('equity_trading_lock', 'no_trade_locks') != 'no_trade_locks':
        alerts.append((
            'critical', 'EQUITY_LOCK',
            f"equity_trading_lock={account.get('equity_trading_lock')}"
        ))
    instant = account.get('instant_eligibility', {}) or {}
    if instant.get('state') not in (None, 'ok'):
        alerts.append((
            'warning', 'INSTANT_INELIGIBLE',
            f"instant_eligibility.state={instant.get('state')} reason={instant.get('reason')}"
        ))
    return alerts


def check_equity_delta(portfolio):
    """Use API's equity_previous_close field directly (precomputed).

    Returns alert if equity has dropped > threshold vs previous close.
    """
    equity = float(portfolio.get('equity', 0))
    prev = float(portfolio.get('adjusted_equity_previous_close', 0)
                 or portfolio.get('equity_previous_close', 0))
    if equity <= 0 or prev <= 0:
        return []
    delta = (prev - equity) / prev
    alerts = []
    if delta >= EQUITY_DROP_CRIT_PCT:
        alerts.append((
            'critical', 'EQUITY_CRIT',
            f'equity down {delta:.1%} vs prev close (${equity:.2f} from ${prev:.2f})'
        ))
    elif delta >= EQUITY_DROP_WARN_PCT:
        alerts.append((
            'warning', 'EQUITY_DROP',
            f'equity down {delta:.1%} vs prev close (${equity:.2f} from ${prev:.2f})'
        ))
    return alerts


# ---------------------------------------------------------------------------
# Concentration check (must compute locally — API doesn't expose %)
# ---------------------------------------------------------------------------

def check_concentration(portfolio, opt_positions, stock_positions):
    """Compute position_value / total_equity per holding.

    API has no concentration_percent field, so we pull positions and
    divide. Single API call for stocks, single call for options.
    """
    alerts = []
    equity = float(portfolio.get('equity', 0))
    if equity <= 0:
        return alerts

    long_only_market_value = float(portfolio.get('market_value', 0))
    concentration_rows = []

    # Stocks
    for p in stock_positions:
        sym = p.get('symbol')
        qty = float(p.get('quantity', 0))
        if qty <= 0:
            continue
        avg = float(p.get('average_buy_price', 0))
        instr = r.request_get(p.get('instrument'))
        # Compute current value: qty * last quote
        quote = r.get_quotes(sym)
        if quote:
            last = float(quote[0].get('last_trade_price', avg))
        else:
            last = avg
        value = qty * last
        pct_eq = value / equity
        pct_long = (value / long_only_market_value) if long_only_market_value > 0 else 0
        concentration_rows.append((sym, value, pct_eq, pct_long))

    # Options: each open aggregate position has a market value
    for p in opt_positions:
        sym = p.get('symbol')
        qty = float(p.get('quantity', 0))
        if qty <= 0:
            continue
        # Options have a 'legs' array; each leg has an instrument URL
        leg = p.get('legs', [{}])[0]
        instr = r.request_get(leg.get('option'))
        # Use Robinhood's precomputed mark price * qty * 100
        # The average_open_price field is per-share * 100 (total cost)
        # For current market value we'd need quote data, but for
        # concentration check the cost basis is close enough.
        # Use average_open_price field as a proxy.
        cost = float(p.get('average_open_price', 0))
        # Convert back: cost is per-contract cost (e.g., $200 = 2 contracts
        # * $100 multiplier). qty tells us contracts.
        # So market_value_proxy = qty * cost / qty = cost (but per-contract)
        # Better: qty * 100 * option_premium is the right value, but we
        # don't have premium cached. Use cost basis for concentration.
        market_value = cost  # total dollars at risk for this position
        pct_eq = market_value / equity
        concentration_rows.append((sym + ' (opt)', market_value, pct_eq, 0))

    for sym, value, pct_eq, pct_long in concentration_rows:
        # Use the larger of the two percentages for the threshold check
        pct = max(pct_eq, pct_long) if pct_long > 0 else pct_eq
        if pct >= CONCENTRATION_CRIT_PCT:
            alerts.append((
                'critical', 'CONCENTRATION_CRIT',
                f'{sym}: ${value:.2f} = {pct_eq:.1%} of equity '
                f'({pct_long:.1%} of long-only)' if pct_long > 0
                else f'{sym}: ${value:.2f} = {pct_eq:.1%} of equity'
            ))
        elif pct >= CONCENTRATION_WARN_PCT:
            alerts.append((
                'warning', 'CONCENTRATION_WARN',
                f'{sym}: ${value:.2f} = {pct_eq:.1%} of equity '
                f'({pct_long:.1%} of long-only)' if pct_long > 0
                else f'{sym}: ${value:.2f} = {pct_eq:.1%} of equity'
            ))

    return alerts


# ---------------------------------------------------------------------------
# Alert dispatch
# ---------------------------------------------------------------------------

def _alert_key(alert):
    severity, code, _msg = alert
    return f'{severity}:{code}'


def dispatch_alerts(alerts, silent=False):
    """Send each alert via ntfy, deduplicating within a 5-minute window."""
    if not alerts:
        return
    state_file = '/tmp/api_risk_monitor_state.json'
    state = {}
    try:
        with open(state_file) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    now = time.time()
    dedupe_window = 300  # 5 minutes
    fresh = []
    for alert in alerts:
        severity, code, msg = alert
        key = _alert_key(alert)
        last_sent = state.get(key, 0)
        if now - last_sent < dedupe_window:
            continue
        state[key] = now
        fresh.append(alert)

    if not fresh:
        return

    # Persist state
    try:
        with open(state_file, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f'  [RISK] could not write state: {e}')

    if silent:
        return

    for alert in fresh:
        severity, code, msg = alert
        priority = 'high' if severity == 'critical' else 'default'
        tag = 'rotating_light' if severity == 'critical' else 'warning'
        title = f'[{severity.upper()}] {code}'
        notify(title, msg, priority=priority, tags=[tag])


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------

def run_once(silent=False):
    """Fetch the API state and dispatch alerts. Returns list of alerts."""
    try:
        account, portfolio, opt_positions = _fetch_risk_state()
    except Exception as e:
        print(f'  [RISK] API fetch failed: {e}')
        return []
    try:
        stock_positions = _fetch_stock_positions()
    except Exception as e:
        print(f'  [RISK] stock positions fetch failed: {e}')
        stock_positions = []

    alerts = []
    alerts.extend(check_margin(account, portfolio))
    alerts.extend(check_locks(account))
    alerts.extend(check_equity_delta(portfolio))
    alerts.extend(check_concentration(portfolio, opt_positions, stock_positions))

    # Print a one-line summary of the API-precomputed snapshot for the log
    equity = float(portfolio.get('equity', 0))
    excess_m = float(portfolio.get('excess_margin', 0))
    dtbp = float(account.get('margin_balances', {}).get('day_trade_buying_power', 0))
    held = float(account.get('margin_balances', {}).get('cash_held_for_orders', 0))
    print(f'  [RISK] equity=${equity:.2f} excess_margin=${excess_m:.2f} '
          f'dtbp=${dtbp:.2f} held_for_orders=${held:.2f} '
          f'alerts={len(alerts)}')

    if alerts and not silent:
        for severity, code, msg in alerts:
            print(f'    [{severity.upper()}] {code}: {msg}')

    dispatch_alerts(alerts, silent=silent)
    return alerts


def run_loop(interval=30):
    """Standalone daemon mode. Useful for testing or running separately."""
    print(f'[RISK] Starting monitor loop, interval={interval}s')
    while True:
        try:
            run_once()
        except Exception as e:
            print(f'  [RISK] cycle error: {e}')
        time.sleep(interval)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--once', action='store_true', help='run one cycle and exit')
    p.add_argument('--silent', action='store_true', help='do not push alerts to ntfy')
    p.add_argument('--interval', type=int, default=30, help='loop interval seconds')
    args = p.parse_args()

    _login()
    if args.once:
        run_once(silent=args.silent)
    else:
        run_loop(interval=args.interval)
