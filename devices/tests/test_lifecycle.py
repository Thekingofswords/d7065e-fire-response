"""Lifecycle helper (lifecycle.py) without a broker."""
import os

import lifecycle


class FakeBus:
    def __init__(self):
        self.published = []
        self.status_payload = None

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def start(self):
        pass

    def stop(self):
        self.published.append(("stopped", None, False))


def test_states_are_published_retained(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "HEARTBEAT_FILE", str(tmp_path / "hb"))
    monkeypatch.setattr(lifecycle, "WATCHDOG_SECONDS", 0)
    bus = FakeBus()
    life = lifecycle.Lifecycle("svc", status_topic="building/services/svc/status", bus=bus)
    life.start()
    life.running("ok")
    life.degraded("BuildSim unreachable")
    life.ok()
    statuses = [p["status"] for t, p, retain in bus.published
                if t == "building/services/svc/status" and retain]
    assert statuses[-3:] == ["online", "degraded", "online"]
    assert bus.status_payload()["state"] == lifecycle.RUNNING
    assert os.path.exists(tmp_path / "hb")
    events = [p for t, p, _ in bus.published if t == "building/events"]
    assert any(e["state"] == "degraded" and e["severity"] == "warning" for e in events)
    life.stop()
    assert life.state == lifecycle.STOPPED
    assert any(t == "stopped" for t, _, _ in bus.published)


def test_sleep_wakes_up_when_stopping(monkeypatch):
    import time
    life = lifecycle.Lifecycle("svc", bus=FakeBus())
    life.stopping = True
    started = time.monotonic()
    life.sleep(10)
    assert time.monotonic() - started < 0.5
