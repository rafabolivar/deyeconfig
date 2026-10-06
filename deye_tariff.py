"""
Electricity tariff periods ([tariff] section of config.toml): which period
(off-peak, mid or peak) applies at a given time, taking weekends and
holidays into account.

Default: Spanish 2.0TD tariff (PVPC / regulated tariff):
  weekdays   00-08 off-peak (valle) | 08-10 mid (llano) | 10-14 peak (punta)
             14-18 mid | 18-22 peak | 22-24 mid
  weekends and national holidays: off-peak all day
"""

import sys
from datetime import date, datetime, timedelta
from datetime import time as dtime

PERIOD_NAMES = ("off-peak", "mid", "peak")

DEFAULT_PERIODS = [["00:00", "off-peak"], ["08:00", "mid"], ["10:00", "peak"],
                   ["14:00", "mid"], ["18:00", "peak"], ["22:00", "mid"]]
# Spanish national holidays with a fixed date (the ones that count as off-peak in 2.0TD)
DEFAULT_HOLIDAYS = ["01-01", "01-06", "05-01", "08-15", "10-12", "11-01", "12-06", "12-08", "12-25"]


class Tariff:
    def __init__(self, cfg: dict, tz, max_periods: int = 6):
        self.tz = tz
        errors = []
        self.periods: list[tuple[dtime, str]] = []
        for item in cfg.get("periods", DEFAULT_PERIODS):
            try:
                start, name = item
                h, m = (int(x) for x in start.split(":"))
                if name not in PERIOD_NAMES:
                    raise ValueError
                self.periods.append((dtime(h, m), name))
            except (ValueError, TypeError, AttributeError):
                errors.append(f"[tariff] invalid period {item!r}: use [\"HH:MM\", \"off-peak\" | \"mid\" | \"peak\"]")
        if self.periods:
            if self.periods[0][0] != dtime(0, 0):
                errors.append("[tariff] the first period must start at 00:00")
            if any(a[0] >= b[0] for a, b in zip(self.periods, self.periods[1:])):
                errors.append("[tariff] period start times must be increasing")
            if len(self.periods) > max_periods:
                errors.append(f"[tariff] at most {max_periods} periods per day (one per Time Of Use slot)")
        else:
            errors.append("[tariff] periods cannot be empty")
        self.weekend_off_peak = cfg.get("weekend_off_peak", True)
        self.holidays_md, self.holidays_full = set(), set()
        for h in cfg.get("holidays", DEFAULT_HOLIDAYS):
            try:
                if len(h) == 5:
                    datetime.strptime(f"2000-{h}", "%Y-%m-%d")
                    self.holidays_md.add(h)
                else:
                    datetime.strptime(h, "%Y-%m-%d")
                    self.holidays_full.add(h)
            except (ValueError, TypeError):
                errors.append(f"[tariff] invalid holiday {h!r}: use \"MM-DD\" (every year) or \"YYYY-MM-DD\"")
        if errors:
            sys.exit("ERROR in configuration:\n  - " + "\n  - ".join(errors))

    def is_off_peak_day(self, d: date) -> bool:
        return ((self.weekend_off_peak and d.weekday() >= 5)
                or f"{d:%m-%d}" in self.holidays_md or d.isoformat() in self.holidays_full)

    def day_periods(self, d: date) -> list[tuple[dtime, str]]:
        return [(dtime(0, 0), "off-peak")] if self.is_off_peak_day(d) else self.periods

    def period_at(self, t: datetime) -> tuple[str, datetime, datetime]:
        """(period name, start, end) of the period containing t."""
        d = t.date()
        periods = self.day_periods(d)
        for i, (start, name) in enumerate(periods):
            begin = datetime.combine(d, start, self.tz)
            end = (datetime.combine(d, periods[i + 1][0], self.tz) if i + 1 < len(periods)
                   else datetime.combine(d + timedelta(days=1), dtime(0, 0), self.tz))
            if begin <= t < end:
                return name, begin, end
        raise ValueError(f"no tariff period for {t}")

    def iter_periods(self, t: datetime, until: datetime):
        """Yield (name, start, end) from the period containing t while start < until."""
        name, start, end = self.period_at(t)
        while start < until:
            yield name, start, end
            name, start, end = self.period_at(end)

    def next_start_of(self, period: str, after: datetime, limit: datetime) -> datetime | None:
        """Start of the first 'period' that begins after 'after' and before 'limit'."""
        for name, start, _ in self.iter_periods(after, limit):
            if name == period and start > after:
                return start
        return None

    def slot_layout(self, d: date, slots: int) -> list[tuple[str, str]]:
        """'slots' (start "HH:MM", period name) pairs for the Time Of Use table on day d.
        Slot times always follow the weekday periods (padded if there are fewer than 'slots');
        on off-peak days every slot is off-peak."""
        times = [p[0] for p in self.periods]
        names = [p[1] for p in self.periods]
        while len(times) < slots:  # split the longest period in two
            ends = [datetime.combine(date.today(), t) for t in times[1:]] + \
                   [datetime.combine(date.today() + timedelta(days=1), dtime(0, 0))]
            lengths = [e - datetime.combine(date.today(), t) for t, e in zip(times, ends)]
            i = lengths.index(max(lengths))
            middle = datetime.combine(date.today(), times[i]) + lengths[i] / 2
            middle = middle.replace(minute=0 if middle.minute < 30 else 30, second=0)
            times.insert(i + 1, middle.time())
            names.insert(i + 1, names[i])
        if self.is_off_peak_day(d):
            names = ["off-peak"] * slots
        return [(f"{t:%H:%M}", n) for t, n in zip(times, names)]

