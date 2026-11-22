"""Edition timing: which slot we are in, the news window, and date labels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone, tzinfo

from .i18n import DAYS, MONTHS, t

SLOTS = ("premarket", "midday", "close", "weekend")


class _USEastern(tzinfo):
    """America/New_York without the tz database (fallback for minimal runtimes).

    DST rules since 2007: from the second Sunday of March 02:00 local time to the
    first Sunday of November 02:00 local time.
    """

    def _bounds_utc(self, year: int) -> tuple[datetime, datetime]:
        march = datetime(year, 3, 8)
        start = march + timedelta(days=(6 - march.weekday()) % 7)  # second Sunday
        nov = datetime(year, 11, 1)
        end = nov + timedelta(days=(6 - nov.weekday()) % 7)  # first Sunday
        return start + timedelta(hours=7), end + timedelta(hours=6)  # 02:00 local in UTC

    def _is_dst_utc(self, naive_utc: datetime) -> bool:
        start, end = self._bounds_utc(naive_utc.year)
        return start <= naive_utc < end

    def utcoffset(self, dt):
        if dt is None:
            return timedelta(hours=-5)
        naive = dt.replace(tzinfo=None)
        # Approximate for local wall times: compare against local boundaries.
        start, end = self._bounds_utc(naive.year)
        start_local, end_local = start - timedelta(hours=5), end - timedelta(hours=4)
        return timedelta(hours=-4) if start_local <= naive < end_local else timedelta(hours=-5)

    def dst(self, dt):
        return self.utcoffset(dt) - timedelta(hours=-5)

    def tzname(self, dt):
        return "EDT" if self.dst(dt) else "EST"

    def fromutc(self, dt):
        naive = dt.replace(tzinfo=None)
        offset = timedelta(hours=-4) if self._is_dst_utc(naive) else timedelta(hours=-5)
        return (naive + offset).replace(tzinfo=self)


def get_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError on runtimes without tzdata
        if name in ("America/New_York", "US/Eastern"):
            return _USEastern()
        return timezone.utc


def pick_slot(local: datetime) -> str:
    if local.weekday() >= 5:
        return "weekend"
    minutes = local.hour * 60 + local.minute
    if minutes < 10 * 60:
        return "premarket"
    if minutes < 16 * 60:
        return "midday"
    return "close"


@dataclass(frozen=True)
class EditionContext:
    now_utc: datetime
    tz: tzinfo
    tz_label: str
    slot: str
    window_start_utc: datetime
    context_start_utc: datetime
    language: str

    @property
    def now_local(self) -> datetime:
        return self.now_utc.astimezone(self.tz)

    @property
    def date(self) -> str:
        return self.now_local.strftime("%Y-%m-%d")

    @property
    def edition_id(self) -> str:
        return f"{self.date}-{self.slot}"

    def local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    def slot_label(self) -> str:
        return t(self.language, f"slot.{self.slot}")

    def long_date(self) -> str:
        d = self.now_local
        day = DAYS[self.language][d.weekday()]
        month = MONTHS[self.language][d.month - 1]
        if self.language == "es":
            return f"{day} {d.day} de {month} de {d.year}".upper()
        return f"{day}, {month} {d.day}, {d.year}".upper()

    def short_date(self, dt: datetime | None = None) -> str:
        d = self.local(dt) if dt else self.now_local
        day = DAYS[self.language][d.weekday()][:3]
        month = MONTHS[self.language][d.month - 1][:3]
        if self.language == "es":
            return f"{day} {d.day} {month}"
        return f"{day} {month} {d.day}"

    def time_label(self, dt: datetime | None = None) -> str:
        d = self.local(dt) if dt else self.now_local
        if self.language == "es":
            return f"{d.hour}:{d.minute:02d} {self.tz_label}"
        hour = d.hour % 12 or 12
        return f"{hour}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'} {self.tz_label}"

    def stamp_label(self, dt: datetime) -> str:
        """'Nov 21, 2:05 PM ET' / '21 nov, 14:05 ET'."""
        d = self.local(dt)
        month = MONTHS[self.language][d.month - 1][:3]
        if self.language == "es":
            return f"{d.day} {month}, {self.time_label(dt)}"
        return f"{month} {d.day}, {self.time_label(dt)}"

    def date_label(self, day: Date) -> str:
        weekday = DAYS[self.language][day.weekday()][:3]
        month = MONTHS[self.language][day.month - 1][:3]
        if self.language == "es":
            return f"{weekday} {day.day} {month}"
        return f"{weekday} {month} {day.day}"


def build_context(
    now_utc: datetime | None = None,
    tz_name: str = "America/New_York",
    tz_label: str = "ET",
    slot: str | None = None,
    language: str = "en",
    window_hours: int | None = None,
    context_days: int = 7,
) -> EditionContext:
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    tz = get_tz(tz_name)
    local = now_utc.astimezone(tz)
    slot = slot if slot in SLOTS else pick_slot(local)
    if window_hours is None:
        if slot == "weekend":
            window_hours = 48
        elif local.weekday() == 0 and slot == "premarket":
            window_hours = 72  # Monday morning covers the weekend
        else:
            window_hours = 24
    return EditionContext(
        now_utc=now_utc,
        tz=tz,
        tz_label=tz_label,
        slot=slot,
        window_start_utc=now_utc - timedelta(hours=window_hours),
        context_start_utc=now_utc - timedelta(days=context_days),
        language=language if language in ("en", "es") else "en",
    )
