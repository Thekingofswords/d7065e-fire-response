"""Actuator gateway: validates and applies commands for every zone's actuators.

Listens on building/<level>/<room>/actuator/<name>/set for commands from the
building agent (or anyone else), validates them, *applies them to BuildSim*
(PUT /api/actuators/{id}/state) and only then confirms the applied state on
building/<level>/<room>/actuator/<name>/state (retained). Every accepted,
rejected or failed command is also reported on building/events, which gives
the database an audit trail.

BuildSim is the single source of truth for actuator state: the fire
simulator reads the sprinkler and damper states back from BuildSim each
tick, so the control loop closes through BuildSim. The MQTT state topic is a
confirmation for the agent and the database.

This gateway owns (registers) one fire-suppression equipment per zone,
fire_suppression-<level>-<room>, with actuators <level>-<room>-sprinkler and
<level>-<room>-damper. After a BuildSim restart (equipment gone) it
re-registers and re-applies every state it applied. When the gateway
itself starts, it takes the states from BuildSim, not from the retained MQTT
confirmations: after a gateway restart BuildSim still holds the running
sprinklers, after a restart of the whole stack BuildSim is fresh and a
retained "on" from the previous run is stale. Stale confirmations are
corrected (re-published with BuildSim's state), so the agent never adopts a
sprinkler that is not running.

The *requested* state (set) and the *applied* state (state) are separate
topics on purpose: the agent acts on the confirmed state only.

Supported actuators:
  sprinkler   "on" or "off"
  damper      number between 0 and 1 (smoke purge)
"""
import os
import queue
import time
import urllib.error
import urllib.parse

import common
import lifecycle
import mqtt_bus
import topology

PROBE_SECONDS = float(os.environ.get("ACTUATOR_PROBE_SECONDS", "15"))
DEFAULTS = {"sprinkler": "off", "damper": "0.0"}


def validate(actuator, state):
    """Return the normalised state or raise ValueError."""
    if actuator == "sprinkler":
        if state not in ("on", "off"):
            raise ValueError('sprinkler state must be "on" or "off"')
        return state
    if actuator == "damper":
        if isinstance(state, bool):
            raise ValueError("damper state must be a number between 0 and 1")
        try:
            value = float(state)
        except (TypeError, ValueError):
            raise ValueError("damper state must be a number between 0 and 1") from None
        if not 0.0 <= value <= 1.0:   # also rejects NaN
            raise ValueError("damper state must be between 0 and 1")
        return str(value)
    raise ValueError(f"unsupported actuator: {actuator}")


def actuator_id(level, room, actuator):
    return f"{level}-{room}-{actuator}"


def equipment(zone):
    level, room = zone.level, zone.name
    return {
        "id": f"fire_suppression-{level}-{room}",
        "name": f"Sprinkler and smoke purge {room}",
        "type": "sprinkler",
        "category": "fire_suppression",
        "level": level,
        "room": room,
        "status": "running",
        "sensors": [],
        "actuators": [
            {"id": actuator_id(level, room, "sprinkler"), "name": "Sprinkler valve",
             "type": "sprinkler", "state": DEFAULTS["sprinkler"]},
            {"id": actuator_id(level, room, "damper"), "name": "Smoke purge damper",
             "type": "damper", "state": DEFAULTS["damper"]},
        ],
    }


def put_state(level, room, actuator, state):
    common.call("PUT", "/api/actuators/" + urllib.parse.quote(
        actuator_id(level, room, actuator), safe="") + "/state", {"state": state})


