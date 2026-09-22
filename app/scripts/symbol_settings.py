"""Per-symbol settings overrides (2026-09-16).

Ralph: the settings page governs GLOBALS; SYMBOL_SETTINGS (config.py) overrides
per symbol. Every key optional — missing key falls back to the global.

Keys (all optional per symbol):
  trading_hours   "HH:MM-HH:MM" ET — symbol may only OPEN trades inside the window
                  (exits still run 24h; EOD flatten still applies globally)
  buy_pct_offset  float — % below ask for the buy limit (overrides BUY_LIMIT_OFFSET_PCT)
  limit_offset    float — $ offset added to buy limit (overrides LIMIT_PRICE_OFFSET; rarely used)
  exit_pct        float — profit sell-off % over entry (overrides PROFIT_TARGET_PCT)
  max_loss_pct    float — max loss as fraction of premium (overrides LOSS_CAP_PCT)
  max_loss_dollar float — max loss $ per position (overrides LOSS_CAP_DOLLAR)
  take_profit     float — $ take profit (overrides TAKE_PROFIT_DOLLAR)
  max_profit      float — $ max profit cap (overrides MAX_PROFIT_DOLLAR)
  sell_off_pct    alias of exit_pct (both write the same knob; exit_pct wins if both set)
  hold_time_min   int — max minutes to hold (optional; global has no hold limit)

Usage:
    from symbol_settings import get(sym, 'max_loss_pct', config.LOSS_CAP_PCT)
"""

import os
import re

_TZ_RE = re.compile(r'^(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$')

# Alias map: user-facing name -> list of accepted keys (first match wins)
_ALIASES = {
    'buy_pct_offset':  ['buy_pct_offset'],
    'limit_offset':    ['limit_offset'],
    'exit_pct':        ['exit_pct', 'sell_off_pct'],
    'max_loss_pct':    ['max_loss_pct'],
    'max_loss_dollar': ['max_loss_dollar'],
    'take_profit':     ['take_profit'],
    'max_profit':      ['max_profit'],
    'hold_time_min':   ['hold_time_min'],
    'trading_hours':   ['trading_hours'],
}


def _raw(sym):
    """Raw override dict for a symbol (may be empty)."""
    table = getattr(__import__('config'), 'SYMBOL_SETTINGS', None) or {}
    return table.get((sym or '').upper(), {}) or {}


def get(sym, key, default):
    """Global fallback resolution: per-symbol -> default. Never raises."""
    try:
        ov = _raw(sym)
        for k in _ALIASES.get(key, [key]):
            if k in ov and ov[k] is not None:
                return ov[k]
    except Exception:
        pass
    return default


def has(sym, key):
    try:
        ov = _raw(sym)
        return any(k in ov and ov[k] is not None for k in _ALIASES.get(key, [key]))
    except Exception:
        return False


def in_trading_hours(sym, now_et_time=None):
    """True if symbol has no override window, or now (ET) is inside it.
    Supports windows crossing midnight (e.g. '22:00-01:00')."""
    window = get(sym, 'trading_hours', None)
    if not window:
        return True
    m = _TZ_RE.match(str(window).strip())
    if not m:
        return True  # malformed -> fail open
    def mins(s):
        h, mi = s.split(':')
        return int(h) * 60 + int(mi)
    a, b = mins(m.group(1)), mins(m.group(2))
    if now_et_time is None:
        from datetime import datetime, timezone, timedelta
        ET = timezone(timedelta(hours=-4))  # EDT; DST edge acceptable for a gate this coarse
        now_et_time = datetime.now(ET).time()
    cur = now_et_time.hour * 60 + now_et_time.minute
    if a <= b:
        return a <= cur < b
    return cur >= a or cur < b  # crosses midnight


def all_overrides():
    """Full table for the dashboard: {sym: {key: value}}
    2026-09-16: reads config.py FROM DISK every call — the dashboard process's
    cached config module goes stale with multiple writers (page tabs, daemon,
    hand edits) and merges from stale data, silently losing overrides.
    FIX 2026-09-17: _c.__file__ can be RELATIVE (config_loader exec_module),
    making dirname collapse to the wrong dir → FileNotFoundError → silent
    stale fallback. Resolve against the KNOWN app root instead."""
    candidates = [
        "/home/ralph/trader-v2/config.py",  # container bind-mount (dashboard + daemon)
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.py"),
    ]
    import runpy
    for cfg_file in candidates:
        try:
            if not os.path.isfile(cfg_file):
                continue
            fresh = runpy.run_path(cfg_file)
            table = fresh.get('SYMBOL_SETTINGS', None)
            if table is not None:
                return dict(table)
        except Exception:
            continue
    try:
        table = getattr(__import__('config'), 'SYMBOL_SETTINGS', None) or {}
        return dict(table)
    except Exception:
        return {}
