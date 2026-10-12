"""End-to-end and fault-injection tests: the whole stack, as processes.

Starts a broker (an existing one from E2E_MQTT_HOST/E2E_MQTT_PORT, or a
local `mosquitto` binary on a free port), an in-memory fake BuildSim, and the
real services: fire simulator (fixed fire in A109), one sensor gateway per
floor, actuator gateway, building agent and BuildSim bridge. Time runs 10x
faster than in the default deployment.

    pytest devices/tests -m e2e -v

Each test prints what it measured (detection time, recovery time, ...);
`-s` shows it. The measurements are written to e2e_results.json as evidence
for the test plan.
"""
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

pytestmark = pytest.mark.e2e

DEVICES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIME_SCALE = 10
RESULTS = {}
MOSQUITTO_DIR = os.path.join(DEVICES, "..", "mosquitto")
# The development users (mosquitto/passwd may hold the real ones from .env).
PASSWD = next((p for p in (os.path.join(MOSQUITTO_DIR, n) for n in ("passwd.test", "passwd"))
               if os.path.exists(p)), os.path.join(MOSQUITTO_DIR, "passwd.test"))
# Broker users per service (mosquitto/passwd, mosquitto/acl); password "<user>-dev".
USERS = {
    "actuator-gateway": "actuators", "sensor-gateway-level0": "sensors",
    "sensor-gateway-level1": "sensors", "sensor-gateway-level2": "sensors",
    "door-gateway": "doors", "building-agent": "agent", "buildsim-bridge": "bridge",
    "fire-simulator": "simulator", "occupants": "evacuation", "db-writer": "recorder",
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(condition, timeout, step=0.5, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        value = condition()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


class Observer:
    """Test-side MQTT client recording everything of interest."""

    def __init__(self, host, port, secure=False):
        import paho.mqtt.client as mqtt
        self.lock = threading.Lock()
        self.states, self.alerts, self.events, self.status = {}, [], [], {}
        self.readings = {}
        self.door_history = []   # (time, door id, payload)
        self.topics = {}         # plan, risk, progress, clock: [payload, ...]
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="e2e-observer")
        if secure:
            self.client.username_pw_set("operator", "operator-dev")
        self.client.on_message = self._on_message
        self.client.connect(host, port)
        for topic in ("building/+/+/actuator/+/state", "building/alerts", "building/events",
                      "building/gateways/+/status", "building/agent/status",
                      "building/services/+/status", "building/+/+/sensors",
                      "building/doors/+/state", *self.KEPT):
            self.client.subscribe(topic, 1)
        self.client.loop_start()

    KEPT = ("building/evacuation/plan", "building/evacuation/progress", "building/risk",
            "sim/clock")

    def history(self, topic):
        with self.lock:
            return list(self.topics.get(topic, []))

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload) if msg.payload else None
        except ValueError:
            return
        parts = msg.topic.split("/")
        with self.lock:
            if msg.topic in self.KEPT:
                if payload is not None:
                    kept = self.topics.setdefault(msg.topic, [])
                    kept.append(payload)
                    del kept[:-500]
                return
            if msg.topic == "building/alerts":
                self.alerts = payload or []
            elif msg.topic == "building/events":
                self.events.append(payload)
            elif parts[1] == "doors" and parts[-1] == "state":
                self.door_history.append((time.time(), parts[2], payload or {}))
            elif parts[-1] == "status":
                self.status["/".join(parts[1:-1])] = (payload or {}).get("status")
            elif parts[-1] == "state":
                self.states[(f"{parts[1]}/{parts[2]}", parts[4])] = (payload or {}).get("state")
            elif parts[-1] == "sensors":
                self.readings[f"{parts[1]}/{parts[2]}"] = (time.time(), payload)

    def publish(self, topic, payload, raw=False):
        data = payload if raw else json.dumps(payload)
        self.client.publish(topic, data, qos=1).wait_for_publish(5)

    def events_of(self, type_):
        with self.lock:
            return [e for e in self.events if isinstance(e, dict) and e.get("type") == type_]

    def alert_ids(self):
        with self.lock:
            return {a.get("id") for a in self.alerts}


