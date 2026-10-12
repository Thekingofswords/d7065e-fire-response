"""Door gateway: owns every door in BuildSim (PUT /api/doors).

BuildSim's door collection is replaced as a whole on every PUT, so exactly
one process may write it: this one. It holds the complete door list:

* one door per doorway of the building (topology.doors()): kind "door", on
  the room's walkable-graph entry node, so a locked, jammed or blocked door
  removes that entry from BuildSim's walkable routes
* the fire exits (EXIT_ROOMS on level 0): kind "fire_exit"
* the lift lobbies (cross-floor "elevator" edges): kind "door"

and merges every validated command into it before publishing the full
snapshot. The fire simulator reads the door states back from BuildSim (an
open door lets smoke through, a closed one only leaks), and evacuation
routing avoids blocked, locked and jammed doors.

Commands (MQTT, JSON {"state"?, "lock_state"?, "reason", "id", "source"}):

    building/doors/<door id>/set          one door
    building/<level>/<room>/doors/set     every door of a zone
    building/<level>/doors/set            every fire door on a floor (FIRE_DOORS)
    building/lifts/set                    every lift lobby
    building/doors/egress/set             every access-controlled door (offices,
                                          stairwells): the fire unlock
    building/doors/<door id>/fault        fault injection {"jammed": true|false}

    state       "open" | "closed" | "blocked" | "normal" (back to the state
                the scenario started with: doors people had left open)
    lock_state  "locked" | "unlocked" | "normal" (back to the access-control
                schedule)

Access control (the building's normal locking, on the building clock from
sim/clock, simclock.py):
* stairwell and lift-lobby doors: locked outside ACCESS_HOURS (07:00-18:00,
  weekdays), so people need a badge to re-enter a floor after hours
* office doors: locked outside ACCESS_HOURS
* secure rooms (stores, server rooms; SECURE_ROOMS, or a deterministic
  SECURE_SHARE of the rooms): always locked
* fire exits: never locked
A locked door is closed. Badge holders still pass in everyday life (the
occupants ignore plain locks outside an evacuation); in an evacuation a
locked door is impassable, which is why the agent unlocks the egress doors
on a fire alarm (building/doors/egress/set). An explicit lock command
overrides the schedule until "normal".

Door position: every access-controlled stairwell door, secure-room door and
fire exit is also a door_lock equipment in BuildSim (door-<id>) with a
binary door_position sensor (true = open) and a lock actuator, updated with
every confirmed change.

Rules (the gateway enforces them, whoever sends the command):
* a jammed door cannot be operated (only a fault reset clears it)
* a locked door cannot be opened; unlock it first (in the same command is
  fine: {"state": "open", "lock_state": "unlocked"})
* fire exits are never locked or blocked
* sources: COMMAND_SOURCES (agent, operator, evacuation by default)

Every door's applied state is confirmed, retained, on
building/doors/<door id>/state, and every change is an event. Confirmations
go out only after BuildSim has accepted the snapshot that contains the
change (as for the actuator gateway), so a confirmed door is a door BuildSim
shows and routes by.

Initial door states come from the scenario (sim/scenario): the doors that
were open when the fire started. A new scenario resets every door.
"""
import json
import os
import queue
import threading
import time

import common
import lifecycle
import mqtt_bus
import scenarios
import simclock
import topology

EXIT_ROOMS = [r.strip() for r in os.environ.get(
    "EXIT_ROOMS", "1575,A183,A119,A1405,A1015,A104").split(",") if r.strip()]
EXIT_LEVEL = os.environ.get("EXIT_LEVEL", "level0")
COMMAND_SOURCES = {s.strip() for s in os.environ.get(
    "DOOR_COMMAND_SOURCES", "building-agent,operator,evacuation,test").split(",") if s.strip()}
REPUBLISH_SECONDS = float(os.environ.get("DOOR_REPUBLISH_SECONDS", "30"))
# Which doors have hold-open devices that a fire alarm releases (and so
# answer a floor-wide "closed"): all | stairs (doors to stairwells and lift
# lobbies only) | none.
FIRE_DOORS = os.environ.get("FIRE_DOORS", "all").strip().lower()
ACCESS_HOURS = os.environ.get("ACCESS_HOURS", "07:00-18:00")
ACCESS_CONTROL = os.environ.get("ACCESS_CONTROL", "true").lower() == "true"
SECURE_ROOMS = {r.strip() for r in os.environ.get("SECURE_ROOMS", "").split(",") if r.strip()}
SECURE_SHARE = float(os.environ.get("SECURE_SHARE", "0.05"))
DOOR_EQUIPMENT = os.environ.get("DOOR_EQUIPMENT", "true").lower() == "true"

