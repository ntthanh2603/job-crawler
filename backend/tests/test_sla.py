"""Phân vị từ histogram của số đo poll (modules/crawl/sla_service.py)."""

from __future__ import annotations

from jobcrawler.modules.crawl import sla_service


def test_bucket_edges() -> None:
    assert sla_service._bucket(0.2, sla_service.DURATION_EDGES) == 0
    assert sla_service._bucket(1, sla_service.DURATION_EDGES) == 1
    assert sla_service._bucket(10_000, sla_service.DURATION_EDGES) == len(
        sla_service.DURATION_EDGES
    )


def test_percentile_is_upper_edge_of_bucket() -> None:
    edges = (1, 2, 5)
    assert sla_service.percentile([0, 0, 0, 0], edges, 0.5) is None
    assert sla_service.percentile([50, 30, 20, 0], edges, 0.5) == 1
    assert sla_service.percentile([50, 30, 20, 0], edges, 0.95) == 5
    assert sla_service.percentile([0, 0, 0, 10], edges, 0.95) == 10  # quá ngưỡng lớn nhất


def test_window_p95_under_target_for_regular_polls() -> None:
    windows = [0] * (len(sla_service.WINDOW_EDGES) + 1)
    for gap in [152.0] * 90 + [170.0] * 8 + [200.0] * 2:
        windows[sla_service._bucket(gap, sla_service.WINDOW_EDGES)] += 1
    assert sla_service.percentile(windows, sla_service.WINDOW_EDGES, 0.95) == 180
