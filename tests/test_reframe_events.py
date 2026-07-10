"""viral_v2 module 3: reaction-aware reframe cuts (pure-path tests, no video)."""
from pipeline import _remap_to_output
from reframe import event_cut_bounds


def test_event_frame_becomes_boundary():
    bounds, accepted = event_cut_bounds(cut_frames=[], event_frames=[120],
                                        n_frames=300, hold_frames=45)
    assert bounds == [0, 120, 300]
    assert accepted == {120}


def test_hysteresis_suppresses_scene_cut_inside_hold():
    # scene cuts 15 frames after AND 15 frames before the event (< hold 45)
    # are both dropped — two resets in quick succession is the flap
    bounds, accepted = event_cut_bounds(cut_frames=[105, 135],
                                        event_frames=[120],
                                        n_frames=300, hold_frames=45)
    assert bounds == [0, 120, 300]
    # scene cuts more than a hold away on either side survive
    bounds, _ = event_cut_bounds(cut_frames=[60, 200], event_frames=[120],
                                 n_frames=300, hold_frames=45)
    assert bounds == [0, 60, 120, 200, 300]


def test_hysteresis_between_events():
    # second event 30 frames after the first (< hold) is dropped
    bounds, accepted = event_cut_bounds(cut_frames=[], event_frames=[120, 150],
                                        n_frames=300, hold_frames=45)
    assert accepted == {120}
    assert bounds == [0, 120, 300]


def test_out_of_range_events_ignored():
    bounds, accepted = event_cut_bounds(cut_frames=[], event_frames=[0, 300, 500],
                                        n_frames=300, hold_frames=45)
    assert accepted == set()
    assert bounds == [0, 300]


def test_no_events_matches_scene_only_bounds():
    bounds, accepted = event_cut_bounds(cut_frames=[60, 200], event_frames=[],
                                        n_frames=300, hold_frames=45)
    assert bounds == [0, 60, 200, 300]
    assert accepted == set()


# ------------------------------------------------- source->output remapping

def test_remap_single_segment():
    assert _remap_to_output(15.0, [[10.0, 40.0]]) == 5.0
    assert _remap_to_output(5.0, [[10.0, 40.0]]) is None
    assert _remap_to_output(45.0, [[10.0, 40.0]]) is None


def test_remap_across_removed_gap():
    segments = [[10.0, 20.0], [25.0, 40.0]]     # 5s pause removed at 20-25
    assert _remap_to_output(30.0, segments) == 15.0   # 10 kept + 5 into seg 2
    assert _remap_to_output(22.0, segments) is None   # inside the removed gap
