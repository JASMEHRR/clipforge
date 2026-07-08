"""ETA math for progress tracking (Feature 1, overnight run)."""
import progress


def test_ema_empty_and_positive():
    assert progress.ema([]) is None
    assert progress.ema([0, -1, None]) is None
    assert progress.ema([2.0]) == 2.0
    # newest value pulls the average toward it
    assert progress.ema([1.0, 3.0], alpha=0.5) == 2.0


def test_estimate_eta_live_and_history_blend():
    # only history: remaining / hist_rate
    assert progress.estimate_eta(60, live_rate=None, history_rates=[2.0]) == 30.0
    # only live rate
    assert progress.estimate_eta(60, live_rate=3.0, history_rates=[]) == 20.0
    # blended: live weighted higher than history
    eta = progress.estimate_eta(60, live_rate=2.0, history_rates=[1.0],
                                live_weight=0.6)
    blended_rate = 0.6 * 2.0 + 0.4 * 1.0  # 1.6
    assert abs(eta - 60 / blended_rate) < 1e-6


def test_estimate_eta_no_signal_and_done():
    assert progress.estimate_eta(60, live_rate=None, history_rates=None) is None
    assert progress.estimate_eta(0, live_rate=5.0) == 0.0


if __name__ == "__main__":
    test_ema_empty_and_positive()
    test_estimate_eta_live_and_history_blend()
    test_estimate_eta_no_signal_and_done()
    print("ok")