STATES = ("open", "closed", "blocked")
LOCKS = ("unlocked", "locked")

DOOR_SET_SUB = "building/doors/+/set"
DOOR_FAULT_SUB = "building/doors/+/fault"
ZONE_DOORS_SUB = "building/+/+/doors/set"
LEVEL_DOORS_SUB = "building/+/doors/set"
LIFTS_TOPIC = "building/lifts/set"
EGRESS_TOPIC = "building/doors/egress/set"


def door_state_topic(door_id):
    return f"building/doors/{mqtt_bus.enc(door_id)}/state"


def door_set_topic(door_id):
    return f"building/doors/{mqtt_bus.enc(door_id)}/set"


def zone_doors_topic(level, room):
    return f"building/{mqtt_bus.enc(level)}/{mqtt_bus.enc(room)}/doors/set"


def level_doors_topic(level):
    return f"building/{mqtt_bus.enc(level)}/doors/set"


def is_fire_door(topo, pair, mode=None):
    """Whether the door between the two zones closes on a fire alarm."""
    mode = (mode or FIRE_DOORS)
    if mode == "all":
        return True
    if mode == "stairs":
        stair_zones = {z for link in topo.stairs for z in link} | set(topo.lifts)
        return any(z in stair_zones for z in pair)
    return False


def is_secure(room_key):
    """Stores and server rooms: SECURE_ROOMS, or a deterministic share."""
    import random
    level, name = topology.split(room_key)
    if SECURE_ROOMS:
        return name in SECURE_ROOMS or room_key in SECURE_ROOMS
    return random.Random(f"secure|{room_key}").random() < SECURE_SHARE


def access_class(topo, pair, room):
    """"stair" | "secure" | "office" for a doorway's door."""
    stair_zones = {z for link in topo.stairs for z in link} | set(topo.lifts)
    exits = {topology.key(EXIT_LEVEL, n) for n in EXIT_ROOMS}
    if any(z in stair_zones for z in pair):
        return "stair"
    if room not in exits and topo.zones[room].type == "room" and is_secure(room):
        return "secure"
    return "office"


def scheduled_lock(door, week_minute):
    """The access-control lock state of a door at this time."""
    if not ACCESS_CONTROL or door["kind"] == "fire_exit":
        return "unlocked"
    access = door.get("access")
    if access == "secure":
        return "locked"
    if access in ("stair", "office"):
        return "unlocked" if simclock.in_hours(week_minute, ACCESS_HOURS) else "locked"
    return "unlocked"


def apply_schedule(door, week_minute):
    """Follow the schedule unless a command overrides it. Returns changed
    fields. Locking closes the door; a jammed door is left alone."""
    if door.get("lock_override") or door["lock_state"] == "jammed":
        return []
    target = scheduled_lock(door, week_minute)
    changed = []
    if target != door["lock_state"]:
        door["lock_state"] = target
        changed.append("lock_state")
    if target == "locked" and door["state"] == "open":
        door["state"] = "closed"
        changed.append("state")
    return changed


def build_doors(topo, scenario=None, week_minute=None):
    """{door id: door dict} for the whole building."""
    doors = {}
    for did, pair, room, entry in topo.doors():
        level, name = topology.split(room)
        is_open = scenarios.door_is_open(scenario, *pair) if scenario else True
        doors[did] = {
            "id": did, "name": f"Door {name}", "kind": "door", "level": level, "room": name,
            "entry_node_id": entry, "state": "open" if is_open else "closed",
            "lock_state": "unlocked", "zones": list(pair),
            "fire_door": is_fire_door(topo, pair),
            "access": access_class(topo, pair, room),
        }
    for name in EXIT_ROOMS:
        if topology.key(EXIT_LEVEL, name) in topo.zones:
            did = f"exit-{EXIT_LEVEL}-{name}"
            doors[did] = {"id": did, "name": f"Fire exit {name}", "kind": "fire_exit",
                          "level": EXIT_LEVEL, "room": name, "state": "open",
                          "lock_state": "unlocked", "zones": [topology.key(EXIT_LEVEL, name)]}
    for zone in topo.lifts:
        level, name = topology.split(zone)
        did = f"lift-{level}-{name}"
        doors[did] = {"id": did, "name": f"Lift lobby {name}", "kind": "door", "level": level,
                      "room": name, "state": "open", "lock_state": "unlocked",
                      "zones": [zone], "lift": True}
    for door in doors.values():
        if week_minute is not None:
            apply_schedule(door, week_minute)
        door["normal_state"] = door["state"]
    return doors


