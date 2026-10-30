"""Panel display filters: timestamps as local time with a zone, limit windows named like the Brain gauges."""
import time

import pytest

from devmem.panel.app import when, window_label


@pytest.fixture
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_when_formats_iso_times_and_leaves_other_values_alone(utc):
    assert when("2026-10-30T06:36:37.525107+00:00") == "30 Oct 06:36 UTC"
    assert when("2026-09-22T08:11:18.5245477Z") == "22 Sep 08:11 UTC"  # ADO's seven fractional digits
    assert when("2026-10-30T10:50:00Z", True) == "30 Oct 10:50:00 UTC"
    assert when("2026-10-02") == "2 Oct 2026"
    assert when("unknown") == "unknown" and when(None) == "" and when("") == ""


def test_window_label_matches_the_gauges():
    assert window_label({"label": "five_hour", "window_minutes": 300}) == "5h"
    assert window_label({"label": "seven_day", "window_minutes": 10080}) == "weekly"
    assert window_label({"label": "seven_day_opus", "window_minutes": 10080}) == "weekly opus"
    assert window_label({"label": "primary", "window_minutes": 300}) == "5h"
    assert window_label({"label": "secondary", "window_minutes": 10080}) == "weekly"
    assert window_label({"label": "other", "window_minutes": 2880}) == "2d"
    assert window_label({"label": "spark:primary", "window_minutes": 300}) == "spark 5h"
