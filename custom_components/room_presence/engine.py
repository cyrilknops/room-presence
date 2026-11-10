"""Room decision for one BLE beacon. Pure Python, no Home Assistant imports.

The engine gets RSSI samples per scanner and a context (phone still or moving,
motion per room, Wi-Fi floor). It returns the room the beacon is in now.

Rules:
- Per scanner: the median of the samples of the last `window` seconds.
- Per room: the strongest scanner in that room.
- A new room must be stronger than the current room by a margin, for a dwell time.
  The margin and dwell time are larger when the phone is still and smaller when it moves.
- Fresh motion in the new room makes the switch easier; motion only in the
  current room makes it harder.
- When the Wi-Fi access point says another floor, a switch to that other floor is harder.
- When no scanner in the current room hears the beacon for `lost_after` seconds,
  the strongest room wins at once.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from statistics import median

NO_SIGNAL = -120.0
FLOOR_DBM = -110.0


@dataclass
class Settings:
    window: float = 4.0  # s of samples per scanner
    stale: float = 10.0  # s after which a scanner reading is ignored
    fade_after: float = 1.5  # s without a new sample before a reading starts to fade
    fade_rate: float = 4.0  # dB per s
    margin: float = 5.0  # dB lead needed for a normal switch
    dwell: float = 2.0  # s the lead must hold
    still_margin: float = 10.0
    still_dwell: float = 12.0
    moving_margin: float = 3.0
    moving_dwell: float = 1.0
    motion_bonus: float = 4.0  # dB
    motion_window: float = 20.0  # s that motion counts as fresh
    floor_penalty: float = 6.0  # dB
    lost_after: float = 5.0  # s without any sample in the current room
    away_after: float = 90.0


@dataclass
class Context:
    """What else is known at this moment."""

    still: bool = False  # phone screen off and not moving
    moving: bool = False  # walking, or screen just turned on
    motion_on: set[str] = field(default_factory=set)  # areas with motion now
    motion_recent: set[str] = field(default_factory=set)  # areas with motion in the motion window
    wifi_floor: str | None = None


@dataclass
class Decision:
    area: str | None
    best: str | None
    lead: float | None  # dB of the current room over the next room (or of best over current)
    reason: str
    areas: dict[str, float]
    changed: bool


class RoomEngine:
    def __init__(
        self,
        settings: Settings | None = None,
        scanner_area: dict[str, str] | None = None,
        area_floor: dict[str, str | None] | None = None,
        offsets: dict[str, float] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.scanner_area = scanner_area or {}
        self.area_floor = area_floor or {}
        self.offsets = offsets or {}
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._gaps: dict[str, deque[float]] = {}  # time between samples per scanner
        self.area: str | None = None
        self.last_seen: float | None = None
        self._area_fresh: float | None = None  # last sample time in the current area
        self._cand: str | None = None
        self._cand_since = 0.0
        self.switched_at: float | None = None

    # ------------------------------------------------------------ samples
    def add_sample(self, scanner: str, rssi: float, ts: float) -> None:
        q = self._samples.setdefault(scanner, deque(maxlen=64))
        if q and ts <= q[-1][0]:
            return
        if q:
            self._gaps.setdefault(scanner, deque(maxlen=12)).append(ts - q[-1][0])
        q.append((ts, float(rssi)))
        self.last_seen = ts if self.last_seen is None else max(self.last_seen, ts)

    def gap(self, scanner: str) -> float:
        """Typical time between two samples of one scanner (slow phones and busy proxies report less)."""
        g = self._gaps.get(scanner)
        return median(g) if g and len(g) >= 3 else 1.0

    def scanner_values(self, now: float) -> dict[str, float]:
        s = self.settings
        out: dict[str, float] = {}
        for scanner, q in self._samples.items():
            while q and now - q[0][0] > max(s.window, s.stale, 4 * self.gap(scanner)):
                q.popleft()
            gap = self.gap(scanner)
            if not q or now - q[-1][0] > max(s.stale, 4 * gap):
                continue
            recent = [r for t, r in q if now - t <= max(s.window, 2 * gap)] or [q[-1][1]]
            # A scanner that stops hearing the beacon (you walked away) fades out
            fade = max(0.0, now - q[-1][0] - max(s.fade_after, 1.5 * gap)) * s.fade_rate
            value = median(recent) + self.offsets.get(scanner, 0.0) - fade
            if value >= FLOOR_DBM:  # faded below what a scanner can hear: no reading
                out[scanner] = value
        return out

    def _lost_after(self, area: str | None) -> float:
        gaps = [self.gap(sc) for sc, a in self.scanner_area.items() if a == area and sc in self._samples]
        return max(self.settings.lost_after, 3 * min(gaps)) if gaps else self.settings.lost_after

    def _last_sample(self, area: str | None) -> float | None:
        times = [q[-1][0] for sc, q in self._samples.items() if q and self.scanner_area.get(sc) == area]
        return max(times) if times else None

    def area_values(self, now: float) -> dict[str, float]:
        areas: dict[str, float] = {}
        for scanner, value in self.scanner_values(now).items():
            area = self.scanner_area.get(scanner)
            if not area:
                continue
            if value > areas.get(area, NO_SIGNAL):
                areas[area] = value
        return areas

    # ------------------------------------------------------------ decision
    def _need(self, best: str, ctx: Context) -> tuple[float, float]:
        s = self.settings
        if ctx.moving:
            margin, dwell = s.moving_margin, s.moving_dwell
        elif ctx.still:
            margin, dwell = s.still_margin, s.still_dwell
        else:
            margin, dwell = s.margin, s.dwell
        if best in ctx.motion_recent:
            margin -= s.motion_bonus
            dwell = min(dwell, s.moving_dwell)
        if self.area in ctx.motion_on and best not in ctx.motion_recent:
            margin += s.motion_bonus
        if ctx.wifi_floor:
            fb = self.area_floor.get(best)
            fc = self.area_floor.get(self.area) if self.area else None
            if fb and fb != ctx.wifi_floor and fc == ctx.wifi_floor:
                margin += s.floor_penalty
        return max(margin, 0.0), max(dwell, 0.0)

    def _switch(self, area: str | None, now: float) -> None:
        self.area = area
        self._cand = None
        self.switched_at = now
        self._area_fresh = self._last_sample(area) if area else None

    def evaluate(self, now: float, ctx: Context | None = None) -> Decision:
        ctx = ctx or Context()
        s = self.settings
        areas = self.area_values(now)
        best = max(areas, key=areas.get) if areas else None
        before = self.area

        if not areas:
            if self.area and self.last_seen is not None and now - self.last_seen > s.away_after:
                self._switch(None, now)
                return Decision(None, None, None, "away", areas, before is not None)
            return Decision(self.area, None, None, "no signal", areas, False)

        if self.area is None:
            self._switch(best, now)
            return Decision(best, best, None, "first", areas, True)

        cur = areas.get(self.area)
        last = self._last_sample(self.area)
        if last is not None:
            self._area_fresh = last
        if self._area_fresh is None or now - self._area_fresh >= self._lost_after(self.area):
            # No scanner in the current room hears the beacon any more: the strongest other room wins
            others = {a: v for a, v in areas.items() if a != self.area}
            if others:
                new = max(others, key=others.get)
                self._switch(new, now)
                return Decision(new, best, None, "current room lost", areas, True)
            return Decision(self.area, best, None, "current room quiet", areas, False)
        if best == self.area:
            self._cand = None
            others = [v for a, v in areas.items() if a != best]
            lead = areas[best] - max(others) if others else None
            return Decision(self.area, best, lead, "stay", areas, False)
        if cur is None:
            return Decision(self.area, best, None, "current room quiet", areas, False)

        lead = areas[best] - cur
        margin, dwell = self._need(best, ctx)
        if lead < margin:
            self._cand = None
            return Decision(self.area, best, lead, f"lead {lead:.1f} < {margin:.1f} dB", areas, False)
        if self._cand != best:
            self._cand = best
            self._cand_since = now
        if now - self._cand_since >= dwell:
            reason = "motion" if best in ctx.motion_recent else "moving" if ctx.moving else "stronger"
            self._switch(best, now)
            return Decision(best, best, lead, reason, areas, True)
        return Decision(self.area, best, lead, f"wait {dwell - (now - self._cand_since):.1f} s", areas, False)