def apply_command(door, command, week_minute=None):
    """Validate and apply one command to one door (in place).
    Returns the list of changed fields; raises ValueError if refused."""
    state = command.get("state")
    lock = command.get("lock_state")
    if state is None and lock is None:
        raise ValueError("command needs state and/or lock_state")
    if state is not None and state not in STATES + ("normal",):
        raise ValueError(f"state must be one of {STATES + ('normal',)}")
    if lock is not None and lock not in LOCKS + ("normal",):
        raise ValueError(f"lock_state must be one of {LOCKS + ('normal',)}")
    if door["lock_state"] == "jammed":
        raise ValueError(f"{door['id']} is jammed")
    if door["kind"] == "fire_exit" and (lock == "locked" or state == "blocked"):
        raise ValueError("fire exits are never locked or blocked")
    target_state = door["normal_state"] if state == "normal" else (state or door["state"])
    if lock == "normal":
        door["lock_override"] = None
        target_lock = scheduled_lock(door, week_minute) if week_minute is not None \
            else door["lock_state"]
    elif lock is not None:
        door["lock_override"] = lock
        target_lock = lock
    else:
        target_lock = door["lock_state"]
    if target_lock == "locked" and door["state"] != "open" and state in (None, "normal"):
        target_state = "closed"   # a locked door stays shut ("normal" too)
    if target_lock == "locked" and state is None and door["state"] == "open":
        target_state = "closed"   # locking a door closes it
    if target_lock == "locked" and target_state == "open" and door["state"] != "open":
        raise ValueError(f"{door['id']} is locked: unlock it to open it")
    changed = []
    if target_state != door["state"]:
        door["state"] = target_state
        changed.append("state")
    if target_lock != door["lock_state"]:
        door["lock_state"] = target_lock
        changed.append("lock_state")
    return changed


def snapshot(doors):
    """BuildSim's /api/doors payload (our bookkeeping fields removed)."""
    keep = ("id", "name", "kind", "level", "room", "entry_node_id", "state", "lock_state")
    return [{k: d[k] for k in keep if k in d} for d in doors.values()]


def has_equipment(door):
    return door["kind"] == "fire_exit" or door.get("access") in ("stair", "secure")


def door_equipment(door):
    return {
        "id": f"door-{door['id']}", "name": door["name"], "type": "door_lock",
        "category": "access_control", "level": door["level"], "room": door["room"],
        "status": "running",
        "sensors": [{"id": f"{door['id']}-position", "name": "Door position",
                     "type": "door_position", "data_type": "binary",
                     "binary_value": door["state"] == "open"}],
        "actuators": [{"id": f"{door['id']}-lock", "name": "Lock", "type": "door_lock",
                       "state": door["lock_state"]}],
    }


