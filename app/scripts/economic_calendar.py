"""
Economic Calendar Gate (per article: 'Federal Reserve meetings, inflation reports,
non-farm payroll data, etc.')

Fetches Forex Factory calendar (free) and flags high-impact events that occur
during market hours. Blocks trading when high-impact events are imminent.

Usage:
    from economic_calendar import check_economic_events
    ok, reason, adj = check_economic_events()
    if not ok:
        # don't enter trades
"""
import sys
import json
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, '/home/ralph/trader-v2')
sys.path.insert(0, '/home/ralph/trader-v2/scripts')
import config

log = logging.getLogger("v2.calendar")

# Cache file for events - persist across restarts
CACHE_PATH = Path("/home/ralph/trader-v2/cache/economic_calendar.json")
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Cache for 6 hours (forexfactory rate-limited)
CACHE_TTL_HOURS = 6

# High-impact keywords to prioritize
HIGH_IMPACT_KEYWORDS = [
    "FOMC", "Fed", "Federal Reserve", "Interest Rate",
    "CPI", "Inflation", "PPI",
    "Non-Farm", "NFP", "Unemployment",
    "GDP", "Gross Domestic Product",
    "PMI", "Manufacturing",
]


def _load_cache() -> dict:
    """Load cached calendar data."""
    if not CACHE_PATH.exists():
        return {"events": [], "fetched_at": None}
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Failed to load calendar cache: {e}")
        return {"events": [], "fetched_at": None}


def _save_cache(events: list):
    """Save calendar data to cache."""
    try:
        with open(CACHE_PATH, 'w') as f:
            json.dump({"events": events, "fetched_at": datetime.now().isoformat()}, f)
    except Exception as e:
        log.warning(f"Failed to save calendar cache: {e}")


def _filter_us_events(events: list) -> list:
    """Filter for USD events that matter for US stocks."""
    us_events = []
    for e in events:
        country = e.get("country", "")
        impact = e.get("impact", "")
        title = e.get("title", "").lower()

        # Only US events (USD) with medium/high impact
        if country != "USD":
            continue
        if impact not in ("Medium", "High"):
            continue

        # Skip low-value events
        skip_keywords = ["public holiday", "speaks", "speech", "auction", "bill auction"]
        if any(kw in title for kw in skip_keywords):
            if impact != "High":
                continue

        us_events.append(e)
    return us_events


def _fetch_calendar() -> list:
    """Fetch from Forex Factory with cache fallback."""
    cache = _load_cache()

    # Check if cache is fresh
    if cache.get("fetched_at"):
        try:
            fetched_at = datetime.fromisoformat(cache["fetched_at"])
            age_hours = (datetime.now() - fetched_at).total_seconds() / 3600
            if age_hours < CACHE_TTL_HOURS:
                log.debug(f"Using cached calendar ({age_hours:.1f}h old)")
                return cache["events"]
        except (ValueError, TypeError):
            pass

    # Fetch fresh
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
            timeout=10
        )
        if r.status_code == 200:
            events = r.json()
            _save_cache(events)
            log.info(f"Fetched {len(events)} calendar events")
            return events
        elif r.status_code == 429:
            # Rate limited - use stale cache
            log.warning(f"FF rate-limited, using stale cache ({len(cache['events'])} events)")
            return cache.get("events", [])
    except Exception as e:
        log.warning(f"Calendar fetch failed: {e}")

    # Final fallback - use stale cache
    return cache.get("events", [])


def check_economic_events() -> tuple[bool, str, int]:
    """
    Check if any high-impact US events are imminent (within 60 min).

    Returns:
      - ok: True if safe to trade
      - reason: human-readable explanation
      - adjustment: score adjustment (-20 to +5)
    """
    events = _fetch_calendar()
    us_events = _filter_us_events(events)

    if not us_events:
        return True, "no_upcoming_events", 0

    now = datetime.now()
    imminent = []  # within 60 min
    same_day = []  # today
    tomorrow = []  # next day

    for e in us_events:
        try:
            date_str = e.get("date", "")
            event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            event_time_local = event_time.replace(tzinfo=None)
            minutes_until = (event_time_local - now).total_seconds() / 60
            impact = e.get("impact", "")
            title = e.get("title", "")

            if -30 <= minutes_until <= 60:  # 30 min before to 60 min after
                imminent.append((title, impact, minutes_until))
            elif 0 <= minutes_until <= 24 * 60:  # today
                same_day.append((title, impact, minutes_until))
            elif 24 * 60 < minutes_until <= 48 * 60:  # tomorrow
                tomorrow.append((title, impact, minutes_until))
        except (ValueError, AttributeError, TypeError):
            continue

    # Hard block: imminent high-impact event
    high_imminent = [e for e in imminent if e[1] == "High"]
    if high_imminent:
        title, _, mins = high_imminent[0]
        return False, f"event_imminent:{title}_{int(mins)}min", -20

    # Soft block: high-impact event within 24 hours (warn)
    if same_day:
        high_today = [e for e in same_day if e[1] == "High"]
        if high_today:
            title, _, mins = high_today[0]
            return True, f"event_today:{title}_{int(mins)}min", -10

    # Tomorrow's high-impact event
    if tomorrow:
        high_tomorrow = [e for e in tomorrow if e[1] == "High"]
        if high_tomorrow:
            title, _, mins = high_tomorrow[0]
            return True, f"event_tomorrow:{title}_{int(mins/60):.0f}h", -5

    return True, "calendar_clear", 0


def get_today_events() -> list:
    """Helper for dashboard display - list today's events."""
    events = _fetch_calendar()
    us_events = _filter_us_events(events)
    now = datetime.now()
    today = []

    for e in us_events:
        try:
            event_time = datetime.fromisoformat(e.get("date", "").replace("Z", "+00:00"))
            if event_time.date() == now.date() or (event_time - now).days == 0:
                today.append({
                    "time": event_time.strftime("%H:%M"),
                    "title": e.get("title", ""),
                    "impact": e.get("impact", ""),
                    "country": e.get("country", ""),
                    "forecast": e.get("forecast", ""),
                    "previous": e.get("previous", ""),
                })
        except (ValueError, AttributeError, TypeError):
            continue

    return sorted(today, key=lambda x: x["time"])


if __name__ == "__main__":
    """Test the calendar check."""
    if not CACHE_PATH.exists():
        events = _fetch_calendar()
        print(f"Fetched {len(events)} events, {len(_filter_us_events(events))} US-relevant")

    print("\n=== Today's US events ===")
    for e in get_today_events():
        print(f"  {e['time']} [{e['impact']:6}] {e['title']:50} (prev: {e['previous']}, fcst: {e['forecast']})")

    print("\n=== Trade gate check ===")
    ok, reason, adj = check_economic_events()
    status = "✅ SAFE" if ok else "❌ BLOCK"
    print(f"  {status}  adj={adj:+3d}  {reason}")