class Stack:
    def __init__(self):
        from fake_buildsim import FakeBuildSim
        self.tmp = tempfile.mkdtemp(prefix="fire-e2e-")
        self.procs = {}
        self.broker = None
        host = os.environ.get("E2E_MQTT_HOST")
        self.secure = os.path.exists(PASSWD)
        if host:
            self.mqtt = (host, int(os.environ.get("E2E_MQTT_PORT", "1883")))
            self.secure = os.environ.get("E2E_MQTT_SECURE", "1") == "1"
        else:
            binary = shutil.which("mosquitto") or (
                "/usr/sbin/mosquitto" if os.path.exists("/usr/sbin/mosquitto") else None)
            if not binary:
                pytest.skip("no MQTT broker: install mosquitto or set E2E_MQTT_HOST")
            port = free_port()
            conf = os.path.join(self.tmp, "mosquitto.conf")
            with open(conf, "w") as f:
                f.write(f"listener {port} 127.0.0.1\n")
                if self.secure:   # the real users and ACL
                    shutil.copy(PASSWD, os.path.join(self.tmp, "passwd"))
                    shutil.copy(os.path.join(MOSQUITTO_DIR, "acl"), os.path.join(self.tmp, "acl"))
                    # Readable by the broker (it drops root privileges).
                    os.chmod(self.tmp, 0o755)
                    for name in ("passwd", "acl"):
                        os.chmod(os.path.join(self.tmp, name), 0o644)
                    f.write(f"allow_anonymous false\npassword_file {self.tmp}/passwd\n"
                            f"acl_file {self.tmp}/acl\n")
                else:
                    f.write("allow_anonymous true\n")
            self.broker = subprocess.Popen([binary, "-c", conf], stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
            self.mqtt = ("127.0.0.1", port)
            time.sleep(0.5)
        from conftest import DATA_DIR
        self.buildsim = FakeBuildSim(data_dir=os.path.abspath(DATA_DIR)).start()
        self.env = dict(
            os.environ, PYTHONUNBUFFERED="1", PYTHONPATH=DEVICES,
            BUILDSIM_URL=self.buildsim.url, MQTT_HOST=self.mqtt[0], MQTT_PORT=str(self.mqtt[1]),
            BUILDING_DATA_DIR=os.path.abspath(DATA_DIR), TIME_SCALE=str(TIME_SCALE),
            SCENARIO_MODE="fixed", START_FIRE_ROOMS="A109", INCIPIENT_SECONDS="120",
            EXTERNAL_DOORS="0", DOOR_OPEN_PROB="0.5", DETECTOR="rules",
            SENSOR_STALE_SECONDS="10", COMMAND_RESEND_SECONDS="3",
            DATA_QUALITY_REPORT_SECONDS="3", ACTUATOR_PROBE_SECONDS="3",
            BRIDGE_REFRESH_SECONDS="60", MAX_REQUESTS_PER_SECOND="400",
            DOOR_REPUBLISH_SECONDS="3", DOOR_RESET_SECONDS="60", ENV_STALE_SECONDS="4",
            HEARTBEAT_FILE=os.path.join(self.tmp, "heartbeat"),
            # Night: the access control has the office and stairwell doors locked.
            BUILDING_CLOCK_START="mon 22:00",
            SPREAD_MODEL_PATH=os.path.join(DEVICES, "..", "models", "spread_lgbm.txt"),
            COVERAGE_SECONDS="1",
        )
        self.services = {
            "actuator-gateway": (["actuator_gateway.py"], {"GATEWAY_NAME": "actuators"}),
            "sensor-gateway-level0": (["sensor.py"], {"GATEWAY_NAME": "sensors-level0",
                                                      "GATEWAY_LEVELS": "level0"}),
            "sensor-gateway-level1": (["sensor.py"], {"GATEWAY_NAME": "sensors-level1",
                                                      "GATEWAY_LEVELS": "level1"}),
            "sensor-gateway-level2": (["sensor.py"], {"GATEWAY_NAME": "sensors-level2",
                                                      "GATEWAY_LEVELS": "level2"}),
            "door-gateway": (["door_gateway.py"], {"GATEWAY_NAME": "doors"}),
            "building-agent": (["building_agent.py"], {}),
            "buildsim-bridge": (["buildsim_bridge.py"], {}),
            "fire-simulator": (["fire_simulator.py"], {}),
            "occupants": ([os.environ.get("E2E_EVACUATION_SCRIPT") or os.path.join(
                               DEVICES, "..", "examples", "api", "scenario",
                               "populate_evacuation.py"), self.buildsim.url,
                           "--people", "20", "--schedule", "off", "--doors", "mqtt",
                           "--tick", "0.5", "--premovement", "5:10", "--seed", "3"], {}),
        }
        if not os.path.exists(self.services["occupants"][0][0]):
            del self.services["occupants"]
        if host:
            self.clear_retained()   # a shared test broker keeps the last run's state
        self.observer = Observer(*self.mqtt, secure=self.secure)
        self.started_at = time.time()
        for name in self.services:
            self.start(name)

    def clear_retained(self):
        import paho.mqtt.client as mqtt
        topics = set()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="e2e-cleaner")
        if self.secure:
            client.username_pw_set("operator", "operator-dev")
        client.on_message = lambda c, u, m: m.retain and m.payload and topics.add(m.topic)
        client.connect(*self.mqtt)
        client.subscribe([("building/#", 0), ("sim/#", 0)])
        client.loop_start()
        time.sleep(2)
        infos = [client.publish(topic, b"", qos=1, retain=True) for topic in topics]
        for info in infos[-50:]:
            info.wait_for_publish(10)
        client.loop_stop()
        client.disconnect()

    def start(self, name):
        args, extra = self.services[name]
        extra = dict(extra, HEARTBEAT_FILE=os.path.join(self.tmp, f"{name}.heartbeat"))
        if self.secure and name in USERS:
            extra.update(MQTT_USERNAME=USERS[name], MQTT_PASSWORD=f"{USERS[name]}-dev")
        log = open(os.path.join(self.tmp, f"{name}.log"), "a")
        self.procs[name] = subprocess.Popen(
            [sys.executable, *args], cwd=DEVICES, env=dict(self.env, **extra),
            stdout=log, stderr=subprocess.STDOUT)

    def kill(self, name, sig=signal.SIGKILL):
        proc = self.procs.pop(name)
        proc.send_signal(sig)
        proc.wait(10)

    def log(self, name):
        with open(os.path.join(self.tmp, f"{name}.log")) as f:
            return f.read()

    def stop(self):
        for name in list(self.procs):
            try:
                self.kill(name)
            except Exception:  # noqa: BLE001
                pass
        if self.broker:
            self.broker.kill()
        self.buildsim.stop()


