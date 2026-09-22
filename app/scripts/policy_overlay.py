"""Overlay policy values on top of config.py at runtime.

The daemon reads strategy params from config.X. The adapter mutates
policy.json. This module makes the daemon use policy values when they
exist, falling back to config defaults.

Usage in daemon.py:
    from policy_overlay import get
    val = get('LOSS_CAP_DOLLAR_V2', config.LOSS_CAP_DOLLAR_V2)

Behavior:
- Frozen policy → returns the safest config value regardless of policy.json.
- Policy file missing → returns the config default.
- Policy file present → returns the policy value if it differs from default.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

POLICY_FILE = Path('/home/ralph/trader-v2/data/policy.json')

# Mapping from config.py attribute name → policy.json key.
_CONFIG_TO_POLICY = {
    'LOSS_CAP_DOLLAR_V2':  'loss_cap_dollar_v2',
    'TAKE_PROFIT_DOLLAR':  'take_profit_dollar',
    'MAX_PROFIT_DOLLAR':   'max_profit_dollar',
    'ML_SKIP_THRESHOLD':   'ml_skip_threshold',
    'SYMBOL_COOLDOWN_SEC': 'symbol_cooldown_sec',
    'MAX_OPEN_POSITIONS':  'max_open_positions',
}

# Safest values — used when policy is frozen
_SAFE_DEFAULTS = {
    'loss_cap_dollar_v2':  20.0,
    'take_profit_dollar':  10.0,
    'max_profit_dollar':   30.0,
    'ml_skip_threshold':   0.65,
    'symbol_cooldown_sec': 60,
    'max_open_positions':  1,
}

_policy_cache = None
_policy_mtime = None


def _load():
    """Read policy.json with simple cache."""
    global _policy_cache, _policy_mtime
    if not POLICY_FILE.exists():
        _policy_cache = None
        _policy_mtime = None
        return None
    try:
        mtime = POLICY_FILE.stat().st_mtime
    except OSError:
        return None
    if _policy_cache is None or mtime != _policy_mtime:
        try:
            with open(POLICY_FILE) as f:
                _policy_cache = json.load(f)
        except Exception:
            _policy_cache = None
        _policy_mtime = mtime
    return _policy_cache


def get(config_attr: str, default: Any) -> Any:
    """Get a config value with policy overlay.

    Args:
        config_attr: name of the config.py attribute (e.g. 'LOSS_CAP_DOLLAR_V2')
        default: the config value to use as the base

    Returns:
        The effective value (policy override if present, else default).
    """
    policy_key = _CONFIG_TO_POLICY.get(config_attr)
    if policy_key is None:
        return default

    policy = _load()
    if policy is None:
        return default

    # Frozen policy → use safer default (which is always within bounds)
    if policy.get('frozen'):
        return _SAFE_DEFAULTS.get(policy_key, default)

    val = policy.get(policy_key)
    if val is None:
        return default

    return val


def reload() -> None:
    """Force a re-read of policy.json on next get() call."""
    global _policy_cache, _policy_mtime
    _policy_cache = None
    _policy_mtime = None



PER_SYMBOL_FILE = Path('/home/ralph/trader-v2/data/per_symbol_tuning.json')

_per_symbol_cache = None
_per_symbol_mtime = None


def _load_per_symbol() -> dict:
    """Load per_symbol_tuning.json with cache."""
    global _per_symbol_cache, _per_symbol_mtime
    if not PER_SYMBOL_FILE.exists():
        _per_symbol_cache = None
        _per_symbol_mtime = None
        return {}
    try:
        mtime = PER_SYMBOL_FILE.stat().st_mtime
    except OSError:
        return {}
    if _per_symbol_cache is None or mtime != _per_symbol_mtime:
        try:
            with open(PER_SYMBOL_FILE) as f:
                data = json.load(f)
            _per_symbol_cache = data.get('per_symbol', {})
        except Exception:
            _per_symbol_cache = {}
        _per_symbol_mtime = mtime
    return _per_symbol_cache


def get_for_symbol(config_attr: str, default: Any, symbol: str) -> Any:
    """Get a config value with per-symbol overlay + policy overlay.

    Order of precedence (highest first):
    1. Per-symbol tuned value for this symbol
    2. Global policy value
    3. config.py default
    """
    if symbol:
        per_sym = _load_per_symbol()
        sym_tuning = per_sym.get(symbol, {})
        if sym_tuning.get('tuned'):
            mapping = {
                'LOSS_CAP_DOLLAR_V2':  'recommended_loss_cap_dollar',
                'TAKE_PROFIT_DOLLAR':  'recommended_take_profit_dollar',
                'ML_SKIP_THRESHOLD':   'recommended_ml_skip_threshold',
            }
            per_sym_key = mapping.get(config_attr)
            if per_sym_key and sym_tuning.get(per_sym_key) is not None:
                return sym_tuning[per_sym_key]
    return get(config_attr, default)


if __name__ == '__main__':
    # Self-test
    print('LOSS_CAP_DOLLAR_V2 default 30 →', get('LOSS_CAP_DOLLAR_V2', 30))
    print('TAKE_PROFIT_DOLLAR default 10 →', get('TAKE_PROFIT_DOLLAR', 10))
    print('MAX_PROFIT_DOLLAR default 50 →', get('MAX_PROFIT_DOLLAR', 50))
    print('ML_SKIP_THRESHOLD default 0.55 →', get('ML_SKIP_THRESHOLD', 0.55))