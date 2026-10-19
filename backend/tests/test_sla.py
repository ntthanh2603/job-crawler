"""Phân vị từ histogram của số đo poll (services/sla.py)."""

from __future__ import annotations

from jobcrawler.services import sla


def test_bucket_edges() -> None:
    assert sla._bucket(0.2, sla.DURATION_EDGES) == 0
    assert sla._bucket(1, sla.DURATION_EDGES) == 1
    assert sla._bucket(10_000, sla.DURATION_EDGES) == len(sla.DURATION_EDGES)


def test_percentile_is_upper_edge_of_bucket() -> None:
    edges = (1, 2, 5)
    assert sla.percentile([0, 0, 0, 0], edges, 0.5) is None
    assert sla.percentile([50, 30, 20, 0], edges, 0.5) == 1
    assert sla.percentile([50, 30, 20, 0], edges, 0.95) == 5
    assert sla.percentile([0, 0, 0, 10], edges, 0.95) == 10  # quá ngưỡng lớn nhất


def test_window_p95_under_target_for_regular_polls() -> None:
    windows = [0] * (len(sla.WINDOW_EDGES) + 1)
    for gap in [152.0] * 90 + [170.0] * 8 + [200.0] * 2:
        windows[sla._bucket(gap, sla.WINDOW_EDGES)] += 1
    assert sla.percentile(windows, sla.WINDOW_EDGES, 0.95) == 180