@pytest.fixture(scope="module")
def stack(data_dir):
    s = Stack()
    yield s
    s.stop()
    with open(os.path.join(DEVICES, "tests", "e2e_results.json"), "w") as f:
        json.dump(RESULTS, f, indent=2)
    print(f"\nlogs in {s.tmp}")


def test_fire_is_detected_and_suppressed_through_buildsim(stack):
    """Closed loop: fire -> BuildSim layers -> sensors -> agent -> actuator
    gateway -> BuildSim actuator state -> simulator -> fire goes out."""
    fake, obs = stack.buildsim, stack.observer
    wait_for(lambda: fake.actuator_state("level0-A109-sprinkler") is not None, 60,
             what="actuator gateway registration")
    flaming_real = stack.started_at + 120 / TIME_SCALE
    wait_for(lambda: fake.actuator_state("level0-A109-sprinkler") == "on", 120,
             what="sprinkler in A109 applied in BuildSim")
    released = time.time()
    # Confirmed on MQTT right after BuildSim accepted it.
    wait_for(lambda: obs.states.get(("level0/A109", "sprinkler")) == "on", 10,
             what="confirmed sprinkler state on MQTT")
    wait_for(lambda: "fire-level0-A109" in obs.alert_ids(), 15, what="fire alert")
    RESULTS["release_after_flaming_sim_s"] = round((released - flaming_real) * TIME_SCALE)
    print(f"sprinkler released ~{RESULTS['release_after_flaming_sim_s']} simulated s "
          f"after flaming started")

    # The simulator read the sprinkler state from BuildSim: the fire decays.
    peak = fake.layer_value("fire_hrr", "level0/A109", 0.0)
    wait_for(lambda: fake.layer_value("fire_hrr", "level0/A109", 0.0) < max(5.0, 0.2 * peak),
             60, what="fire heat release falling under the sprinkler")
    # No sprinkler anywhere else.
    others = [k for k, v in obs.states.items() if k[1] == "sprinkler" and v == "on"
              and k[0] != "level0/A109"]
    assert not others


