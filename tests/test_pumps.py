from datetime import datetime, timedelta, timezone

from varbot.analysis import pumps


def _chart(closes, start=datetime(2025, 10, 28, tzinfo=timezone.utc), volumes=None):
    pts = []
    vols = []
    for i, c in enumerate(closes):
        ts = int((start + timedelta(days=i)).timestamp() * 1000)
        pts.append([ts, c])
        vols.append([ts, (volumes[i] if volumes else 1000.0)])
    return {"prices": pts, "total_volumes": vols}


def test_detects_two_pumps_and_ignores_slow_grind():
    closes = [1.0] * 30                                  # flat
    closes += [1.0 + 0.1 * k for k in range(1, 8)]        # +70% in 7 days -> pump 1 (peak 1.7)
    closes += [1.7 * (1 - 0.05 * k) for k in range(1, 9)]  # retrace 40% -> episode ends
    closes += [1.02] * 60                                 # flat
    closes += [1.02 + 0.3 * k for k in range(1, 5)]       # +118% in 4 days -> pump 2 (peak 2.22)
    closes += [2.22 * (1 - 0.1 * k) for k in range(1, 5)]  # retrace 40%
    closes += [1.4 + 0.004 * k for k in range(1, 200)]    # slow grind +57% over 200 days: not a pump
    stats = pumps.analyse(_chart(closes))
    assert stats.available
    assert stats.pumps_12m == 2
    e1, e2 = stats.episodes
    assert 65 < e1.gain_pct < 75 and e1.days_to_peak == 7
    assert e2.gain_pct > 100
    assert e1.retrace_pct >= 30 and e2.retrace_pct >= 30
    assert stats.max_gain_pct == e2.gain_pct
    assert stats.last_peak_date == e2.peak_date
    assert stats.current_vs_last_peak_pct is not None and stats.current_vs_last_peak_pct < 0
    assert stats.propensity is not None and stats.propensity >= 3


def test_no_pumps_on_flat_series():
    stats = pumps.analyse(_chart([1.0 + 0.001 * (i % 7) for i in range(365)]))
    assert stats.available and stats.pumps_12m == 0 and stats.propensity == 0


def test_volume_pickup_and_short_series():
    closes = [1.0] * 200
    vols = [1000.0] * 170 + [3000.0] * 30
    stats = pumps.analyse(_chart(closes, volumes=vols))
    assert stats.volume_30d_vs_180d and stats.volume_30d_vs_180d > 2.5
    assert not pumps.analyse(_chart([1.0] * 10)).available