class DoorGateway:
    def __init__(self, name, topo):
        self.name = name
        self.topo = topo
        self.doors = build_doors(topo)
        self.lock = threading.Lock()
        self.commands = queue.Queue()
        self.dirty = True
        self.pending = []   # confirmations waiting for a successful PUT
        self.clock = simclock.Follower()
        self.equipment_ready = False
        self.equipment_dirty = set()   # door ids whose BuildSim equipment needs updating
        self.scenario_id = None
        self.life = lifecycle.Lifecycle(name, status_topic=mqtt_bus.gateway_status_topic(name))
        self.bus = self.life.bus
        self.bus.subscribe(DOOR_SET_SUB, self.on_message)
        self.bus.subscribe(DOOR_FAULT_SUB, self.on_message)
        self.bus.subscribe(ZONE_DOORS_SUB, self.on_message)
        self.bus.subscribe(LEVEL_DOORS_SUB, self.on_message)
        self.bus.subscribe(LIFTS_TOPIC, self.on_message)
        self.bus.subscribe(EGRESS_TOPIC, self.on_message)
        self.bus.subscribe(mqtt_bus.SCENARIO_TOPIC, self.on_scenario)
        self.bus.subscribe(mqtt_bus.CLOCK_TOPIC, self.clock.on_message, qos=0)

    def on_message(self, topic, payload):
        self.commands.put((topic, payload))

    def on_scenario(self, topic, payload):
        if isinstance(payload, dict) and payload.get("id") != self.scenario_id:
            self.commands.put(("__scenario__", payload))

    # -- command handling -----------------------------------------------------

    def targets(self, topic):
        parts = [mqtt_bus.dec(p) for p in topic.split("/")]
        if topic == LIFTS_TOPIC:
            return [d for d in self.doors.values() if d.get("lift")]
        if topic == EGRESS_TOPIC:
            return [d for d in self.doors.values() if d.get("access") in ("stair", "office")]
        if len(parts) == 4 and parts[1] == "doors":
            return [self.doors[parts[2]]] if parts[2] in self.doors else []
        if len(parts) == 5 and parts[3] == "doors":        # building/<level>/<room>/doors/set
            zone = topology.key(parts[1], parts[2])
            return [d for d in self.doors.values() if zone in d["zones"]]
        if len(parts) == 4 and parts[2] == "doors":        # building/<level>/doors/set
            return [d for d in self.doors.values() if d["level"] == parts[1]
                    and d["kind"] == "door" and d.get("fire_door")]
        return []

    def handle(self, topic, payload):
        if topic == "__scenario__":
            self.scenario_id = payload.get("id")
            with self.lock:
                self.doors = build_doors(self.topo, payload, self.clock.week_minute())
                self.dirty = True
            print(f"scenario {self.scenario_id}: {sum(d['state'] == 'open' for d in self.doors.values())}"
                  f" of {len(self.doors)} doors open", flush=True)
            for door in self.doors.values():
                self.pending.append((door["id"], None, "scenario start", ""))
            return
        command = payload if isinstance(payload, dict) else {}
        cid, reason, source = command.get("id"), command.get("reason", ""), command.get("source", "")
        doors = self.targets(topic)
        if topic.endswith("/fault"):
            for door in doors:
                with self.lock:
                    door["lock_state"] = "jammed" if command.get("jammed") else "unlocked"
                    self.dirty = True
                self.pending.append((door["id"], cid, "fault injected" if command.get("jammed")
                                     else "fault cleared", ""))
            return
        try:
            if not doors:
                raise ValueError(f"no door matches {topic}")
            if source not in COMMAND_SOURCES:
                raise ValueError(f"source {source!r} may not operate doors")
        except ValueError as exc:
            self.reject(topic, command, str(exc))
            return
        refused = []
        for door in doors:
            with self.lock:
                try:
                    changed = apply_command(door, command, self.clock.week_minute())
                except ValueError as exc:
                    refused.append(f"{door['id']}: {exc}")
                    continue
                if changed:
                    self.dirty = True
            if changed:
                self.pending.append((door["id"], cid, reason, source))
        if refused:
            self.reject(topic, command, "; ".join(refused[:5]) +
                        (f" (+{len(refused) - 5} more)" if len(refused) > 5 else ""))

    def reject(self, topic, command, why):
        print(f"REJECTED {topic}: {why}", flush=True)
        self.bus.publish(mqtt_bus.EVENTS_TOPIC, mqtt_bus.event(
            "door_command_rejected", why, severity="warning", topic=topic,
            command_id=command.get("id"), source=command.get("source"),
            requested={k: command.get(k) for k in ("state", "lock_state")}), qos=1)

    def confirm(self, door, command_id, reason, source=""):
        self.bus.publish(door_state_topic(door["id"]), {
            "state": door["state"], "lock_state": door["lock_state"], "kind": door["kind"],
            "level": door["level"], "room": door["room"], "ts": time.time(),
            "command_id": command_id, "reason": reason, "source": source,
        }, qos=1, retain=True)
        if command_id or reason not in ("scenario start",):
            self.bus.publish(mqtt_bus.EVENTS_TOPIC, mqtt_bus.event(
                "door_applied", f"{door['id']}: {door['state']}/{door['lock_state']} ({reason})",
                door["level"], door["room"], severity="info", door=door["id"],
                state=door["state"], lock_state=door["lock_state"], command_id=command_id,
                source=source), qos=1)

    # -- BuildSim ---------------------------------------------------------------

    def publish_doors(self):
        with self.lock:
            body = snapshot(self.doors)
            self.dirty = False
        try:
            common.call("PUT", "/api/doors", body)
            self.life.ok()
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.dirty = True
            self.life.degraded(f"cannot write doors to BuildSim: {exc}")
            return False
        pending, self.pending = self.pending, []
        for door_id, cid, reason, source in pending:
            if door_id in self.doors:
                self.confirm(self.doors[door_id], cid, reason, source)
                if has_equipment(self.doors[door_id]):
                    self.equipment_dirty.add(door_id)
        return True

    # -- access-control schedule and door equipment ---------------------------

    def follow_schedule(self):
        minute = self.clock.week_minute()
        with self.lock:
            changed = [d for d in self.doors.values() if apply_schedule(d, minute)]
            if changed:
                self.dirty = True
        for door in changed:
            self.pending.append((door["id"], None, f"access control ({simclock.label(minute)})",
                                 "schedule"))
        if changed:
            print(f"{simclock.label(minute)}: access control changed {len(changed)} doors",
                  flush=True)

    def sync_equipment(self):
        """Register the door_lock equipment once (and after a BuildSim
        restart), then push changed positions and lock states."""
        if not DOOR_EQUIPMENT:
            return
        import urllib.error
        import urllib.parse
        try:
            if not self.equipment_ready:
                items = [door_equipment(d) for d in self.doors.values() if has_equipment(d)]
                for i in range(0, len(items), 150):
                    common.call("POST", "/api/equipment/bulk", items[i:i + 150])
                self.equipment_ready = True
                self.equipment_dirty = {d["id"] for d in self.doors.values() if has_equipment(d)}
                print(f"registered {len(items)} door_lock equipment items", flush=True)
            for door_id in list(self.equipment_dirty)[:100]:
                door = self.doors.get(door_id)
                self.equipment_dirty.discard(door_id)
                if door is None:
                    continue
                q = urllib.parse.quote
                common.call("PUT", f"/api/sensors/{q(door_id + '-position', safe='')}/value",
                            {"data_type": "binary", "binary_value": door["state"] == "open"})
                common.call("PUT", f"/api/actuators/{q(door_id + '-lock', safe='')}/state",
                            {"state": door["lock_state"]})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self.equipment_ready = False   # BuildSim restarted
        except Exception as exc:  # noqa: BLE001
            print(f"door equipment update failed: {exc}", flush=True)

    def probe_equipment(self):
        """False when BuildSim has lost the door equipment (restart)."""
        if not DOOR_EQUIPMENT:
            return True
        door = next((d for d in self.doors.values() if has_equipment(d)), None)
        if door is None:
            return True
        try:
            common.call("GET", "/api/equipment/door-" + door["id"])
            return True
        except Exception:  # noqa: BLE001
            return False

    def run(self):
        self.life.start()
        time.sleep(1.0)   # the retained scenario arrives first
        self.life.running(f"{len(self.doors)} doors")
        last_publish = 0.0
        last_schedule = 0.0
        while not self.life.stopping:
            try:
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    try:
                        topic, payload = self.commands.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    self.handle(topic, payload)
                # A PUT replaces the whole collection: batch the changes, and
                # republish now and then in case BuildSim restarted.
                if time.monotonic() - last_schedule > 1.0:
                    last_schedule = time.monotonic()
                    self.follow_schedule()
                if self.dirty or time.monotonic() - last_publish > REPUBLISH_SECONDS:
                    if self.publish_doors():
                        if time.monotonic() - last_publish > REPUBLISH_SECONDS:
                            self.equipment_ready = self.equipment_ready and self.probe_equipment()
                        last_publish = time.monotonic()
                self.sync_equipment()
                self.life.beat()
            except Exception as exc:  # noqa: BLE001
                print(f"door gateway loop failed: {exc}", flush=True)
                time.sleep(1)
        self.life.stop()


def main():
    common.wait_until_healthy()
    name = os.environ.get("GATEWAY_NAME", "doors")
    DoorGateway(name, topology.Topology.load(common.BASE)).run()


if __name__ == "__main__":
    main()
