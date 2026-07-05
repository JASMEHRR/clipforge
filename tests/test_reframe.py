import random

from reframe import (clamp_velocity, crop_geometry, enforce_smoothness,
                     follow_path, path_metrics, smooth_path)


def test_smooth_path_flattens_noise():
    random.seed(1)
    raw = [100 + random.uniform(-30, 30) for _ in range(200)]
    smoothed = smooth_path(raw, ema_alpha=0.15, lookahead=12)
    assert path_metrics(smoothed)["max_velocity"] < path_metrics(raw)["max_velocity"]


def test_smooth_path_lookahead_anticipates():
    raw = [0.0] * 50 + [100.0] * 50
    smoothed = smooth_path(raw, ema_alpha=0.3, lookahead=10)
    # future frames influence the current crop: movement starts BEFORE the step
    assert smoothed[45] > 0.5


def test_clamp_velocity():
    path = [0, 100, 0, 100]
    out = clamp_velocity(path, 10)
    assert path_metrics(out)["max_velocity"] <= 10 + 1e-9


def test_follow_path_guarantees_both_constraints():
    random.seed(7)
    targets = [random.uniform(0, 320) for _ in range(600)]
    out = follow_path(targets, max_v=14.0, max_a=4.0)
    m = path_metrics(out)
    assert m["max_velocity"] <= 14.0 + 1e-6
    assert m["max_accel"] <= 4.0 * 2 + 1e-6  # accel bound on velocity delta
    # the real check: velocity deltas
    vels = [b - a for a, b in zip(out, out[1:])]
    dvs = [abs(b - a) for a, b in zip(vels, vels[1:])]
    assert max(dvs) <= 4.0 + 1e-6


def test_follow_path_converges():
    out = follow_path([0.0] + [200.0] * 300, max_v=14, max_a=4)
    assert abs(out[-1] - 200.0) < 2.0


def test_enforce_smoothness_scaled(cfg):
    rcfg = cfg["reframe"]
    random.seed(3)
    scale = 8.0  # e.g. 1080 output / 135 crop width
    raw = [random.uniform(60, 260) for _ in range(300)]
    path, m, ok = enforce_smoothness(raw, rcfg, scale)
    assert ok, m
    assert m["max_velocity"] <= rcfg["max_center_velocity_px"] + 1e-3
    assert m["max_accel"] <= rcfg["max_center_accel_px"] + 1e-3


def test_path_metrics_short_paths():
    assert path_metrics([]) == {"max_velocity": 0.0, "max_accel": 0.0}
    assert path_metrics([1.0, 2.0]) == {"max_velocity": 0.0, "max_accel": 0.0}


def test_crop_geometry_916():
    cw, ch, y0 = crop_geometry(1920, 1080, "9:16")
    assert (cw, ch, y0) == (606, 1080, 0)


def test_crop_geometry_square():
    cw, ch, y0 = crop_geometry(1920, 1080, "1:1")
    assert (cw, ch) == (1080, 1080) and y0 == 0


def test_crop_geometry_narrow_source():
    cw, ch, y0 = crop_geometry(320, 240, "9:16")
    assert cw <= 320 and ch <= 240