def test_fire_alarm_unlocks_egress_doors(stack):
    """At night the stairwell doors are locked; the fire alarm releases them
    (closed fire doors stay unlocked), the fire exits were never locked."""
    obs = stack.observer
    history = list(obs.door_history)
    locked_at_start = {i for _, i, p in history
                       if p.get("lock_state") == "locked" and "scenario" in p.get("reason", "")}
    assert len(locked_at_start) > 500, "the access control should lock most doors at night"
    wait_for(lambda: any("egress" in p.get("reason", "") and p.get("lock_state") == "unlocked"
                         for _, _, p in obs.door_history), 30, what="egress unlock")
    time.sleep(2)
    unlocked = {i for _, i, p in obs.door_history
                if "egress" in p.get("reason", "") and p.get("lock_state") == "unlocked"}
    # Everything except the secure rooms (stores, server rooms) is released.
    assert unlocked <= locked_at_start
    assert len(unlocked) > 0.85 * len(locked_at_start)
    assert all(p.get("lock_state") != "locked" for _, _, p in obs.door_history
               if p.get("kind") == "fire_exit")
    RESULTS["egress_doors_unlocked"] = len(unlocked)


def test_smoke_purge_opens_dampers(stack):
    fake = stack.buildsim
    wait_for(lambda: any(a.get("state") == "1.0" for i, a in fake.actuators.items()
                         if i.endswith("-damper")), 60, what="a damper opened in BuildSim")


def test_corrupt_sensor_data_is_rejected(stack):
    obs = stack.observer
    topic = "building/level0/A110/sensors"
    now = time.time()
    good = {"ts": now, "gateway": "sensors-level0", "temperature": 21.0, "smoke": 0.0,
            "co": 0, "co2": 420}
    before = len(obs.events_of("data_quality"))
    obs.publish(topic, b"\x00\xffnot json", raw=True)
    obs.publish(topic, '{"ts": %f, "gateway": "x", "temperature": NaN, "smoke": 0, '
                       '"co": 0, "co2": 420}' % now, raw=True)
    obs.publish(topic, dict(good, temperature=5000))
    obs.publish(topic, dict(good, smoke=-3))
    obs.publish(topic, dict(good, ts=now + 7200))
    obs.publish(topic, dict(good, ts=now - 3600))     # older than the last reading
    obs.publish(topic, [1, 2, 3])
    wait_for(lambda: len(obs.events_of("data_quality")) > before, 20,
             what="data_quality event from the agent")
    event = obs.events_of("data_quality")[-1]
    assert set(event["rejected"]) >= {"malformed", "not_numeric", "out_of_range", "future",
                                      "duplicate"}
    assert stack.procs["building-agent"].poll() is None, "agent must survive corrupt data"
    assert obs.states.get(("level0/A110", "sprinkler")) != "on"
    RESULTS["corrupt_messages_rejected"] = event["rejected"]


