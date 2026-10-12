#!/usr/bin/env python3
"""Populate the building with people and evacuate them when a fire is detected.

Based on populate_building.py: walks the floors through the REST API and
creates the population in rooms on every level. Instead of equipment it
creates people (moving entities) and keeps running:

  normal       people sit in their rooms, shift around a little and now and
               then walk to another room on their floor and back again
  fire         fire detected -> nearest *safe* fire exit is calculated for
               everyone -> the route is displayed -> people move along it
  all clear    once the fire has been gone for a while, everyone walks back

Fire detection (--trigger):
  plan    (default when the MQTT broker is reachable) the building agent's
          evacuation plan (building/evacuation/plan, retained): the agent
          decides when to evacuate, which floors (phased: the alarm floor and
          the floors above first, every floor on a confirmed fire or after 5
          minutes), the fire, smoke and predicted-spread zones, and the
          all-clear. People on floors not (yet) being evacuated stay put.
  alerts  (default without a broker) the building agent's alerts: any
          "fire-<room>" alert with severity warning or critical
  layers  the fire simulator's ground truth (fire_phase room layer)
  manual  --fire-rooms A109[,A110] after --fire-at seconds, or a timeline
          with --events "5:fire:A1500B,20:fire:A1508,10:smoke:A1500A"
          (real seconds; no simulator needed, handy for demos)

Evacuation routing (on BuildSim's walkable graph, fetched from the API):
  * hazards are weighed, not blocked: every metre through a space with a
    confirmed fire counts as FIRE_FACTOR+1 metres, through a space with a
    fire alarm (smoke) as SMOKE_FACTOR+1 metres, and next to a burning
    room's door a little extra. A weighted shortest-path search over all
    floors then finds the best exit for everyone: people detour around a
    fire when they can, and take the least-exposed way when they can't.
    Zones the agent predicts the fire will spread to (risk) count RISK_FACTOR.
    Nobody is ever "trapped" by a label, only by a missing connection.
  * shelter in place: if the only way out of a room that is NOT burning
    passes more than --shelter-fire-metres through fire, people stay in
    the room with the door closed and wait for rescue (the standard advice).
    If their own room catches fire, they leave by the least-exposed way.
  * lifts are out of service (never routed; lobbies shown as blocked doors)
    and people on upper floors take the stairs.
  * fire exits are published as doors; burning dead-end rooms are shown as
    blocked doors (no route passes through a dead end, so this can't cut
    anyone off). Corridors are never blocked.
  * routes are recalculated whenever the fire spreads or escalates.

Display (session highlights, in every open viewer):
  * red = fire, orange = smoke or predicted fire spread (lighter), green =
    safe: exits and the corridors used by the evacuation; purple = rooms
    with people sheltering or trapped (where rescuers must go)
  * each viewer shows one evacuee's remaining route (the session route API
    holds a single route). BuildSim's viewer flies the camera to every route
    it receives, so by default (--route-display once) one route is sent when
    the evacuation starts and the camera is left to the operator;
    --route-display cycle shows a different evacuee every --route-cycle
    seconds, --route-display off sends none
  * people show their status: "pre-movement", "evacuating to A119 (54 m)",
    "evacuated via A119 in 95 s", ...

Reports (with MQTT): building/evacuation/progress (retained, every 5 s
during an evacuation): people in pre-movement, evacuating, out, and awaiting
rescue per zone (the agent raises a critical alert for them). Every re-plan
cross-checks a few routes with BuildSim's own /api/graph/route (same walkable
graph and doors, hazards ignored) and logs any disagreement (route_check).

Movement is realistic in a simple way: a pre-movement delay after the alarm
(people take a while to react), 1.2 m/s walking, slower on stairs, and the
same TIME_SCALE as the fire simulator so people and fire move on one clock.

Daily schedule (--schedule on, the default; the clock starts at --start-time
on --day, default BUILDING_CLOCK_START, and runs at --time-scale; with MQTT
it follows the building clock the fire simulator publishes on sim/clock, so
people, door schedule and agent share one clock):
  * weekdays: everyone has an office; they arrive through a fire exit/entrance
    between 07:00 and 09:00 (centred on 08:00), go for lunch around 11:30-12:30
    (half to the floor's lunch room, half out of the building), wander to
    meeting rooms and colleagues during the day, and leave between 16:00 and
    18:00. Nights and weekends the building is empty.
  * room types from the floor plan: corridors; rooms of 25 m2 and more are
    meeting rooms; the largest room on each floor is the lunch room; the
    rest are offices (override with --room-types FILE.json, {"A109": "office"}).
  * people who are out of the building are not drawn and take no part in an
    evacuation; nobody arrives during one.
  * every 15 simulated minutes the number of people present is logged (and
    published as an "occupancy" event when MQTT is available).
--schedule off: everyone is in from the start, as before.

Doors (--doors):
  mqtt  (default when the broker is reachable) the door gateway owns BuildSim's
        doors: burning dead-end rooms are marked "blocked" through it (source
        "evacuation"); exits and lifts are the door gateway's and the agent's
  api   no door gateway: this script writes /api/doors itself (exits, lift
        lobbies, burning dead ends), as before
  off   no doors at all
Routing always honours BuildSim's door snapshot: a blocked, locked or jammed
door cannot be passed (except outwards from the room a person is in, as
panic hardware allows), and routes are re-planned when the doors change.

Usage:
    python3 populate_evacuation.py [BASE_URL] [options]
    python3 populate_evacuation.py http://127.0.0.1:9090 --people 40
    python3 populate_evacuation.py --trigger manual --fire-rooms A109 --fire-at 15

Requires: pip install requests (and paho-mqtt for --doors mqtt)
"""

import argparse
import json
import math
import os
import random
import sys
import time

import requests

# The fire-safety services' MQTT helper, when run from the repository or the
# devices image (/app).
for _path in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "devices"),
              "/app"):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.append(_path)
try:
    import mqtt_bus  # noqa: E402
except Exception:  # noqa: BLE001 - paho missing or not in the repository
    mqtt_bus = None
try:
    import simclock  # noqa: E402 - the building clock (devices/simclock.py)
except Exception:  # noqa: BLE001
    simclock = None

FIRST_NAMES = {
    "woman": ["Alice", "Maja", "Elsa", "Sara", "Ida", "Emma", "Nora", "Linnea",
              "Wilma", "Klara", "Saga", "Agnes", "Hanna", "Julia", "Ebba"],
    "man": ["Johan", "Erik", "Lars", "Oskar", "Lucas", "Hugo", "Elias", "Nils",
            "Axel", "Karl", "Viktor", "Anton", "Emil", "Filip", "Adam"],
}

EXIT_LEVEL = "level0"
DEFAULT_EXITS = "1575,A183,A119,A1405,A1015,A104"

# Route weighting: a metre through a hazard counts as (1 + factor) metres.
FIRE_FACTOR = 25.0        # confirmed fire in the space
SMOKE_FACTOR = 4.0        # fire alarm (smoke) in the space
RISK_FACTOR = 1.0         # the agent predicts fire spread into the space
NEAR_FACTOR = 1.5         # within NEAR_RADIUS of a burning room's door
NEAR_RADIUS = 12.0        # plan units
STAIR_WEIGHT = 20.0       # same cost as BuildSim's multi-floor graph
SEVERITY = {"risk": 1, "smoke": 2, "fire": 3}
ALARMING = ("smoke", "fire")   # "risk" re-routes but never starts an evacuation
# Dashboard colours (session highlights): red = fire, orange = smoke or
# predicted spread, green = safe (exits and the corridors people use).
COLOURS = {"fire": ("#ef4444", 0.85), "smoke": ("#f97316", 0.75), "risk": ("#fb923c", 0.45),
           "exit": ("#22c55e", 0.85), "safe": ("#86efac", 0.45), "refuge": ("#a855f7", 0.9)}
PROGRESS_SECONDS = 5.0    # real seconds between evacuation progress reports
ROUTE_CHECKS = 10         # BuildSim route cross-checks per re-plan (D6)
ROUTE_TOLERANCE = 0.05    # relative difference that counts as agreement
RISK_REPLAN_SECONDS = 10.0       # real seconds between re-plans for predicted spread alone
HIGHLIGHT_SECONDS = 3.0          # real seconds between viewer highlight updates
ROUTE_MIN_RESEND_SECONDS = 60.0   # --route-display once: real seconds between route updates
TRAPPED = "trapped"
SHELTERING = "sheltering"


# ---------------------------------------------------------------------------
# Daily schedule
# ---------------------------------------------------------------------------

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MEETING_ROOM_M2 = 25.0


