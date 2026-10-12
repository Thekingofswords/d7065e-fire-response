"""Process lifecycle shared by every service.

    Starting --connected--> Syncing --retained state in--> Running
    Running  --inputs stale / BuildSim unreachable--> Degraded --recovered--> Running
    any      --SIGTERM--> Stopping --> Stopped (exit 0)
    crash    --> MQTT last will: offline

* The state is published, retained, on the service's status topic:
  {"status": "online" | "degraded" | "offline", "state", "detail", "ts"}.
  Gateways use building/gateways/<name>/status, so the agent treats a
  degraded sensor gateway like an offline one.
* A heartbeat file (HEARTBEAT_FILE, default /tmp/heartbeat) is touched every
  loop; `python healthcheck.py` fails when it is older than
  HEALTH_MAX_AGE_SECONDS, so Docker restarts a hung process.
* A watchdog thread exits the process (code 70) when the main loop has not
  beaten for WATCHDOG_SECONDS, so the compose restart policy restarts a hung
  service (Docker itself does not restart unhealthy containers).
* SIGTERM (docker compose stop) and SIGINT stop the main loop cleanly: the
  service publishes "offline" itself instead of waiting for the last will.
"""
import os
import signal
import threading
import time

import mqtt_bus

HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat")
WATCHDOG_SECONDS = float(os.environ.get("WATCHDOG_SECONDS", "120"))   # 0 = off

STARTING, SYNCING, RUNNING, DEGRADED, STOPPING, STOPPED = (
    "starting", "syncing", "running", "degraded", "stopping", "stopped")


class Lifecycle:
    def __init__(self, name, status_topic=None, bus=None):
        self.name = name
        self.status_topic = status_topic
        self.bus = bus or mqtt_bus.Bus(name, status_topic=status_topic)
        self.bus.status_payload = self.payload
        self.state = STARTING
        self.detail = ""
        self.since = time.time()
        self.stopping = False
        self.last_beat = time.monotonic()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._on_signal)
            except ValueError:   # not the main thread (tests)
                pass

    # -- state ----------------------------------------------------------------

    def payload(self):
        status = {DEGRADED: "degraded", STOPPING: "offline", STOPPED: "offline"}.get(
            self.state, "online")
        return {"status": status, "state": self.state, "detail": self.detail,
                "since": self.since, "ts": time.time()}

    def set(self, state, detail=""):
        if state == self.state and detail == self.detail:
            return
        changed = state != self.state
        self.state, self.detail = state, detail
        if changed:
            self.since = time.time()
            print(f"{self.name}: {state.upper()}" + (f" ({detail})" if detail else ""), flush=True)
            self.bus.publish(mqtt_bus.EVENTS_TOPIC, mqtt_bus.event(
                "service_state", f"{self.name}: {state}" + (f" ({detail})" if detail else ""),
                severity="warning" if state == DEGRADED else "info",
                service=self.name, state=state), qos=1)
        if self.status_topic:
            self.bus.publish(self.status_topic, self.payload(), qos=1, retain=True)

    def start(self):
        self.set(STARTING)
        self.bus.start()
        self.set(SYNCING)
        self.beat()
        if WATCHDOG_SECONDS > 0:
            threading.Thread(target=self._watchdog, daemon=True).start()
        return self

    def _watchdog(self):
        while not self.stopping:
            time.sleep(min(5.0, WATCHDOG_SECONDS / 4))
            silent = time.monotonic() - self.last_beat
            if silent > WATCHDOG_SECONDS and not self.stopping:
                print(f"{self.name}: WATCHDOG main loop silent for {silent:.0f}s, exiting "
                      f"so the service is restarted", flush=True)
                os._exit(70)

    def running(self, detail=""):
        self.set(RUNNING, detail)

    def degraded(self, detail):
        self.set(DEGRADED, detail)

    def ok(self, detail=""):
        """Back to Running after a degraded period (no-op otherwise)."""
        if self.state == DEGRADED:
            self.set(RUNNING, detail)

    # -- liveness -------------------------------------------------------------

    def beat(self):
        self.last_beat = time.monotonic()
        try:
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(f"{time.time():.0f} {self.state}\n")
        except OSError:
            pass

    def sleep(self, seconds):
        """Sleep, waking early when asked to stop."""
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            time.sleep(min(0.2, end - time.monotonic()))

    # -- shutdown ---------------------------------------------------------------

    def _on_signal(self, signum, frame):
        self.stopping = True

    def stop(self):
        self.set(STOPPING, "shutdown requested")
        try:
            self.bus.stop()
        except Exception:  # noqa: BLE001
            pass
        self.state = STOPPED
        print(f"{self.name}: STOPPED", flush=True)