def test_corrupt_actuator_command_is_rejected(stack):
    obs = stack.observer
    before = len(obs.events_of("command_rejected"))
    obs.publish("building/level0/A110/actuator/sprinkler/set", {"state": "maybe", "id": "t1"})
    obs.publish("building/level0/A110/actuator/damper/set", {"state": 7, "id": "t2"})
    obs.publish("building/level0/NOPE/actuator/sprinkler/set", {"state": "on", "id": "t3"})
    wait_for(lambda: len(obs.events_of("command_rejected")) >= before + 3, 15,
             what="three command_rejected events")
    assert stack.buildsim.actuator_state("level0-A110-sprinkler") == "off"


def test_doors_are_published_to_buildsim(stack):
    fake = stack.buildsim
    wait_for(lambda: len(fake.doors) > 800, 30, what="door snapshot in BuildSim")
    kinds = {d["kind"] for d in fake.doors}
    assert {"door", "fire_exit"} <= kinds
    assert any(d["id"].startswith("lift-") for d in fake.doors)
    assert all(d.get("entry_node_id") is not None for d in fake.doors if d["kind"] == "door"
               and not d["id"].startswith("lift-"))


def test_fire_alarm_closes_doors_and_recalls_lifts(stack):
    """The fire in A109 releases the hold-open devices on level 0 and recalls
    the lifts; the simulator reads the closed doors back from BuildSim."""
    fake = stack.buildsim

    def level0_closed():
        doors = [d for d in fake.doors if d["level"] == "level0" and d["kind"] == "door"
                 and not d["id"].startswith("lift-")]
        return doors and all(d["state"] == "closed" for d in doors)
    wait_for(level0_closed, 60, what="all level-0 doors closed")
    wait_for(lambda: all(d["state"] == "blocked" for d in fake.doors
                         if d["id"].startswith("lift-")), 30, what="lifts recalled")
    # Doors on other floors are left alone by the fire-door policy (at night
    # they are closed anyway: locked doors are shut).
    assert not any("hold-open" in p.get("reason", "") and p.get("level") == "level2"
                   for _, _, p in stack.observer.door_history)
    wait_for(lambda: "doors changed state" in stack.log("fire-simulator"), 20,
             what="simulator applying the door states")


def test_door_rules_are_enforced(stack):
    obs, fake = stack.observer, stack.buildsim
    door = next(d["id"] for d in fake.doors if d["kind"] == "door" and d["level"] == "level1"
                and not d["id"].startswith("lift-"))
    exit_door = next(d["id"] for d in fake.doors if d["kind"] == "fire_exit")
    before = len(obs.events_of("door_command_rejected"))
    obs.publish(f"building/doors/{door}/set", {"state": "closed", "lock_state": "locked",
                                                "source": "operator", "id": "lock-1"})
    wait_for(lambda: (fake.door(door) or {}).get("lock_state") == "locked", 15,
             what="door locked")
    obs.publish(f"building/doors/{door}/set", {"state": "open", "source": "operator", "id": "o"})
    obs.publish(f"building/doors/{exit_door}/set", {"lock_state": "locked", "source": "operator"})
    obs.publish(f"building/doors/{door}/set", {"state": "open", "source": "intruder"})
    obs.publish(f"building/doors/{door}/set", {"state": "ajar", "source": "operator"})
    wait_for(lambda: len(obs.events_of("door_command_rejected")) >= before + 4, 15,
             what="four rejected door commands")
    assert fake.door(door)["state"] == "closed" and fake.door(door)["blocked"]
    assert fake.door(exit_door)["lock_state"] == "unlocked"
    obs.publish(f"building/doors/{door}/set", {"state": "open", "lock_state": "unlocked",
                                                "source": "operator", "id": "u"})
    wait_for(lambda: fake.door(door)["state"] == "open", 15, what="unlock and open together")


