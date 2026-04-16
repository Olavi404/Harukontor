from app import worker


def test_heartbeat_interval_seconds_within_limit(monkeypatch):
    monkeypatch.setattr(worker.settings, "heartbeat_interval_seconds", 900)
    assert worker.heartbeat_interval_seconds() == 900


def test_heartbeat_interval_seconds_clamps_to_30_minutes(monkeypatch):
    monkeypatch.setattr(worker.settings, "heartbeat_interval_seconds", 7200)
    assert worker.heartbeat_interval_seconds() == 1800