def parse_clock(text):
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def fmt_clock(minute_of_week):
    day = int(minute_of_week // 1440) % 7
    m = minute_of_week % 1440
    return f"{DAYS[day].capitalize()} {int(m // 60):02d}:{int(m % 60):02d}"


class Schedule:
    """Simulated clock and every person's working day."""

    def __init__(self, start="07:30", day="mon", enabled=True):
        self.enabled = enabled
        self.origin = DAYS.index(day.lower()[:3]) * 1440 + parse_clock(start)

    def sync(self, week_minute, sim_time):
        """Follow the building clock: choose the origin so that
        minute(sim_time) == week_minute."""
        self.origin = (week_minute - sim_time / 60.0) % (7 * 1440)

    def minute(self, sim_time):
        """Minute of the week (0 = Monday 00:00)."""
        return (self.origin + sim_time / 60.0) % (7 * 1440)

    def day_minute(self, sim_time):
        return self.minute(sim_time) % 1440

    def day_index(self, sim_time):
        return int(self.minute(sim_time) // 1440)

    def is_workday(self, sim_time):
        return self.day_index(sim_time) < 5

    @staticmethod
    def plan_day(rng):
        """One person's working day, minutes after midnight."""
        arrive = min(max(rng.gauss(8 * 60, 30), 7 * 60), 9 * 60 + 30)
        lunch = rng.uniform(11 * 60 + 30, 12 * 60 + 30)
        return {
            "arrive": arrive,
            "lunch": lunch,
            "lunch_end": lunch + rng.uniform(30, 60),
            "lunch_out": rng.random() < 0.5,
            "leave": rng.uniform(16 * 60, 18 * 60),
        }

    def wants_in(self, plan, sim_time):
        """Should this person be in the building now?"""
        if not self.enabled:
            return True
        if not self.is_workday(sim_time):
            return False
        m = self.day_minute(sim_time)
        if plan["lunch_out"] and plan["lunch"] <= m < plan["lunch_end"]:
            return False
        return plan["arrive"] <= m < plan["leave"]

    def at_lunch(self, plan, sim_time):
        m = self.day_minute(sim_time)
        return self.enabled and plan["lunch"] <= m < plan["lunch_end"]


def room_types(building, overrides=None, units_per_metre=5.0):
    """{(level, name): "corridor" | "office" | "meeting" | "lunch"}."""
    overrides = overrides or {}
    types = {}
    for level in building.levels:
        rooms = building.rooms[level]
        candidates = [r for r in rooms.values() if r.get("type") != "corridor"]
        lunch = max(candidates, key=lambda r: r.get("area", 0))["name"] if candidates else None
        for name, room in rooms.items():
            area_m2 = room.get("area", 0) / units_per_metre ** 2
            if room.get("type") == "corridor":
                kind = "corridor"
            elif name == lunch:
                kind = "lunch"
            elif area_m2 >= MEETING_ROOM_M2:
                kind = "meeting"
            else:
                kind = "office"
            types[(level, name)] = overrides.get(f"{level}/{name}", overrides.get(name, kind))
    return types


# ---------------------------------------------------------------------------
# Small geometry helpers
# ---------------------------------------------------------------------------

def point_in_polygon(x, y, polygon):
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


# Floor page size per level ({level: (width, height)}): BuildSim rejects the
# whole entities PUT when one position lies outside the page, and a few rooms
# on the upper floors reach past the page edge.
PAGES = {}
PAGE_MARGIN = 0.5


def on_page(level, x, y):
    w, h = PAGES.get(level, (math.inf, math.inf))
    return PAGE_MARGIN <= x <= w - PAGE_MARGIN and PAGE_MARGIN <= y <= h - PAGE_MARGIN


def clamp_to_page(level, x, y):
    w, h = PAGES.get(level, (math.inf, math.inf))
    return (min(max(x, PAGE_MARGIN), w - PAGE_MARGIN),
            min(max(y, PAGE_MARGIN), h - PAGE_MARGIN))


def random_point_in_room(room, rng, margin=0.3, level=None):
    """Random point inside a room polygon (and on the floor page), biased
    towards the centre."""
    cx, cy = room["center"]
    polygon = room.get("polygon") or []
    if level is not None:
        cx, cy = clamp_to_page(level, cx, cy)
    if len(polygon) < 3:
        return cx, cy
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    for _ in range(30):
        x = cx + (rng.uniform(min(xs), max(xs)) - cx) * margin * 2
        y = cy + (rng.uniform(min(ys), max(ys)) - cy) * margin * 2
        if point_in_polygon(x, y, polygon) and (level is None or on_page(level, x, y)):
            return x, y
    return cx, cy


def path_length(path):
    total = 0.0
    for a, b in zip(path, path[1:]):
        if a["level"] == b["level"]:
            total += math.hypot(b["x"] - a["x"], b["y"] - a["y"])
    return total


# ---------------------------------------------------------------------------
# Building model (from the REST API)
# ---------------------------------------------------------------------------

class Building:
    def __init__(self, api, levels):
        self.api = api
        self.levels = levels
        self.rooms = {}        # level -> name -> room
        self.nodes = {}        # level -> id -> node
        self.room_node = {}    # level -> name -> node id (room node preferred)
        self.entries = {}      # level -> name -> [entry nodes]
        self.edges = {}        # level -> walkable edges
        self.stair_links = []  # ((level, name), (level, name)) stair connections
        for level in levels:
            floor = api.get(f"/api/building/floors/{level}")
            page = floor.get("page") or {}
            if page.get("width") and page.get("height"):
                PAGES[level] = (float(page["width"]), float(page["height"]))
            self.rooms[level] = {r["name"]: r for r in floor["rooms"]}
            graph = floor.get("walkable_graph") or api.get(
                f"/api/graph?level={level}&type=walkable")
            self.nodes[level] = {n["id"]: n for n in graph["nodes"]}
            self.edges[level] = graph["edges"]
            self.room_node[level] = {}
            self.entries[level] = {}
            rank = {"room": 0, "entry": 1, "corridor": 2}
            for node in graph["nodes"]:
                name = node["name"]
                best = self.room_node[level].get(name)
                if best is None or rank.get(node["type"], 3) < rank.get(
                        self.nodes[level][best]["type"], 3):
                    self.room_node[level][name] = node["id"]
                if node["type"] == "entry":
                    self.entries[level].setdefault(name, []).append(node)
            print(f"{level}: {len(self.rooms[level])} rooms, "
                  f"{len(self.nodes[level])} walkable nodes")

        # Stairs and lifts.
        self.stairs_down = {level: set() for level in levels}
        self.lift_pairs = set()
        stair_rooms, lift_rooms = set(), set()
        order = {level: i for i, level in enumerate(levels)}
        for edge in api.get("/api/building/cross-floor-edges") or []:
            a = (edge["from_level"].split("/")[-1], edge["from_name"])
            b = (edge["to_level"].split("/")[-1], edge["to_name"])
            if a[0] not in order or b[0] not in order:
                continue
            if edge.get("type") == "elevator":
                self.lift_pairs.add(frozenset((a, b)))
                lift_rooms.update((a, b))
                continue
            stair_rooms.update((a, b))
            self.stair_links.append((a, b))
            upper, lower = (a, b) if order[a[0]] > order[b[0]] else (b, a)
            if self.has_entry(*upper) and self.has_entry(*lower):
                self.stairs_down[upper[0]].add(upper[1])
        # Lift lobbies that are not also stairs and are real rooms.
        self.lift_lobbies = sorted(
            (lv, name) for lv, name in lift_rooms - stair_rooms
            if self.rooms.get(lv, {}).get(name, {}).get("type") == "room"
        )
        self.stair_rooms = stair_rooms

    def has_entry(self, level, name):
        return name in self.entries.get(level, {}) or (
            self.nodes.get(level) and any(
                n["name"] == name and n["type"] == "room"
                for n in self.nodes[level].values()))

    def is_room(self, level, name):
        return name in self.rooms.get(level, {})

    def nearest_node(self, level, x, y):
        return min(self.nodes[level].values(),
                   key=lambda n: (n["x"] - x) ** 2 + (n["y"] - y) ** 2)

    def door_points(self, rooms):
        """{level: [(x, y)]} of the doors of the given (level, name) rooms."""
        points = {}
        for level, name in rooms:
            for node in self.entries.get(level, {}).get(name, []):
                points.setdefault(level, []).append((node["x"], node["y"]))
        return points

    def is_dead_end(self, level, name):
        """A room with a single door: no route passes through it."""
        room = self.rooms.get(level, {}).get(name)
        return (room is not None and room.get("type") == "room"
                and len(self.entries.get(level, {}).get(name, [])) == 1
                and (level, name) not in self.stair_rooms)


# ---------------------------------------------------------------------------
# REST helper
# ---------------------------------------------------------------------------

class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.s = requests.Session()

    def get(self, path, quiet=False):
        resp = self.s.get(self.base + path, timeout=10)
        if resp.status_code == 404 and quiet:
            return None
        resp.raise_for_status()
        return resp.json()

    def put(self, path, body):
        resp = self.s.put(self.base + path, json=body, timeout=10)
        if resp.status_code >= 400:
            print(f"PUT {path} failed: {resp.status_code} {resp.text[:200]}")
        return resp

    def route_nodes(self, level, from_id, to_id):
        r = self.get(f"/api/graph/route?from={from_id}&to={to_id}"
                     f"&level={level}&type=walkable", quiet=True)
        if not r or not r.get("path"):
            return None
        for node in r["path"]:
            node["level"] = level
        return r["path"]

    def route_names(self, from_name, from_level, to_name, to_level):
        try:
            r = self.get(
                "/api/graph/route?type=walkable"
                f"&from_name={requests.utils.quote(from_name)}&from_level={from_level}"
                f"&to_name={requests.utils.quote(to_name)}&to_level={to_level}",
                quiet=True)
        except requests.HTTPError:
            return None
        if not r or not r.get("path"):
            return None
        for node in r["path"]:
            node["level"] = (node.get("level") or from_level).split("/")[-1]
        return r["path"]


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

class Person:
    def __init__(self, pid, name, icon, level, room, x, y):
        self.id = pid
        self.name = name
        self.icon = icon
        self.home = (level, room)
        self.level, self.room = level, room
        self.x, self.y = x, y
        self.heading = 0.0
        self.state = "idle"          # idle, trip, pre_movement, evacuating,
        self.status = "working"      # evacuated, trapped, returning
        self.path = []
        self.index = 0
        self.speed = 0.0
        self.stair_timer = 0.0
        self.next_action = 0.0
        self.alarm_time = None
        self.exit = None
        self.destination = None

    def entity(self, building, transition_ms):
        entity = {
            "id": self.id, "name": self.name, "type": self.icon,
            "level": self.level,
            "position": [round(v, 2) for v in clamp_to_page(self.level, self.x, self.y)],
            "heading": round(self.heading, 1), "status": self.status,
            "transition_ms": transition_ms,
        }
        if self.room and building.is_room(self.level, self.room):
            entity["room"] = self.room
        return entity

    def follow(self, path, speed, keep_stair=False):
        """Walk `path`. keep_stair: a re-plan while the person is on the
        stairs keeps the time already spent there (re-plans come every few
        seconds while the fire changes; resetting the timer each time kept
        people from ever reaching the next floor)."""
        self.path = path
        self.index = 0
        self.speed = speed
        if not keep_stair:
            self.stair_timer = 0.0

    def remaining_path(self):
        here = {"name": self.room or "", "level": self.level, "x": self.x,
                "y": self.y, "room_id": -1}
        return [here] + self.path[self.index:]

    def step(self, dt, stair_seconds):
        """Walk along the path for dt simulated seconds. True when done."""
        budget = self.speed * dt
        while self.index < len(self.path):
            target = self.path[self.index]
            if target["level"] != self.level:
                self.stair_timer += dt
                if self.stair_timer < stair_seconds:
                    return False
                self.stair_timer = 0.0
                self.level = target["level"]
                self.x, self.y = target["x"], target["y"]
                self.room = target.get("name")
                self.index += 1
                return False
            dx, dy = target["x"] - self.x, target["y"] - self.y
            dist = math.hypot(dx, dy)
            if dist > 1e-6:
                self.heading = math.degrees(math.atan2(dy, dx))
            if dist <= budget:
                self.x, self.y = target["x"], target["y"]
                self.room = target.get("name") or self.room
                budget -= dist
                self.index += 1
            else:
                self.x += dx / dist * budget
                self.y += dy / dist * budget
                return False
        return True


# ---------------------------------------------------------------------------
# Evacuation planning
# ---------------------------------------------------------------------------

class Plan:
    def __init__(self, cost, exit_name, path, fire_m, smoke_m, action, hazards_on_route):
        self.cost = cost
        self.exit = exit_name
        self.path = path
        self.fire_m = fire_m
        self.smoke_m = smoke_m
        self.action = action                      # "evacuate" or "shelter"
        self.hazards_on_route = hazards_on_route  # names of hazard spaces passed


class LocalRouter:
    """Hazard-weighted shortest paths over BuildSim's walkable graph.

    Same nodes, edges and stair links as BuildSim's multi-floor graph, but
    lifts are left out and edge costs grow with the hazard in the spaces they
    cross, so routes detour around fire and smoke instead of being blocked.
    """

    def __init__(self, building, exits, units_per_metre, shelter_fire_metres):
        self.b = building
        self.units = units_per_metre
        self.shelter_fire_metres = shelter_fire_metres
        self.exits = [e for e in exits if e in building.room_node.get(EXIT_LEVEL, {})]
        missing = set(exits) - set(self.exits)
        if missing:
            print(f"ignoring exits without walkable nodes: {sorted(missing)}")
        if not self.exits:
            sys.exit("no usable fire exits")

        self.adj = {}
        for level in building.levels:
            nodes = building.nodes[level]
            for edge in building.edges[level]:
                a, b = (level, edge["from"]), (level, edge["to"])
                w = edge.get("weight") or math.hypot(
                    nodes[edge["to"]]["x"] - nodes[edge["from"]]["x"],
                    nodes[edge["to"]]["y"] - nodes[edge["from"]]["y"])
                self.adj.setdefault(a, []).append((b, w))
                self.adj.setdefault(b, []).append((a, w))
        # Stairs attach where BuildSim attaches them: the room's entry node.
        attach = {}
        for level in building.levels:
            for node in building.nodes[level].values():
                if node["type"] in ("room", "entry"):
                    key = (level, node["name"])
                    if key not in attach or node["type"] == "entry":
                        attach[key] = (level, node["id"])
        for a, b in building.stair_links:
            if a in attach and b in attach:
                self.adj.setdefault(attach[a], []).append((attach[b], STAIR_WEIGHT))
                self.adj.setdefault(attach[b], []).append((attach[a], STAIR_WEIGHT))
        self.targets = {(EXIT_LEVEL, building.room_node[EXIT_LEVEL][e]): e for e in self.exits}

    def _factor(self, key, hazards, near):
        level, nid = key
        node = self.b.nodes[level][nid]
        severity = hazards.get((level, node["name"]))
        if severity == "fire":
            return FIRE_FACTOR
        if severity == "smoke":
            return SMOKE_FACTOR
        if severity == "risk":
            return RISK_FACTOR
        for x, y in near.get(level, ()):
            if math.hypot(node["x"] - x, node["y"] - y) < NEAR_RADIUS:
                return NEAR_FACTOR
        return 0.0

    def plan(self, level, start, hazards, blocked=None, exit_own_room=True):
        """Best Plan from a start node (walkable node dict), or None when no
        exit is physically reachable. blocked: {(level, node id)} entry nodes
        of blocked, locked or jammed doors (passable only outwards from the
        start room)."""
        import heapq

        blocked = {k for k in (blocked or ())
                   if not exit_own_room
                   or self.b.nodes[k[0]][k[1]]["name"] != start["name"] or k[0] != level}
        near = self.b.door_points([k for k, v in hazards.items() if v == "fire"])
        factors = {}

        def factor(key):
            if key not in factors:
                factors[key] = self._factor(key, hazards, near)
            return factors[key]

        source = (level, start["id"])
        dist, prev = {source: 0.0}, {}
        heap = [(0.0, source)]
        found = {}
        while heap and len(found) < len(self.targets):
            d, key = heapq.heappop(heap)
            if d > dist.get(key, math.inf):
                continue
            if key in self.targets:
                found[key] = d
            for other, w in self.adj.get(key, ()):
                if other in blocked:
                    continue
                cost = d + w * (1.0 + max(factor(key), factor(other)))
                if cost < dist.get(other, math.inf):
                    dist[other] = cost
                    prev[other] = key
                    heapq.heappush(heap, (cost, other))
        if not found:
            return None

        target = min(found, key=found.get)
        keys = [target]
        while keys[-1] != source:
            keys.append(prev[keys[-1]])
        keys.reverse()
        path = []
        for lv, nid in keys:
            node = self.b.nodes[lv][nid]
            path.append({"name": node["name"], "level": lv, "x": node["x"], "y": node["y"],
                         "room_id": nid, "type": node["type"]})

        # Exposure along the route, outside the space the person starts in.
        own = (level, start["name"])
        fire = smoke = 0.0
        passed = []
        for a, b in zip(path, path[1:]):
            if a["level"] != b["level"]:
                continue
            seg = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
            kinds = [hazards.get((n["level"], n["name"])) for n in (a, b)
                     if (n["level"], n["name"]) != own]
            if "fire" in kinds:
                fire += seg
            elif "smoke" in kinds:
                smoke += seg
            for n in (a, b):
                k = (n["level"], n["name"])
                if k != own and k in hazards and n["name"] not in passed:
                    passed.append(n["name"])

        own_fire = hazards.get(own) == "fire"
        fire_m, smoke_m = fire / self.units, smoke / self.units
        action = "evacuate"
        if start["type"] == "room" and not own_fire and fire_m > self.shelter_fire_metres:
            action = "shelter"
        return Plan(found[target], self.targets[target], path, fire_m, smoke_m, action, passed)


# ---------------------------------------------------------------------------
# Scenario controller
# ---------------------------------------------------------------------------

class Evacuation:
    def __init__(self, args):
        self.args = args
        self.api = Api(args.base)
        self.rng = random.Random(args.seed)
        levels = args.levels.split(",") if args.levels else [
            lv["id"] for lv in self.api.get("/api/building").get("levels", [])]
        self.b = Building(self.api, levels)
        self.router = LocalRouter(self.b, [e.strip() for e in args.exits.split(",") if e.strip()],
                                  args.units_per_metre, args.shelter_fire_metres)
        self.people = []
        self.hazards = {}   # (level, name) -> "fire" | "smoke"
        self.evacuating = False
        self.alarm_sim_time = None
        self.clear_since = None
        self.sim_time = 0.0
        self.route_cycle = 0
        self.shown_route = None       # (person id, exit) of the route on screen
        self.route_focus = None
        self.last_route_put = -math.inf
        self.last_risk_replan = -math.inf
        self.last_display = 0.0
        self.displayed = False
        self.complete = False
        self.walk = args.walk_speed * args.units_per_metre
        self.stroll = args.stroll_speed * args.units_per_metre
        self.started = time.monotonic()
        self.schedule = Schedule(args.start_time, args.day, args.schedule == "on")
        overrides = {}
        if args.room_types:
            with open(args.room_types, encoding="utf-8") as f:
                overrides = json.load(f)
        self.types = room_types(self.b, overrides, args.units_per_metre)
        self.blocked = set()          # {(level, entry node id)} from BuildSim's doors
        self.hard_blocked = set()     # the subset that is blocked or jammed (not just locked)
        self.door_signature = None
        self.last_door_read = 0.0
        self.last_occupancy_log = None
        self.blocked_dead_ends = set()
        self.evacuate_levels = None   # None = every floor (the plan may phase it)
        self.plan = None              # the agent's latest evacuation plan
        self.clock = None             # the building clock (sim/clock)
        self.clock_followed = False
        self.last_progress = 0.0
        self.route_checks = {"checked": 0, "agree": 0, "mismatch": 0}
        self.route_cache = {}
        self.bus = None
        self.door_mode = args.doors
        if args.doors in ("mqtt", "auto") or args.trigger in ("plan", "auto"):
            self.bus = self.connect_mqtt()
        if args.doors in ("mqtt", "auto"):
            self.door_mode = "mqtt" if self.bus else ("api" if args.doors == "auto" else "off")
            if args.doors == "mqtt" and not self.bus:
                print("WARNING: MQTT unavailable, doors are not operated")
        if args.trigger == "auto":
            args.trigger = "plan" if self.bus else "alerts"
        if args.trigger == "plan" and not self.bus:
            sys.exit("--trigger plan needs the MQTT broker (the agent's evacuation plan)")
        if self.bus:
            self.bus.subscribe(mqtt_bus.EVAC_PLAN_TOPIC, self._on_plan)
            if simclock is not None:
                self.clock = simclock.Follower(args.time_scale)
                self.bus.subscribe(mqtt_bus.CLOCK_TOPIC, self.clock.on_message, qos=0)
        print(f"trigger: {args.trigger}; doors: {self.door_mode}; schedule: {args.schedule} "
              f"(clock starts {fmt_clock(self.schedule.origin)}"
              f"{', follows the building clock' if self.clock else ''})")

    def _on_plan(self, topic, payload):
        if isinstance(payload, dict) and "phase" in payload:
            self.plan = payload

    def connect_mqtt(self):
        if mqtt_bus is None:
            print("MQTT helper/paho not available")
            return None
        try:
            import socket
            with socket.create_connection((mqtt_bus.MQTT_HOST, mqtt_bus.MQTT_PORT), timeout=2):
                pass
            bus = mqtt_bus.Bus("evacuation",
                               status_topic=mqtt_bus.service_status_topic("evacuation"))
            bus.start(wait=False)
            if not bus.connected.wait(5):
                print("MQTT login failed (set MQTT_USERNAME=evacuation and MQTT_PASSWORD)")
                bus.client.loop_stop()
                return None
            return bus
        except OSError as exc:
            print(f"no MQTT broker at {mqtt_bus.MQTT_HOST}:{mqtt_bus.MQTT_PORT} ({exc})")
            return None

    def event(self, type_, message, **data):
        if self.bus:
            self.bus.publish(mqtt_bus.EVENTS_TOPIC, mqtt_bus.event(type_, message, **data), qos=1)

    # -- population --------------------------------------------------------

    def placeable_rooms(self, level, kind=None):
        excluded = {name for lv, name in self.b.stair_rooms if lv == level}
        excluded |= {name for lv, name in self.b.lift_lobbies if lv == level}
        excluded |= set(self.router.exits) if level == EXIT_LEVEL else set()
        if kind:
            return [name for name in self.placeable_rooms(level)
                    if self.types.get((level, name)) == kind]
        return [
            name for name, room in self.b.rooms[level].items()
            if room.get("type") != "corridor" and name not in excluded
            and room.get("area", 0) >= 40
            and self.b.nodes[level].get(self.b.room_node[level].get(name, -1), {}).get("type") == "room"
        ]

    def populate(self):
        """Create people in rooms on every level (cf. populate_building.py)."""
        slots = []
        for level in self.b.levels:
            rooms = self.placeable_rooms(level)
            print(f"{level}: {len(rooms)} rooms people can be placed in")
            slots += [(level, room) for room in rooms]

        chosen = []
        for item in self.args.seed_rooms.split(","):
            item = item.strip()
            if not item:
                continue
            level, room = item.split("/", 1) if "/" in item else (EXIT_LEVEL, item)
            if (level, room) in slots:
                chosen += [(level, room)] * 2
        self.rng.shuffle(slots)
        while len(chosen) < self.args.people and slots:
            level, room = slots.pop()
            chosen += [(level, room)] * self.rng.choice([1, 1, 2, 3])
        chosen = chosen[: self.args.people]

        for i, (level, room) in enumerate(chosen):
            icon = self.rng.choice(["man", "woman"])
            x, y = random_point_in_room(self.b.rooms[level][room], self.rng, level=level)
            person = Person(f"person-{i + 1:03d}", self.rng.choice(FIRST_NAMES[icon]),
                            icon, level, room, x, y)
            person.next_action = self.rng.uniform(30, 600)
            person.day_plan = self.schedule.plan_day(self.rng)
            person.plan_day = self.schedule.day_index(0.0)
            if not self.schedule.wants_in(person.day_plan, 0.0):
                person.state, person.status = "away", "not in the building"
            self.people.append(person)
        present = sum(p.state != "away" for p in self.people)
        print(f"created {len(self.people)} people, {present} in the building at "
              f"{fmt_clock(self.schedule.origin)}")

    # -- fire detection ------------------------------------------------------

    @staticmethod
    def _key(item):
        item = item.strip()
        return tuple(item.split("/", 1)) if "/" in item else (EXIT_LEVEL, item)

    def detect_hazards(self):
        """{(level, name): "fire" | "smoke"} currently detected."""
        trigger = self.args.trigger
        hazards = {}

        def add(key, severity):
            if SEVERITY[severity] > SEVERITY.get(hazards.get(key), 0):
                hazards[key] = severity

        if trigger == "manual":
            elapsed = time.monotonic() - self.started
            events = self.args.events
            first = min((t for t, _, _ in events), default=0.0)
            if not (self.args.clear_after and elapsed >= first + self.args.clear_after):
                for t, severity, key in events:
                    if elapsed >= t:
                        add(key, severity)
        elif trigger == "plan":
            plan = self.plan or {}
            if plan.get("phase") == "evacuate":
                for zone in plan.get("risk_zones") or ():
                    add(self._key(zone), "risk")
                for zone in (plan.get("alarm_zones") or []) + (plan.get("smoke_zones") or []):
                    add(self._key(zone), "smoke")
                for zone in plan.get("fire_zones") or ():
                    add(self._key(zone), "fire")
        elif trigger == "layers":
            for layer in self.api.get("/api/room-layers") or []:
                values = layer.get("values") or {}
                if layer.get("id") == "fire_phase":
                    for key, value in values.items():
                        if 1 <= value <= 4:
                            add(tuple(key.split("/", 1)), "fire")
                elif layer.get("id") == "smoke_obscuration":
                    for key, value in values.items():
                        if value >= self.args.smoke_threshold:
                            add(tuple(key.split("/", 1)), "smoke")
        else:
            for alert in self.api.get("/api/alerts") or []:
                if str(alert.get("id", "")).startswith("fire-") and alert.get("room"):
                    severity = {"critical": "fire", "warning": "smoke"}.get(alert.get("severity"))
                    if severity:
                        add((alert.get("level") or EXIT_LEVEL, alert["room"]), severity)
        return hazards

    # -- doors -----------------------------------------------------------------

    def doors(self):
        if self.door_mode == "mqtt":
            # The door gateway owns BuildSim's doors: only ask it to mark the
            # burning dead ends (a route never passes through a dead end, so
            # this cannot cut anyone off; people inside can still get out).
            wanted = set()
            if self.evacuating:
                wanted = {(lv, name) for (lv, name), severity in self.hazards.items()
                          if severity == "fire" and self.b.is_dead_end(lv, name)}
            for level, name in sorted(wanted - self.blocked_dead_ends):
                self.door_command(level, name, "blocked", "fire, do not enter")
            for level, name in sorted(self.blocked_dead_ends - wanted):
                self.door_command(level, name, "normal", "all clear")
            self.blocked_dead_ends = wanted
            return
        if self.door_mode != "api":
            return
        doors = [{
            "id": f"fire-exit-{name}", "name": f"Fire exit {name}", "kind": "fire_exit",
            "level": EXIT_LEVEL, "room": name, "state": "open", "lock_state": "unlocked",
        } for name in self.router.exits]
        if self.evacuating:
            doors += [{
                "id": f"lift-{name}", "name": f"Lift lobby {name} (lifts recalled)",
                "kind": "door", "level": level, "room": name,
                "state": "blocked", "lock_state": "unlocked",
            } for level, name in self.b.lift_lobbies]
            doors += [{
                "id": f"fire-door-{name}", "name": f"{name}: fire, do not enter",
                "kind": "door", "level": level, "room": name,
                "state": "blocked", "lock_state": "unlocked",
            } for (level, name), severity in sorted(self.hazards.items())
              if severity == "fire" and self.b.is_dead_end(level, name)]
        self.api.put("/api/doors", doors)

    def door_command(self, level, name, state, reason):
        topic = (f"building/{mqtt_bus.enc(level)}/{mqtt_bus.enc(name)}/doors/set")
        self.bus.publish(topic, {"state": state, "reason": reason, "source": "evacuation",
                                 "id": f"evac-{int(time.time() * 1000)}", "ts": time.time()},
                         qos=1)
        print(f"  doors of {level}/{name}: {state} ({reason})")

    def read_doors(self):
        """Entry nodes BuildSim currently treats as impassable. True when
        the set changed."""
        try:
            doors = self.api.get("/api/doors") or []
        except requests.RequestException:
            return False
        blocked, hard = set(), set()
        for door in doors:
            if not door.get("blocked"):
                continue
            level = door.get("level")
            if level not in self.b.nodes:
                continue
            if door.get("entry_node_id") is not None:
                entries = {(level, int(door["entry_node_id"]))}
            elif door.get("room"):
                entries = {(level, n["id"]) for n in
                           self.b.entries.get(level, {}).get(door["room"], [])}
            else:
                continue
            blocked |= entries
            # A merely locked door stops nobody with a badge in everyday
            # life; blocked or jammed doors stop everyone.
            if door.get("state") == "blocked" or door.get("lock_state") == "jammed":
                hard |= entries
        changed = blocked != self.blocked
        self.blocked, self.hard_blocked = blocked, hard
        return changed

    def everyday_blocked(self):
        """Impassable entries outside an evacuation (badge holders open
        locked doors); in an evacuation every locked door counts."""
        return self.blocked if self.evacuating else self.hard_blocked

    # -- evacuation ------------------------------------------------------------

    def start_node(self, person):
        if person.state in ("idle", "pre_movement", "evacuated", TRAPPED, SHELTERING) \
                and person.room and person.room in self.b.room_node[person.level]:
            node = self.b.nodes[person.level][self.b.room_node[person.level][person.room]]
            if node["type"] == "room":
                return node
        if person.index < len(person.path) and person.path[person.index]["level"] == person.level:
            target = person.path[person.index]
            return self.b.nearest_node(person.level, target["x"], target["y"])
        return self.b.nearest_node(person.level, person.x, person.y)

    def plan_people(self, people):
        for person in people:
            start = self.start_node(person)
            plan = self.router.plan(person.level, start, self.hazards, self.blocked)
            if plan is None:
                person.state = TRAPPED
                person.status = "trapped: no physical way to an exit, awaiting rescue"
                person.path = []
                print(f"  {person.name} ({person.level}/{person.room}): NO ROUTE AT ALL")
                continue
            person.exit = plan.exit
            if plan.action == "shelter":
                if person.state != SHELTERING:
                    print(f"  {person.name} shelters in {person.room}: the only way out "
                          f"passes {plan.fire_m:.0f} m of fire ({', '.join(plan.hazards_on_route)})")
                person.state = SHELTERING
                person.path = []
                person.status = (f"sheltering in {person.room}, door closed: fire in "
                                 f"{', '.join(plan.hazards_on_route)} blocks the way out")
                continue
            person.follow(plan.path, self.walk,
                          keep_stair=person.state == "evacuating"
                          and getattr(person, "stair_timer", 0.0) > 0.0)
            if person.state != "pre_movement":
                person.state = "evacuating"
            note = ""
            if plan.fire_m > 0.5:
                note = f", through fire in {', '.join(plan.hazards_on_route)}"
            elif plan.smoke_m > 0.5:
                note = f", through smoke in {', '.join(plan.hazards_on_route)}"
            person.note = note
            metres = path_length(plan.path) / self.args.units_per_metre
            person.status = (f"evacuating to {plan.exit} ({metres:.0f} m{note})"
                             if person.state == "evacuating" else
                             f"alarm heard, leaving soon via {plan.exit}{note}")

    def in_scope(self, person):
        """Is the person's floor being evacuated (phased evacuation)?"""
        return self.evacuate_levels is None or person.level in self.evacuate_levels

    def plan_levels(self):
        """Floors to evacuate: the agent's plan, or every floor."""
        if self.args.trigger != "plan" or not self.plan:
            return None
        levels = self.plan.get("evacuate_levels")
        return frozenset(levels) if levels else None

    def alarm_active(self, hazards):
        if self.args.trigger == "plan":
            return (self.plan or {}).get("phase") == "evacuate"
        return any(sev in ALARMING for sev in hazards.values())

    def update_evacuation(self, hazards, levels=None):
        changed = sorted(k for k, v in hazards.items() if self.hazards.get(k) != v)
        first = not self.evacuating
        widened = levels != self.evacuate_levels
        self.hazards = hazards
        self.evacuate_levels = levels
        scope = "every floor" if levels is None else ", ".join(sorted(levels))
        if first:
            self.evacuating = True
            self.alarm_sim_time = self.sim_time
            present = sum(p.state != "away" for p in self.people)
            print(f"\nFIRE DETECTED: {self.describe(hazards)}; evacuating {scope} "
                  f"({present} people in the building)")
        elif widened:
            print(f"\nevacuation extended to {scope}")
        else:
            print(f"\nfire situation changed ({self.describe({k: hazards[k] for k in changed})}): "
                  f"re-planning routes")
        self.complete = False
        for person in self.people:
            if person.state in ("idle", "trip", "returning", "arriving", "leaving") \
                    and self.in_scope(person):
                lo, hi = self.args.premovement
                person.alarm_time = self.sim_time
                person.next_action = self.sim_time + self.rng.uniform(lo, hi)
                person.state = "pre_movement"
        self.doors()
        self.plan_people([p for p in self.people
                          if p.state in ("pre_movement", "evacuating", TRAPPED, SHELTERING)])
        self.check_routes()
        self.display(force=True)
        self.publish_progress(force=True)

    @staticmethod
    def describe(hazards):
        shown = {k: v for k, v in hazards.items() if v in ALARMING}
        text = ", ".join(f"{severity} in {name}" for (_, name), severity in sorted(shown.items()))
        risk = sum(v == "risk" for v in hazards.values())
        return text + (f" ({risk} zones at risk of spread)" if risk else "")

    # -- cross-check with BuildSim's route API (D6) -------------------------------

    def check_routes(self):
        """Compare the first same-floor leg of a few evacuees' *unweighted*
        routes (hazards ignored, same door snapshot) with BuildSim's own
        /api/graph/route. Both search the same walkable graph, so they must
        agree; a mismatch means the local graph or door handling drifted from
        BuildSim's and is logged (and published as a route_check event)."""
        mismatches = []
        checked = 0
        for person in self.people:
            if checked >= ROUTE_CHECKS:
                break
            if person.state not in ("pre_movement", "evacuating") or not person.path:
                continue
            here = self.start_node(person)
            plan = self.router.plan(person.level, here, {}, self.blocked)
            if plan is None:
                continue
            # Compare from the first node outside the person's own room: people
            # leave their own room through a locked door (panic hardware),
            # BuildSim's route API does not model that.
            leg = [n for n in plan.path if n["level"] == person.level]
            while len(leg) > 2 and leg[0]["name"] == here["name"]:
                leg = leg[1:]
            if len(leg) < 2 or leg[0]["name"] == here["name"]:
                continue
            start = {"id": leg[0]["room_id"], "name": leg[0]["name"]}
            end = leg[-1]
            key = (person.level, start["id"], end["room_id"], frozenset(self.blocked))
            if key in self.route_cache or end["room_id"] == start["id"]:
                continue
            checked += 1
            local_m = path_length(leg) / self.args.units_per_metre
            try:
                theirs = self.api.route_nodes(person.level, start["id"], end["room_id"])
            except requests.RequestException:
                theirs = None
            if theirs is None:
                result = ("mismatch", f"{person.level}: BuildSim has no route "
                          f"{start['name']} -> {end['name']}, local {local_m:.0f} m")
            else:
                their_m = path_length(theirs) / self.args.units_per_metre
                ok = abs(their_m - local_m) <= max(1.0, ROUTE_TOLERANCE * local_m)
                result = ("agree" if ok else "mismatch",
                          f"{person.level}: {start['name']} -> {end['name']} local {local_m:.0f} m, "
                          f"BuildSim {their_m:.0f} m")
            self.route_cache[key] = result
            self.route_checks["checked"] += 1
            self.route_checks[result[0]] += 1
            if result[0] == "mismatch":
                mismatches.append(result[1])
        for text in mismatches:
            print(f"  ROUTE CHECK mismatch: {text}")
        if checked:
            self.event("route_check", f"{checked} routes checked against BuildSim, "
                       f"{len(mismatches)} mismatches", checked=checked,
                       mismatches=len(mismatches), details=mismatches[:5],
                       totals=dict(self.route_checks))

    # -- progress for the agent and the dashboard ---------------------------------

    def awaiting_rescue(self):
        waiting = {}
        for p in self.people:
            if p.state in (TRAPPED, SHELTERING):
                zone = f"{p.level}/{p.room}"
                waiting[zone] = waiting.get(zone, 0) + 1
        return waiting

    def publish_progress(self, force=False):
        """building/evacuation/progress (retained): how the evacuation is going;
        the agent raises a critical alert for people awaiting rescue."""
        if not self.bus:
            return
        now = time.monotonic()
        if not force and now - self.last_progress < PROGRESS_SECONDS:
            return
        self.last_progress = now
        counts = {}
        for p in self.people:
            counts[p.state] = counts.get(p.state, 0) + 1
        times = [p.evac_time for p in self.people if p.state == "evacuated"
                 and getattr(p, "evac_time", None) is not None]
        self.bus.publish(mqtt_bus.EVAC_PROGRESS_TOPIC, {
            "phase": "evacuate" if self.evacuating else "normal",
            "plan_version": (self.plan or {}).get("version"),
            "levels": sorted(self.evacuate_levels) if self.evacuate_levels else "all",
            "present": len(self.people) - counts.get("away", 0),
            "pre_movement": counts.get("pre_movement", 0),
            "evacuating": counts.get("evacuating", 0),
            "evacuated": counts.get("evacuated", 0),
            "awaiting_rescue": self.awaiting_rescue() if self.evacuating else {},
            "last_out_s": round(max(times), 1) if times else None,
            "route_checks": dict(self.route_checks),
            "clock": fmt_clock(self.schedule.minute(self.sim_time)),
            "ts": time.time()}, qos=1, retain=True)

    def all_clear(self):
        print("\nALL CLEAR: people return to their rooms")
        self.evacuating = False
        self.complete = False
        self.hazards = {}
        self.evacuate_levels = None
        self.doors()
        times = [p.evac_time for p in self.people if getattr(p, "evac_time", None) is not None]
        if times:
            print(f"  evacuated {len(times)}/{len(self.people)}, "
                  f"mean {sum(times) / len(times):.0f} s, last out after {max(times):.0f} s (simulated)")
        for person in self.people:
            if person.state in ("evacuated", "evacuating", "pre_movement", TRAPPED, SHELTERING):
                if self.schedule.wants_in(person.day_plan, self.sim_time):
                    self.send_home(person)
                else:
                    self.go_away(person)
        self.clear_display()
        self.publish_progress(force=True)

    def send_home(self, person):
        level, room = person.home
        path = None
        if person.level == level and person.room in self.b.room_node[level]:
            start = self.start_node(person)
            path = self.api.route_nodes(level, start["id"], self.b.room_node[level][room])
        if path is None and person.room:
            path = self.api.route_names(person.room, person.level, room, level)
        if path is None:
            person.state, person.status = "idle", "working"
            return
        person.follow(path, self.stroll)
        person.state = "returning"
        person.status = f"returning to {room}"

    # -- arriving and leaving (daily schedule) ----------------------------------

    def exit_path(self, person):
        """Path from where the person is to the nearest exit (no hazards)."""
        plan = self.router.plan(person.level, self.start_node(person), {},
                                self.everyday_blocked())
        return plan.path if plan else None

    def go_away(self, person):
        path = self.exit_path(person)
        if not path:
            person.state, person.status = "away", "not in the building"
            return
        person.follow(path, self.stroll)
        person.state = "leaving"
        person.status = "going home" if not self.schedule.at_lunch(
            person.day_plan, self.sim_time) else "out for lunch"

    def arrive(self, person):
        """Walk in from the nearest exit to the person's office."""
        level, room = person.home
        node = self.b.nodes[level][self.b.room_node[level][room]]
        plan = self.router.plan(level, node, {}, self.everyday_blocked())
        if not plan:
            return
        path = list(reversed(plan.path))
        first = path[0]
        person.level, person.room = first["level"], first["name"]
        person.x, person.y = first["x"], first["y"]
        person.follow(path[1:], self.stroll)
        person.state, person.status = "arriving", f"arriving, walking to {room}"
        person.destination = None

    def daily(self, person):
        """Arrivals, lunch and departures from the person's working day."""
        day = self.schedule.day_index(self.sim_time)
        if getattr(person, "plan_day", None) != day:
            person.day_plan = self.schedule.plan_day(self.rng)
            person.plan_day = day
        wants_in = self.schedule.wants_in(person.day_plan, self.sim_time)
        if person.state == "away":
            if wants_in:
                self.arrive(person)
            return True
        if not wants_in and person.state in ("idle", "trip", "returning"):
            self.go_away(person)
            return True
        if person.state == "idle" and person.room == person.home[1] and \
                self.schedule.at_lunch(person.day_plan, self.sim_time) and \
                not getattr(person, "had_lunch", None) == day:
            person.had_lunch = day
            rooms = self.placeable_rooms(person.level, "lunch")
            if rooms:
                start = self.start_node(person)
                path = self.api.route_nodes(person.level, start["id"],
                                            self.b.room_node[person.level][rooms[0]])
                if path:
                    person.follow(path, self.stroll)
                    person.state, person.destination = "trip", rooms[0]
                    person.status = f"lunch in {rooms[0]}"
                    person.next_action = self.sim_time + (
                        person.day_plan["lunch_end"] - self.schedule.day_minute(self.sim_time)) * 60
                    return True
        return False

    def occupancy_log(self):
        """Every 15 simulated minutes: how many people are in the building."""
        slot = int(self.schedule.minute(self.sim_time) // 15)
        if slot == self.last_occupancy_log:
            return
        self.last_occupancy_log = slot
        present = [p for p in self.people if p.state != "away"]
        by_type = {}
        for p in present:
            kind = self.types.get((p.level, p.room), "corridor")
            by_type[kind] = by_type.get(kind, 0) + 1
        clock = fmt_clock(self.schedule.minute(self.sim_time))
        print(f"[{clock}] {len(present)}/{len(self.people)} people in the building {by_type}")
        self.event("occupancy", f"{clock}: {len(present)} people in the building",
                   present=len(present), total=len(self.people), clock=clock, by_type=by_type)

    # -- everyday movement -----------------------------------------------------

    def everyday(self, person):
        if self.sim_time < person.next_action:
            return
        level = person.level
        if person.state == "idle" and not self.args.no_wander and self.rng.random() < 0.35:
            # Walk to another room on the floor (meeting, coffee, printer).
            options = self.placeable_rooms(level)
            destination = self.rng.choice(options)
            start = self.start_node(person)
            target = self.b.room_node[level][destination]
            path = self.api.route_nodes(level, start["id"], target)
            if path and path_length(path) < 250:
                person.follow(path, self.stroll)
                person.state = "trip"
                person.destination = destination
                person.status = f"walking to {destination}"
                return
        # Otherwise move a little within the room.
        room = self.b.rooms[level].get(person.room)
        if room and person.state == "idle":
            x, y = random_point_in_room(room, self.rng, level=level)
            person.follow([{"name": person.room, "level": level, "x": x, "y": y}], self.stroll / 2)
        person.next_action = self.sim_time + self.rng.uniform(60, 600)

    # -- per-tick update --------------------------------------------------------

    def step_people(self, dt):
        for person in self.people:
            if person.state == "pre_movement":
                if self.sim_time >= person.next_action:
                    person.state = "evacuating"
                    person.status = f"evacuating to {person.exit}{getattr(person, 'note', '')}"
                continue
            if person.state in ("evacuated", TRAPPED, SHELTERING, "away"):
                continue
            done = person.step(dt, self.args.stair_seconds)
            if person.state == "evacuating" and person.path:
                left = path_length(person.remaining_path()) / self.args.units_per_metre
                person.status = (f"evacuating to {person.exit} ({left:.0f} m"
                                 f"{getattr(person, 'note', '')})")
            if not done:
                continue
            if person.state == "evacuating":
                person.state = "evacuated"
                person.evac_time = self.sim_time - self.alarm_sim_time
                person.status = f"evacuated via {person.exit} in {person.evac_time:.0f} s"
                print(f"  {person.name} out via {person.exit} after {person.evac_time:.0f} s")
            elif person.state == "trip":
                person.state = "idle"
                person.status = f"in {person.destination}"
                # Lunch lasts until its planned end; other visits a few minutes.
                person.next_action = max(person.next_action if person.status.startswith(
                    "in ") and self.types.get((person.level, person.destination)) == "lunch"
                    else 0.0, self.sim_time + self.rng.uniform(120, 600))
                person.destination = None
            elif person.state in ("returning", "arriving"):
                person.state = "idle"
                person.room = person.home[1]
                person.status = "working"
                person.next_action = self.sim_time + self.rng.uniform(60, 300)
            elif person.state == "leaving":
                person.state, person.status = "away", "not in the building"
                person.path = []
            elif person.state == "idle":
                person.path = []
        if not self.evacuating:
            for person in self.people:
                if person.state in ("away", "idle", "trip", "returning") and self.daily(person):
                    continue
                if person.state == "idle" and person.index >= len(person.path):
                    if person.room != person.home[1] and person.destination is None \
                            and self.sim_time >= person.next_action:
                        self.send_home(person)
                    else:
                        self.everyday(person)

    # -- display -----------------------------------------------------------------

    def sessions(self):
        try:
            return [s["id"] for s in self.api.get("/api/sessions") or []]
        except requests.RequestException:
            return []

    def display(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_display < HIGHLIGHT_SECONDS:
            return
        self.last_display = now
        evacuees = [p for p in self.people if p.state in ("pre_movement", "evacuating")]
        def mark(level, name, kind):
            colour, opacity = COLOURS[kind]
            return {"level": level, "room": name, "color": colour, "opacity": opacity}

        highlights = [mark(lv, name, sev) for (lv, name), sev in sorted(self.hazards.items())
                      if self.b.is_room(lv, name)]
        highlights += [mark(EXIT_LEVEL, name, "exit")
                       for name in self.router.exits if (EXIT_LEVEL, name) not in self.hazards]
        # Rooms with people sheltering in place: where rescuers must go.
        refuges = sorted({(p.level, p.room) for p in self.people
                          if p.state in (SHELTERING, TRAPPED) and self.b.is_room(p.level, p.room)})
        highlights += [mark(lv, name, "refuge") for lv, name in refuges if (lv, name) not in self.hazards]
        corridors = set()
        for p in evacuees:
            for node in p.path[p.index:]:
                key = (node["level"], node.get("name"))
                room = self.b.rooms.get(key[0], {}).get(key[1])
                if room and room.get("type") == "corridor" and key not in self.hazards:
                    corridors.add(key)
        highlights += [mark(lv, name, "safe") for lv, name in sorted(corridors)]

        route, clear = self.route_to_show(evacuees, now)
        for session in self.sessions():
            self.api.put(f"/api/sessions/{session}/highlights", highlights)
            if route is not None:
                self.api.put(f"/api/sessions/{session}/route", route)
            elif clear:
                self.api.put(f"/api/sessions/{session}/route", {"path": [], "distance": 0})
        self.displayed = True

    def route_to_show(self, evacuees, now):
        """(route to PUT or None, clear the route?).

        BuildSim's viewer treats every remotely sent route as "show me this":
        it switches to 3D and flies the camera to frame the route. Sending a
        new route every few seconds therefore takes the camera away from the
        operator. --route-display decides how often that may happen:
          once   (default) one representative route (the evacuee with the
                 longest way out) when the evacuation starts, sent again only
                 if that person's exit changes, at most every
                 ROUTE_MIN_RESEND_SECONDS; the camera is otherwise free
          cycle  a different evacuee every --route-cycle seconds (demo mode)
          off    never send routes (highlights only)
        """
        mode = self.args.route_display
        if not evacuees:
            clear = self.shown_route is not None
            self.shown_route = None
            self.route_focus = None
            return None, clear
        if mode == "off":
            return None, False
        if mode == "cycle":
            if now - self.last_route_put < self.args.route_cycle:
                return None, False
            focus = evacuees[self.route_cycle % len(evacuees)]
            self.route_cycle += 1
        else:
            focus = next((p for p in evacuees if p.id == self.route_focus), None)
            if focus is None:
                focus = max(evacuees, key=lambda p: path_length(p.remaining_path()))
            key = (focus.id, getattr(focus, "exit", None))
            if key == self.shown_route or (
                    self.shown_route is not None
                    and now - self.last_route_put < ROUTE_MIN_RESEND_SECONDS):
                return None, False
            self.shown_route = key
            self.route_focus = focus.id
        path = focus.remaining_path()
        if len(path) < 2:
            return None, False
        self.last_route_put = now
        if mode == "cycle":
            self.shown_route = (focus.id, getattr(focus, "exit", None))
        return {"path": [{"room_id": n.get("room_id", -1), "name": n.get("name", ""),
                          "level": n["level"], "x": n["x"], "y": n["y"]} for n in path],
                "distance": path_length(path)}, False

    def clear_display(self):
        for session in self.sessions():
            self.api.put(f"/api/sessions/{session}/highlights", [])
            self.api.put(f"/api/sessions/{session}/route", {"path": [], "distance": 0})
        self.displayed = False

    def publish(self):
        transition = int(self.args.tick * 1000) + 100
        self.api.put("/api/entities", [p.entity(self.b, transition) for p in self.people
                                       if p.state != "away"])

    def follow_clock(self):
        """P3: one clock. Align the daily schedule with the building clock
        the fire simulator publishes, whenever it is available."""
        if self.clock is None or not self.clock.following:
            return
        self.schedule.sync(self.clock.week_minute(), self.sim_time)
        if not self.clock_followed:
            self.clock_followed = True
            print(f"following the building clock: {self.clock.label()}")

    @staticmethod
    def heartbeat():
        try:
            with open(os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat"), "w") as f:
                f.write(f"{time.time():.0f}\n")
        except OSError:
            pass

    # -- main loop ---------------------------------------------------------------

    def run(self):
        self.follow_clock()
        self.populate()
        self.doors()
        self.publish()
        self.publish_progress(force=True)
        print(f"\nrunning (trigger={self.args.trigger}, time scale x{self.args.time_scale}); Ctrl+C to stop")
        last = time.monotonic()
        while True:
            try:
                now = time.monotonic()
                dt = (now - last) * self.args.time_scale
                last = now
                self.sim_time += dt

                self.follow_clock()
                hazards = self.detect_hazards()
                alarm = self.alarm_active(hazards)
                # Fire and smoke only grow during an evacuation (a fire that
                # is out still leaves smoke and damage) until the all-clear;
                # predicted spread ("risk") follows the agent's latest estimate.
                merged = {k: v for k, v in self.hazards.items() if v in ALARMING} \
                    if self.evacuating else {}
                for key, severity in hazards.items():
                    if SEVERITY[severity] > SEVERITY.get(merged.get(key), 0):
                        merged[key] = severity
                levels = self.plan_levels()
                alarming_changed = {k: v for k, v in merged.items() if v in ALARMING} != \
                    {k: v for k, v in self.hazards.items() if v in ALARMING}
                risk_due = time.monotonic() - self.last_risk_replan >= RISK_REPLAN_SECONDS
                if alarm and (not self.evacuating or alarming_changed
                              or levels != self.evacuate_levels
                              or (merged != self.hazards and risk_due)):
                    if merged != self.hazards and not alarming_changed:
                        self.last_risk_replan = time.monotonic()
                    self.update_evacuation(merged, levels)
                if self.evacuating:
                    if self.args.trigger == "plan":
                        # The agent owns the all-clear.
                        if (self.plan or {}).get("phase") in ("all_clear", "normal"):
                            self.all_clear()
                    elif alarm:
                        self.clear_since = None
                    elif self.clear_since is None:
                        self.clear_since = self.sim_time
                    elif self.sim_time - self.clear_since >= self.args.all_clear:
                        self.all_clear()

                # Doors: re-plan when BuildSim's door snapshot changes (a door
                # jammed or locked, a dead end marked blocked).
                if time.monotonic() - self.last_door_read > 2.0:
                    self.last_door_read = time.monotonic()
                    if self.read_doors() and self.evacuating:
                        print(f"\ndoors changed ({len(self.blocked)} entries impassable): "
                              f"re-planning routes")
                        self.event("evacuation_replan", "doors changed: routes re-planned",
                                   blocked_entries=len(self.blocked))
                        self.plan_people([p for p in self.people if p.state in (
                            "pre_movement", "evacuating", TRAPPED, SHELTERING)])
                        self.check_routes()

                self.step_people(dt)
                self.occupancy_log()
                self.publish()
                if self.evacuating:
                    self.publish_progress()
                if self.evacuating and not self.complete:
                    self.display()
                    if all(p.state in ("evacuated", TRAPPED, SHELTERING, "away")
                           or (not self.in_scope(p) and p.state not in ("pre_movement", "evacuating"))
                           for p in self.people):
                        self.complete = True
                        out = sum(p.state == "evacuated" for p in self.people)
                        times = [p.evac_time for p in self.people if p.state == "evacuated"]
                        waiting = {}
                        for p in self.people:
                            if p.state in (TRAPPED, SHELTERING):
                                waiting.setdefault(f"{p.level}/{p.room}", []).append(p.name)
                        print(f"\nEVACUATION COMPLETE: {out}/{len(self.people)} out"
                              + (f", last one after {max(times):.0f} s (simulated)" if times else ""))
                        for room, names in sorted(waiting.items()):
                            print(f"  awaiting rescue in {room}: {', '.join(names)}")
                        self.display(force=True)
                        self.publish_progress(force=True)
                        self.event("evacuation_complete",
                                   f"{out}/{len(self.people)} out", evacuated=out,
                                   waiting=sum(len(v) for v in waiting.values()),
                                   last_out_s=max(times) if times else None,
                                   mean_s=sum(times) / len(times) if times else None)
                self.heartbeat()
            except requests.RequestException as exc:
                print(f"tick failed: {exc}")
            time.sleep(self.args.tick)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Populate the building with people and evacuate them on fire.")
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:9090")
    parser.add_argument("--people", type=int, default=40)
    parser.add_argument("--levels", default="", help="default: all levels")
    parser.add_argument("--seed-rooms", default="A109,A110",
                        help="rooms that always get two people (level0 unless level/room)")
    parser.add_argument("--exits", default=DEFAULT_EXITS,
                        help="fire exit rooms on level0 (comma separated)")
    parser.add_argument("--trigger", choices=["auto", "plan", "alerts", "layers", "manual"],
                        default="auto",
                        help="auto = plan when the broker is reachable, else alerts")
    parser.add_argument("--fire-rooms", default="A109", help="manual trigger rooms")
    parser.add_argument("--events", default="",
                        help='manual timeline "SECONDS:fire|smoke:ROOM,...", e.g. '
                             '"5:fire:A1500B,20:fire:A1508" (overrides --fire-rooms)')
    parser.add_argument("--shelter-fire-metres", type=float, default=3.0,
                        help="shelter in place (non-burning rooms) when the best way out "
                             "passes more than this many metres of fire; large value = never")
    parser.add_argument("--smoke-threshold", type=float, default=10.0,
                        help="layers trigger: smoke obscuration (%%/m) treated as smoke hazard")
    parser.add_argument("--fire-at", type=float, default=20.0,
                        help="manual trigger: real seconds after start")
    parser.add_argument("--clear-after", type=float, default=0.0,
                        help="manual trigger: real seconds until the fire is out (0 = never)")
    parser.add_argument("--time-scale", type=float,
                        default=float(os.environ.get("TIME_SCALE", "5")),
                        help="simulated seconds per real second (match TIME_SCALE)")
    parser.add_argument("--tick", type=float, default=0.5, help="real seconds per update")
    parser.add_argument("--units-per-metre", type=float, default=5.0)
    parser.add_argument("--walk-speed", type=float, default=1.2, help="evacuation speed, m/s")
    parser.add_argument("--stroll-speed", type=float, default=0.9, help="everyday speed, m/s")
    parser.add_argument("--stair-seconds", type=float, default=20.0,
                        help="simulated seconds to go down one storey")
    parser.add_argument("--premovement", default="30:90",
                        help="min:max simulated seconds before people react to the alarm")
    parser.add_argument("--all-clear", type=float, default=120.0,
                        help="simulated seconds without fire before people return")
    parser.add_argument("--route-display", choices=["once", "cycle", "off"],
                        default=os.environ.get("ROUTE_DISPLAY", "once"),
                        help="evacuation route in the viewer: once (one route at the "
                             "start; the camera stays free), cycle (a different evacuee "
                             "every --route-cycle s; the viewer flies to each), off")
    parser.add_argument("--route-cycle", type=float, default=30.0,
                        help="real seconds per evacuee with --route-display cycle")
    parser.add_argument("--no-wander", action="store_true", help="people stay in their rooms")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--schedule", choices=["on", "off"], default="on",
                        help="daily schedule (arrivals, lunch, departures, empty nights)")
    start = os.environ.get("BUILDING_CLOCK_START", "mon 07:30").split()
    parser.add_argument("--start-time", default=start[-1],
                        help="clock at start, HH:MM (BUILDING_CLOCK_START; the building "
                             "clock on sim/clock overrides it when available)")
    parser.add_argument("--day", default=start[0] if len(start) > 1 else "mon",
                        help="weekday at start (mon..sun)")
    parser.add_argument("--room-types", default="",
                        help='JSON {"A109": "office", "level1/A2306": "meeting"}')
    parser.add_argument("--doors", choices=["auto", "mqtt", "api", "off"], default="auto",
                        help="who operates BuildSim's doors (see the module docstring)")
    args = parser.parse_args()
    lo, hi = (float(v) for v in args.premovement.split(":"))
    args.premovement = (lo, hi)
    events = []
    if args.events:
        for item in args.events.split(","):
            t, severity, room = item.strip().split(":", 2)
            if severity not in SEVERITY:
                parser.error(f"event severity must be fire, smoke or risk: {item}")
            events.append((float(t), severity, Evacuation._key(room)))
    else:
        events = [(args.fire_at, "fire", Evacuation._key(r))
                  for r in args.fire_rooms.split(",") if r.strip()]
    args.events = events
    return args


if __name__ == "__main__":
    try:
        Evacuation(parse_args()).run()
    except KeyboardInterrupt:
        print("\nstopped")