def test_jammed_door_makes_the_evacuation_replan(stack):
    if "occupants" not in stack.procs:
        pytest.skip("populate_evacuation.py not found")
    obs, fake = stack.observer, stack.buildsim
    wait_for(lambda: "FIRE DETECTED" in stack.log("occupants"), 60, what="evacuation started")
    # Jam the door of a room on level 0 that has people's routes nearby.
    door = next(d["id"] for d in fake.doors if d["room"] == "A110" and d["kind"] == "door")
    obs.publish(f"building/doors/{door}/fault", {"jammed": True, "source": "test"})
    wait_for(lambda: (fake.door(door) or {}).get("lock_state") == "jammed", 15,
             what="door jammed in BuildSim")
    wait_for(lambda: "re-planning routes" in stack.log("occupants").split("FIRE DETECTED")[-1],
             20, what="routes re-planned after the jam")
    obs.publish(f"building/doors/{door}/fault", {"jammed": False, "source": "test"})


def test_agent_plan_drives_the_evacuation(stack):
    """D4: the agent owns the evacuation plan; the occupants follow it on the
    building clock and report progress back."""
    if "occupants" not in stack.procs:
        pytest.skip("populate_evacuation.py not found")
    obs = stack.observer
    plan = wait_for(lambda: next((p for p in obs.history("building/evacuation/plan")
                                  if p.get("phase") == "evacuate"), None), 60,
                    what="an evacuation plan from the agent")
    # A109 is on the ground floor: phased evacuation starts there and above,
    # i.e. every floor.
    assert plan["evacuate_levels"] == ["level0", "level1", "level2"]
    assert "level0/A109" in plan["fire_zones"] + plan["alarm_zones"]
    assert plan["doors"]["egress"] == "unlocked"
    assert isinstance(plan["version"], int) and plan["clock"].startswith("Mon 2")
    log = stack.log("occupants")
    assert "trigger: plan" in log and "following the building clock: Mon 2" in log
    progress = wait_for(lambda: next((p for p in reversed(obs.history(
        "building/evacuation/progress")) if p.get("phase") == "evacuate"
        and p["pre_movement"] + p["evacuating"] + p["evacuated"] > 0), None), 60,
        what="evacuation progress from the occupants")
    assert progress["levels"] == ["level0", "level1", "level2"]
    risk = wait_for(lambda: obs.history("building/risk"), 30, what="spread risk")
    assert "zones" in risk[-1] and risk[-1]["model"] in ("lightgbm", "baseline")
    RESULTS["spread_model"] = risk[-1]["model"]
    RESULTS["evacuation_plan_versions"] = len(obs.history("building/evacuation/plan"))


def test_dashboard_highlights_and_coverage(stack):
    """D5: the viewer shows red fire, green exits and safe corridors (orange
    for smoke and predicted spread), and smoke as coverage zones; every PUT
    passed BuildSim's validation."""
    if "occupants" not in stack.procs:
        pytest.skip("populate_evacuation.py not found")
    fake = stack.buildsim
    wait_for(lambda: any(any(h["room"] == "A109" and h["color"] == "#ef4444" for h in hs)
                         for hs in list(fake.history["highlights"])), 60,
             what="A109 highlighted red")
    colours = {h["color"] for hs in list(fake.history["highlights"]) for h in hs}
    assert "#22c55e" in colours, "exits green"
    assert "#86efac" in colours, "safe corridors green"
    cover = wait_for(lambda: next((c for c in list(fake.history["coverage"])
                                   if any(z["room"] == "A109" for z in c)), None), 60,
                     what="smoke coverage over A109")
    zone = next(z for z in cover if z["room"] == "A109")
    assert zone["id"] == "smoke-level0-A109" and zone["radius"] > 0 and 0 < zone["opacity"] <= 1
    assert not fake.rejected, fake.rejected[:3]
    RESULTS["coverage_zones_max"] = max(len(c) for c in fake.history["coverage"])


