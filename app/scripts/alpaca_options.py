"""
alpaca_options.py — Alpaca options feed replacement for Robinhood chains.
Created 2026-09-18 during RH login lockout. Paid Alpaca plan gives full
option snapshots (bid/ask/size + greeks) for every contract.

Contract symbols look like: SPY260918C00300000 (SPY, 2026-09-18, Call, $300)
"""
import os
import re
import requests
from datetime import datetime, timedelta

BASE = "https://data.alpaca.markets/v1beta1/options"
KEY = None
SECRET = None
_etag_cache = {}


def _init():
    global KEY, SECRET
    if KEY is None:
        # keys live in ~/.tokens/alpaca.env (mounted from host ./tokens/)
        env_path = os.path.expanduser("~/.tokens/alpaca.env")
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k, v)
        KEY = os.environ["APCA_API_KEY_ID"]
        SECRET = os.environ["APCA_API_SECRET_KEY"]
    return KEY, SECRET


def _headers():
    k, s = _init()
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def _all_snapshots(symbol):
    """Page through all option snapshots for a symbol."""
    out = {}
    token = None
    while True:
        url = f"{BASE}/snapshots/{symbol}?limit=500"
        if token:
            url += f"&next_page_token={token}"
        r = requests.get(url, headers=_headers(), timeout=15)
        if r.status_code != 200:
            return out
        d = r.json()
        out.update(d.get("snapshots", {}))
        token = d.get("next_page_token")
        if not token:
            break
    return out


_CONTRACT_RE = re.compile(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$")


def _parse_contract(sym):
    """SPY260918C00300000 -> dict(symbol, expiry, type, strike)"""
    m = _CONTRACT_RE.match(sym)
    if not m:
        return None
    und, ymd, cp, strike = m.groups()
    expiry = f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}"
    return {"symbol": und, "expiry": expiry, "type": "call" if cp == "C" else "put",
            "strike": int(strike) / 1000.0, "contract_id": sym}


def get_chains(symbol):
    """Drop-in replacement for r.get_chains() — returns dict with expiration_dates."""
    snaps = _all_snapshots(symbol)
    expiries = set()
    for cid in snaps:
        p = _parse_contract(cid)
        if p:
            expiries.add(p["expiry"])
    return {"expiration_dates": sorted(expiries), "source": "alpaca"}


def find_options_by_expiration(symbol, expirationDate=None, optionType=None, info=None):
    """Drop-in replacement for r.find_options_by_expiration(). Returns list of
    dicts with strike_price, bid_price, ask_price, and metadata."""
    snaps = _all_snapshots(symbol)
    out = []
    for cid, snap in snaps.items():
        p = _parse_contract(cid)
        if not p:
            continue
        if expirationDate and p["expiry"] != expirationDate:
            continue
        if optionType and p["type"] != optionType:
            continue
        q = snap.get("latestQuote", {})
        out.append({
            "id": cid,
            "chain_symbol": symbol,
            "strike_price": p["strike"],
            "expiration_date": p["expiry"],
            "type": p["type"],
            "bid_price": q.get("bp"),
            "ask_price": q.get("ap"),
            "bid_size": q.get("bs"),
            "ask_size": q.get("as"),
            "volume": (snap.get("dailyBar") or {}).get("v"),
            "open_interest": snap.get("open_interest"),
            "source": "alpaca",
        })
    return out


def get_option_quote(symbol, expiry, strike, opt_type):
    """Real-time bid/ask for one contract. Returns dict or None."""
    exp = expiry.replace("-", "")[2:]  # 2026-09-18 -> 260918
    oc = "C" if opt_type == "call" else "P"
    strike_str = str(int(round(strike * 1000))).zfill(8)
    cid = f"{symbol}{exp}{oc}{strike_str}"
    r = requests.get(f"{BASE}/snapshots/{symbol}?limit=500", headers=_headers(), timeout=15)
    if r.status_code != 200:
        return None
    snap = r.json().get("snapshots", {}).get(cid)
    if not snap:
        return None
    q = snap.get("latestQuote", {})
    return {"bid": q.get("bp"), "ask": q.get("ap"), "bid_size": q.get("bs"),
            "ask_size": q.get("as"), "ts": q.get("t"), "source": "alpaca"}