class Gateway:
    def __init__(self, name, topo):
        self.name = name
        self.topo = topo
        self.zones = set(topo.sensor_zones())
        self.states = {}          # (zone, actuator) -> applied state
        self.retained = {}        # (zone, actuator) -> retained confirmation at start
        self.synced = False
        self.commands = queue.Queue()
        self.registered = False
        self.life = lifecycle.Lifecycle(name, status_topic=mqtt_bus.gateway_status_topic(name))
        self.bus = self.life.bus
        self.bus.subscribe(mqtt_bus.ACTUATOR_STATE_SUB, self.on_state)
        self.bus.subscribe(mqtt_bus.ACTUATOR_SET_SUB, self.on_command)

    # -- BuildSim -------------------------------------------------------------

    def register(self):
        items = [equipment(self.topo.zones[k]) for k in sorted(self.zones)]
        created = 0
        for i in range(0, len(items), 150):
            result = common.call("POST", "/api/equipment/bulk", items[i:i + 150]) or {}
            created += int(result.get("created", 0))
        self.registered = True
        print(f"registered fire-suppression equipment for {len(items)} zones "
              f"({created} new)", flush=True)
        if not self.synced:
            self.sync_from_buildsim()
            self.synced = True
        else:
            self.reapply()

    def sync_from_buildsim(self):
        """At start: BuildSim's actuator states are the truth; correct any
        retained confirmation that disagrees."""
        items = common.call("GET", "/api/equipment?category=fire_suppression") or []
        actual = {}
        for item in items:
            zone = f"{item.get('level')}/{item.get('room')}"
            for a in item.get("actuators") or []:
                kind = str(a.get("id", "")).rsplit("-", 1)[-1]
                if kind in DEFAULTS and zone in self.zones:
                    actual[(zone, kind)] = str(a.get("state", DEFAULTS[kind]))
        corrected = 0
        for key, state in self.retained.items():
            now = actual.get(key, DEFAULTS.get(key[1]))
            self.states[key] = now
            if state is not None and str(state) != now:
                level, room = topology.split(key[0])
                self.bus.publish(mqtt_bus.actuator_state_topic(level, room, key[1]), {
                    "state": now, "ts": time.time(), "command_id": None, "corrected": True,
                    "reason": f"gateway start: BuildSim has {now} (retained {state} was stale)",
                    "source": self.name}, qos=1, retain=True)
                corrected += 1
        for key, state in actual.items():
            if state != DEFAULTS.get(key[1]):
                self.states[key] = state
        if corrected:
            print(f"corrected {corrected} stale actuator confirmations from BuildSim",
                  flush=True)

    def reapply(self):
        """Write every non-default state we hold (after a BuildSim restart)."""
        n = 0
        for (zone, actuator), state in list(self.states.items()):
            if state is not None and state != DEFAULTS.get(actuator):
                level, room = topology.split(zone)
                try:
                    put_state(level, room, actuator, state)
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"re-apply {zone} {actuator} failed: {exc}", flush=True)
        if n:
            print(f"re-applied {n} actuator states to BuildSim", flush=True)

    def probe(self):
        zone = sorted(self.zones)[0]
        level, room = topology.split(zone)
        try:
            common.call("GET", "/api/actuators/" + urllib.parse.quote(
                actuator_id(level, room, "sprinkler"), safe=""))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print("BuildSim lost the actuators (restart?): re-registering", flush=True)
                self.registered = False
        except Exception:  # noqa: BLE001
            pass

    # -- MQTT -----------------------------------------------------------------

    def on_state(self, topic, payload):
        # Retained confirmations seen at start (checked against BuildSim).
        level, room, rest = mqtt_bus.parse_room_topic(topic)
        if level and len(rest) >= 2 and isinstance(payload, dict) and not self.synced:
            self.retained.setdefault((f"{level}/{room}", rest[1]), payload.get("state"))

    def on_command(self, topic, payload):
        # Runs on the MQTT thread: only queue, the HTTP call happens in run().
        self.commands.put((topic, payload))

    def handle(self, topic, payload):
        level, room, rest = mqtt_bus.parse_room_topic(topic)
        if not level or len(rest) < 3:
            return
        actuator = rest[1]
        zone = f"{level}/{room}"
        command = payload if isinstance(payload, dict) else {"state": payload}
        command_id = command.get("id")
        reason = command.get("reason", "")
        try:
            if zone not in self.zones:
                raise ValueError(f"unknown room {zone}")
            state = validate(actuator, command.get("state"))
        except ValueError as exc:
            print(f"REJECTED {zone} {actuator}: {exc}", flush=True)
            self.bus.publish(mqtt_bus.EVENTS_TOPIC, mqtt_bus.event(
                "command_rejected", str(exc), level, room, severity="warning",
                actuator=actuator, command_id=command_id,
                requested=str(command.get("state"))), qos=1)
            return

        try:
            put_state(level, room, actuator, state)
        except Exception as exc:  # noqa: BLE001
            # Not applied, so not confirmed: the agent re-sends its command.
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                self.registered = False
            else:
                self.life.degraded(f"BuildSim write failed: {exc}")
            print(f"FAILED {zone} {actuator}={state}: {exc}", flush=True)
            self.bus.publish(mqtt_bus.EVENTS_TOPIC, mqtt_bus.event(
                "actuator_write_failed", f"BuildSim did not accept {actuator}={state}: {exc}",
                level, room, severity="warning", actuator=actuator, command_id=command_id),
                qos=1)
            return

        self.life.ok()
        previous = self.states.get((zone, actuator))
        self.states[(zone, actuator)] = state
        self.bus.publish(mqtt_bus.actuator_state_topic(level, room, actuator), {
            "state": state, "ts": time.time(), "command_id": command_id,
            "reason": reason, "source": command.get("source", ""),
        }, qos=1, retain=True)
        self.bus.publish(mqtt_bus.EVENTS_TOPIC, mqtt_bus.event(
            "actuator_applied", f"{actuator} {previous or 'unknown'} -> {state}: {reason}",
            level, room,
            severity="critical" if (actuator == "sprinkler" and state == "on") else "info",
            actuator=actuator, state=state, previous=previous, command_id=command_id,
            source=command.get("source", "")), qos=1)
        print(f"applied {zone} {actuator}={state} ({reason})", flush=True)

    def run(self):
        self.life.start()
        time.sleep(1.0)   # retained states arrive first
        print(f"actuator gateway {self.name}: sprinklers and smoke-purge dampers for "
              f"{len(self.zones)} zones", flush=True)
        last_probe = time.monotonic()
        while not self.life.stopping:
            try:
                self.life.beat()
                if not self.registered:
                    try:
                        self.register()
                    except Exception as exc:  # noqa: BLE001
                        self.life.degraded(f"cannot register equipment in BuildSim: {exc}")
                        self.life.sleep(2)
                        continue
                    if self.life.state == lifecycle.SYNCING:
                        self.life.running(f"{len(self.zones)} zones")
                    else:
                        self.life.ok()
                try:
                    topic, payload = self.commands.get(timeout=1.0)
                    self.handle(topic, payload)
                except queue.Empty:
                    pass
                if time.monotonic() - last_probe > PROBE_SECONDS:
                    last_probe = time.monotonic()
                    self.probe()
            except Exception as exc:  # noqa: BLE001
                print(f"actuator gateway loop failed: {exc}", flush=True)
                time.sleep(2)
        self.life.stop()


def main():
    common.wait_until_healthy()
    name = os.environ.get("GATEWAY_NAME", "actuators")
    Gateway(name, topology.Topology.load(common.BASE)).run()


if __name__ == "__main__":
    main()