def test_routes_agree_with_buildsims_route_api(stack):
    """D6: the local hazard-weighted planner searches the same graph as
    BuildSim; ignoring hazards, both find the same routes."""
    if "occupants" not in stack.procs:
        pytest.skip("populate_evacuation.py not found")
    checks = wait_for(lambda: stack.observer.events_of("route_check"), 60,
                      what="route cross-check")
    totals = checks[-1]["totals"]
    assert totals["checked"] > 0
    assert totals["mismatch"] == 0, checks[-1].get("details")
    RESULTS["route_checks"] = totals


def test_sensor_gateway_crash_and_recovery(stack):
    """Kill a floor's gateway (its last will marks it offline), then restart."""
    obs = stack.observer
    zone = "level1/A2000"
    stack.kill("sensor-gateway-level1")
    killed = time.time()
    wait_for(lambda: "gateway-sensors-level1" in obs.alert_ids(), 30,
             what="gateway offline alert")
    RESULTS["gateway_offline_alert_s"] = round(time.time() - killed, 1)
    restarted = time.time()
    stack.start("sensor-gateway-level1")
    wait_for(lambda: obs.readings.get(zone, (0,))[0] > restarted, 30,
             what="fresh readings from the restarted gateway")
    RESULTS["sensor_recovery_s"] = round(time.time() - restarted, 1)
    wait_for(lambda: "gateway-sensors-level1" not in obs.alert_ids(), 30,
             what="gateway alert cleared")
    print(f"offline alert after {RESULTS['gateway_offline_alert_s']} s, readings back "
          f"{RESULTS['sensor_recovery_s']} s after restart")


def test_degraded_mode(stack):
    """Two sensor gateways down: the agent goes Degraded, alerts, keeps water
    on, and recovers when they return."""
    obs = stack.observer
    stack.kill("sensor-gateway-level1")
    stack.kill("sensor-gateway-level2")
    wait_for(lambda: obs.status.get("agent") == "degraded", 30, what="agent degraded")
    wait_for(lambda: "agent-degraded" in obs.alert_ids(), 15, what="degraded alert")
    stack.start("sensor-gateway-level1")
    stack.start("sensor-gateway-level2")
    wait_for(lambda: obs.status.get("agent") == "online", 40, what="agent running again")
    wait_for(lambda: "agent-degraded" not in obs.alert_ids(), 15, what="alert cleared")


def test_gateway_degrades_when_buildsim_is_unreachable(stack):
    """A gateway that cannot read the environment stops publishing and says
    so, rather than passing stale values off as fresh."""
    obs, fake = stack.observer, stack.buildsim
    fake.blocked_paths = {"/api/room-layers"}
    try:
        wait_for(lambda: obs.status.get("gateways/sensors-level0") == "degraded", 30,
                 what="sensor gateway degraded")
        wait_for(lambda: obs.status.get("agent") == "degraded", 30,
                 what="agent degraded because of it")
    finally:
        fake.blocked_paths = set()
    wait_for(lambda: obs.status.get("gateways/sensors-level0") == "online", 30,
             what="sensor gateway recovered")


def test_graceful_shutdown(stack):
    proc = stack.procs["buildsim-bridge"]
    started = time.time()
    stack.kill("buildsim-bridge", signal.SIGTERM)
    assert proc.returncode == 0, f"exit code {proc.returncode}"
    assert time.time() - started < 5
    wait_for(lambda: obs_status(stack, "services/buildsim-bridge") == "offline", 10,
             what="bridge announced offline")
    assert "STOPPED" in stack.log("buildsim-bridge")
    stack.start("buildsim-bridge")


def obs_status(stack, key):
    return stack.observer.status.get(key)


def test_agent_restart_keeps_a_running_sprinkler_on(stack):
    obs = stack.observer
    obs.publish("building/level0/A120/actuator/sprinkler/set",
                {"state": "on", "id": "manual-1", "source": "operator", "reason": "manual"})
    wait_for(lambda: obs.states.get(("level0/A120", "sprinkler")) == "on", 15,
             what="manual sprinkler applied")
    stack.kill("building-agent")
    restarted = time.time()
    stack.start("building-agent")
    wait_for(lambda: "adopting running sprinkler" in stack.log("building-agent"), 30,
             what="agent adopts the running sprinkler")
    time.sleep(5)
    assert obs.states.get(("level0/A120", "sprinkler")) == "on"
    assert stack.buildsim.actuator_state("level0-A120-sprinkler") == "on"
    RESULTS["agent_restart_adopt_s"] = round(time.time() - restarted - 5, 1)


