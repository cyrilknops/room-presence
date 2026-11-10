"""Tests for the room decision (no Home Assistant needed)."""

from custom_components.room_presence.engine import Context, RoomEngine, Settings

MAP = {"A1": "living", "K1": "kitchen", "K2": "kitchen", "B1": "bedroom"}
FLOORS = {"living": "main", "kitchen": "main", "bedroom": "up"}


def feed(e, t, **rssi):
    for scanner, value in rssi.items():
        e.add_sample(scanner, value, t)


def run(e, t0, t1, ctx=None, step=0.5, **rssi):
    """Feed the same RSSI every step from t0 to t1 and evaluate each time."""
    d = None
    t = t0
    while t <= t1 + 1e-9:
        feed(e, t, **rssi)
        d = e.evaluate(t, ctx)
        t += step
    return d


def engine(**kw):
    return RoomEngine(Settings(**kw), MAP, FLOORS)


def test_first_reading_picks_strongest_room():
    e = engine()
    feed(e, 0, A1=-70, K1=-60)
    d = e.evaluate(0)
    assert d.area == "kitchen" and d.changed and d.reason == "first"


def test_strongest_scanner_counts_for_the_room():
    e = engine()
    feed(e, 0, K1=-90, K2=-55, A1=-60)
    assert e.area_values(0) == {"kitchen": -55, "living": -60}


def test_noise_does_not_switch():
    e = engine()
    run(e, 0, 3, A1=-60, K1=-70)
    assert e.area == "living"
    # Kitchen 3 dB stronger (below the 5 dB margin) for a long time: stay
    d = run(e, 3.5, 30, A1=-65, K1=-62)
    assert d.area == "living"


def test_clear_lead_switches_after_dwell():
    e = engine()
    run(e, 0, 5, A1=-60, K1=-75)
    # The median needs a moment to follow; then the 2 s dwell
    d = run(e, 5.5, 7, A1=-78, K1=-58)
    assert d.area == "living"
    d = run(e, 7.5, 12, A1=-78, K1=-58)
    assert d.area == "kitchen" and e.switched_at is not None


def test_still_phone_holds_longer():
    e = engine()
    still = Context(still=True)
    run(e, 0, 5, still, A1=-60, K1=-75)
    # 8 dB lead: enough normally, not when the phone is still (10 dB)
    d = run(e, 5.5, 40, still, A1=-68, K1=-60)
    assert d.area == "living"
    # 15 dB lead, but it must hold for 12 s
    run(e, 40.5, 50, still, A1=-75, K1=-60)
    assert e.area == "living"
    d = run(e, 50.5, 60, still, A1=-75, K1=-60)
    assert d.area == "kitchen"


def test_moving_switches_fast():
    e = engine()
    moving = Context(moving=True)
    run(e, 0, 5, moving, A1=-60, K1=-75)
    d = run(e, 5.5, 9, moving, A1=-66, K1=-61)
    assert d.area == "kitchen"
    assert e.switched_at <= 9


def test_motion_in_new_room_helps():
    e = engine()
    run(e, 0, 5, None, A1=-60, K1=-75)
    # 2 dB lead only: no switch without motion
    d = run(e, 5.5, 20, None, A1=-64, K1=-62)
    assert d.area == "living"
    # Motion in the kitchen: margin 5 - 4 = 1 dB, dwell 1 s
    d = run(e, 20.5, 23, Context(motion_on={"kitchen"}, motion_recent={"kitchen"}), A1=-64, K1=-62)
    assert d.area == "kitchen" and e.switched_at <= 21.5


def test_motion_only_in_current_room_holds():
    e = engine()
    run(e, 0, 5, None, A1=-60, K1=-75)
    ctx = Context(motion_on={"living"}, motion_recent={"living"})
    # 7 dB lead would switch, but motion in the living room asks 9 dB
    d = run(e, 5.5, 30, ctx, A1=-67, K1=-60)
    assert d.area == "living"


def test_other_floor_is_harder_with_wifi_floor():
    e = engine()
    ctx = Context(wifi_floor="main")
    run(e, 0, 5, ctx, A1=-60, B1=-80)
    # Bedroom (upstairs) 8 dB stronger: normal margin 5 + floor penalty 6 = 11 dB
    d = run(e, 5.5, 30, ctx, A1=-70, B1=-62)
    assert d.area == "living"
    d = run(e, 30.5, 40, Context(wifi_floor="up"), A1=-70, B1=-62)
    assert d.area == "bedroom"


def test_current_room_lost_switches():
    e = engine()
    run(e, 0, 5, None, A1=-60, K1=-80)
    # Living room scanner stops hearing the beacon; kitchen is weak but the only one
    d = run(e, 5.5, 8, None, K1=-82)
    assert d.area == "living"
    d = run(e, 8.5, 10.5, None, K1=-82)
    assert d.area == "kitchen"


def test_away_after_no_signal():
    e = engine(away_after=30)
    run(e, 0, 2, None, A1=-60)
    assert e.evaluate(20).area == "living"
    d = e.evaluate(40)
    assert d.area is None and d.reason == "away"


def test_scanner_offset():
    e = RoomEngine(Settings(), MAP, FLOORS, offsets={"K1": -10})
    feed(e, 0, A1=-65, K1=-60)
    assert e.evaluate(0).area == "living"


def test_old_samples_fade_and_expire():
    e = engine()
    feed(e, 0, A1=-60)
    feed(e, 5, K1=-70)
    # A1: 5 s old, 3.5 s past fade_after at 4 dB/s = -74
    assert e.area_values(5) == {"living": -74, "kitchen": -70}
    assert e.area_values(11) == {"kitchen": -88}


def test_leaving_room_fades_old_room():
    e = engine()
    run(e, 0, 5, None, A1=-55, K1=-80)
    # You walk to the kitchen: the living room scanner stops hearing you
    d = run(e, 5.5, 11, None, K1=-62)
    assert d.area == "kitchen" and e.switched_at <= 11


def test_slow_scanner_is_not_lost_too_early():
    e = engine()
    # The living room scanner hears the phone only every 4 s (low power advertising)
    t = 0.0
    while t <= 20:
        if t % 4 == 0:
            e.add_sample("A1", -88, t)
        e.add_sample("K1", -97, t) if t % 4 == 2 else None
        e.evaluate(t)
        t += 0.5
    assert e.area == "living"
    # 8 s quiet in the living room: still under 3 x 4 s
    d = None
    while t <= 30:
        if t % 4 == 2:
            e.add_sample("K1", -97, t)
        d = e.evaluate(t)
        t += 0.5
    assert d.area == "living"
