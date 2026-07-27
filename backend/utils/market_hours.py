"""
Market-hours gating for the Prices daemon.

The daemon polls FMP every few minutes; to keep API hits (and cost) down it
fetches each asset class only while that market is open. Crypto trades 24/7;
US equities follow the NYSE regular session; commodity futures follow the CME
Globex window. Sovereign yields are not gated here — the daemon refreshes them
once per day.

US Eastern time is derived with a small self-contained DST calculation: DST
(EDT, UTC-4) runs from the second Sunday of March to the first Sunday of
November; otherwise EST (UTC-5).

Two deliberate approximations, both harmless for trading windows but worth
knowing before anyone "fixes" them:

  • The DST decision keys off the **UTC** calendar date, not the Eastern one,
    so for roughly one hour on each transition day the offset is applied an
    hour early/late. No ``is_open`` answer changes (both transitions fall in
    the weekend/overnight gap when every gated market is shut), but
    ``eastern_now().date()`` can differ from true Eastern by a day inside that
    window, nudging the daemon's once-per-day refresh rollover. Swapping in
    ``zoneinfo.ZoneInfo("America/New_York")`` would change that rollover, so
    it is intentionally NOT used here.
  • Holidays are not modeled: on a market holiday FMP simply returns the prior
    close, costing a few redundant calls a year — acceptable.
"""

from datetime import datetime, timedelta, timezone, date

import backend.utils.constants as constants


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the date of the ``n``-th ``weekday`` (Mon=0..Sun=6) in a month."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _is_us_dst(d: date) -> bool:
    """True if ``d`` falls in US daylight saving time (EDT)."""
    dst_start = _nth_weekday(d.year, 3, 6, 2)   # 2nd Sunday of March
    dst_end = _nth_weekday(d.year, 11, 6, 1)    # 1st Sunday of November
    return dst_start <= d < dst_end


def eastern_now(now_utc: datetime) -> datetime:
    """Convert an aware UTC datetime to a naive US Eastern wall-clock datetime."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    offset = -4 if _is_us_dst(now_utc.date()) else -5
    return (now_utc + timedelta(hours=offset)).replace(tzinfo=None)


def _commodities_open(weekday: int, hour_dec: float) -> bool:
    """CME Globex window, approximated: Sun 18:00 ET → Fri 17:00 ET, with a
    daily 17:00–18:00 ET maintenance break Mon–Thu."""
    if weekday == 5:  # Saturday
        return False
    if weekday == 6:  # Sunday — opens at the evening Globex restart
        return hour_dec >= constants.GLOBEX_BREAK_END_ET
    if weekday == 4:  # Friday — closes at the afternoon break
        return hour_dec < constants.GLOBEX_BREAK_START_ET
    # Mon–Thu: open except the daily maintenance break
    return not (constants.GLOBEX_BREAK_START_ET <= hour_dec < constants.GLOBEX_BREAK_END_ET)


def is_open(asset_class: str, now_utc: datetime) -> bool:
    """Whether ``asset_class`` is actively trading at ``now_utc`` (aware UTC).

    crypto      → always
    stocks      → NYSE regular session, Mon–Fri 09:30–16:00 ET
    commodities → CME Globex window
    bonds       → not gated (refreshed daily); returns True
    """
    if asset_class == "crypto":
        return True
    if asset_class == "bonds":
        return True

    et = eastern_now(now_utc)
    weekday = et.weekday()  # Mon=0 .. Sun=6
    hour_dec = et.hour + et.minute / 60.0

    if asset_class == "stocks":
        return weekday <= 4 and constants.NYSE_OPEN_ET <= hour_dec < constants.NYSE_CLOSE_ET
    if asset_class == "commodities":
        return _commodities_open(weekday, hour_dec)

    # Unknown class: don't gate.
    return True