def test_stale_sprinkler_confirmation_is_corrected(stack):
    """A retained "on" left over from a previous run must not switch water on
    after a restart: the actuator gateway takes the states from BuildSim at
    start and corrects the stale confirmation; the agent drops any adoption."""
    obs, fake = stack.observer, stack.buildsim
    topic = "building/level1/A2000/actuator/sprinkler/state"
    obs.client.publish(topic, json.dumps({"state": "on", "ts": time.time() - 3600,
                                          "source": "previous run"}),
                       qos=1, retain=True).wait_for_publish(5)
    time.sleep(1)
    stack.kill("actuator-gateway")
    stack.start("actuator-gateway")
    wait_for(lambda: obs.states.get(("level1/A2000", "sprinkler")) == "off", 30,
             what="stale confirmation corrected")
    time.sleep(2 * TIME_SCALE / 5 + 4)
    assert fake.actuator_state("level1-A2000-sprinkler") != "on"
    assert obs.states.get(("level1/A2000", "sprinkler")) == "off"


def test_buildsim_restart_is_recovered(stack):
    """BuildSim keeps state in memory: owners re-register and re-apply."""
    fake, obs = stack.buildsim, stack.observer
    on = [z for (z, a), v in obs.states.items() if a == "sprinkler" and v == "on"]
    fake.restart()
    restarted = time.time()
    wait_for(lambda: any(i.startswith("fire_suppression-") for i in fake.equipment), 30,
             what="suppression equipment re-registered")
    wait_for(lambda: any(i.startswith("fire_detection-") for i in fake.equipment), 60,
             what="detection equipment re-registered")
    for zone in on:
        level, room = zone.split("/", 1)
        if obs.states.get((zone, "sprinkler")) == "on":
            wait_for(lambda: fake.actuator_state(f"{level}-{room}-sprinkler") == "on", 15,
                     what=f"sprinkler state of {zone} re-applied")
    wait_for(lambda: fake.layers, 15, what="room layers re-published")
    wait_for(lambda: len(fake.doors) > 800, 15, what="doors re-published")
    RESULTS["buildsim_restart_recovery_s"] = round(time.time() - restarted, 1)


def test_broker_enforces_users_and_acl(stack):
    if not stack.secure:
        pytest.skip("broker without users (no mosquitto/passwd)")
    import paho.mqtt.client as mqtt
    host, port = stack.mqtt
    anonymous = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="e2e-anon")
    result = {}
    anonymous.on_connect = lambda c, u, f, rc, p: result.setdefault("rc", rc)
    anonymous.connect(host, port)
    anonymous.loop_start()
    wait_for(lambda: "rc" in result, 10, what="anonymous connect answer")
    anonymous.loop_stop()
    assert result["rc"].is_failure, "anonymous clients must be refused"

    # A sensor gateway's credentials cannot command a sprinkler.
    sensors = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="e2e-sensors")
    sensors.username_pw_set("sensors", "sensors-dev")
    sensors.connect(host, port)
    sensors.loop_start()
    sensors.publish("building/level0/A130/actuator/sprinkler/set",
                    json.dumps({"state": "on", "source": "intruder", "id": "acl"}), qos=1)
    time.sleep(5)
    sensors.loop_stop()
    assert stack.observer.states.get(("level0/A130", "sprinkler")) != "on"
    assert stack.buildsim.actuator_state("level0-A130-sprinkler") == "off"


def test_no_service_crashed(stack):
    for name, proc in stack.procs.items():
        assert proc.poll() is None, f"{name} exited:\n{stack.log(name)[-2000:]}"
    assert math.isfinite(time.time() - stack.started_at)
