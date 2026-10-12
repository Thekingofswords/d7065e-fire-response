"""Building topology for the whole building, derived from BuildSim's data.

Every room and corridor on every floor is a *zone*, keyed "level/name"
(for example "level0/A109"). Zones that share a doorway are *connected*:

* room <-> corridor      a room's entry node is adjacent to a corridor node
* room <-> room          two rooms' entry nodes are adjacent (inner doors)
* corridor <-> corridor  adjacent corridor segments with different names
                         (a wide opening rather than a door)

Geometry comes from the floor plan: floor area from the room polygon
(converted with PLAN_UNITS_PER_M), ceiling height from CEILING_HEIGHT_M.

Stairs (cross_floor_edges.json, type "stair") link zones on adjacent floors;
the fire model moves smoke up through them.

Corridors are split into *cells* of about CORRIDOR_CELL_M along their
walkable graph, for the physics only: smoke then travels along a corridor
instead of reaching every door at once. Each doorway records which cell of
the corridor it opens into. Sensors, actuators and the agent still work per
corridor (the fire model reports the worst cell).

Load it from a running BuildSim (Topology.from_api) or from the repository's
data folder (Topology.from_files, for offline dataset generation). The
result is plain data, so it can be cached as JSON.
"""
import json
import math
import os
import urllib.request

PLAN_UNITS_PER_M = float(os.environ.get("PLAN_UNITS_PER_M", "5"))
CEILING_HEIGHT_M = float(os.environ.get("CEILING_HEIGHT_M", "2.7"))
DOOR_WIDTH_M = float(os.environ.get("DOOR_WIDTH_M", "0.9"))
DOOR_HEIGHT_M = float(os.environ.get("DOOR_HEIGHT_M", "2.0"))
# Opening between two corridor segments.
OPENING_WIDTH_M = float(os.environ.get("CORRIDOR_OPENING_WIDTH_M", "1.8"))
OPENING_HEIGHT_M = float(os.environ.get("CORRIDOR_OPENING_HEIGHT_M", "2.4"))
MIN_AREA_M2 = 2.0
# Length of one corridor cell in the fire model (m).
CORRIDOR_CELL_M = float(os.environ.get("CORRIDOR_CELL_M", "4"))

# Which zones get fire sensors and sprinklers: "rooms" or "rooms+corridors".
SENSOR_SCOPE = os.environ.get("SENSOR_SCOPE", "rooms+corridors")


def key(level, name):
    return f"{level}/{name}"


def door_id(room_key, entry_node_id):
    """BuildSim door id of the door on a room's entry node."""
    level, name = split(room_key)
    return f"door-{level}-{name}-{entry_node_id}"


def split(zone_key):
    level, name = zone_key.split("/", 1)
    return level, name


class Zone:
    __slots__ = ("key", "level", "name", "type", "area", "height", "center")

    def __init__(self, level, name, zone_type, area, height, center):
        self.key = key(level, name)
        self.level = level
        self.name = name
        self.type = zone_type
        self.area = area
        self.height = height
        self.center = center

    @property
    def volume(self):
        return self.area * self.height

    def to_dict(self):
        return {"level": self.level, "name": self.name, "type": self.type,
                "area": self.area, "height": self.height, "center": self.center}


class Topology:
    def __init__(self, zones, connections, stairs=(), cells=None, cell_links=(),
                 conn_cells=None, conn_doors=None, lifts=()):
        """zones: {key: Zone}; connections: [(key_a, key_b, width, height)];
        stairs: [(lower key, upper key)].

        Fire-model detail for split corridors: cells {corridor key: [{"center",
        "share"}]}, cell_links [(corridor key, cell i, cell j, width, height)]
        and conn_cells {(key_a, key_b): (cell of a, cell of b)}.

        Doors: conn_doors {(key_a, key_b): (room key, entry node id)}, the
        walkable-graph entry node a doorway's door sits on (BuildSim's
        /api/doors entry_node_id); lifts: zones that are lift lobbies."""
        self.zones = zones
        self.connections = connections
        self.stairs = list(stairs)
        self.cells = dict(cells or {})
        self.cell_links = [tuple(c) for c in cell_links]
        self.conn_cells = dict(conn_cells or {})
        self.conn_doors = dict(conn_doors or {})
        self.lifts = list(lifts)
        self.neighbours = {k: [] for k in zones}
        for a, b, _, _ in connections:
            self.neighbours[a].append(b)
            self.neighbours[b].append(a)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_floors(cls, floors, cross_edges=()):
        """floors: {level: floor JSON as served by /api/building/floors}."""
        zones, connections, seen = {}, {}, set()
        cells, cell_links, conn_cells, conn_doors = {}, [], {}, {}
        for level, floor in floors.items():
            for room in floor["rooms"]:
                name = room["name"]
                zone_key = key(level, name)
                if zone_key in zones:
                    continue  # duplicate label on one floor: keep the first
                zones[zone_key] = Zone(
                    level, name, room.get("type", "room"),
                    max(MIN_AREA_M2, float(room.get("area", 0)) / PLAN_UNITS_PER_M ** 2),
                    CEILING_HEIGHT_M, room.get("center"),
                )
            graph = floor.get("walkable_graph") or {}
            nodes = {n["id"]: n for n in graph.get("nodes", [])}

            # Corridor cells from the corridor's own nodes and edges.
            node_cell = {}
            by_corridor = {}
            for n in nodes.values():
                zone_key = key(level, n["name"])
                if zone_key in zones and zones[zone_key].type == "corridor":
                    by_corridor.setdefault(zone_key, []).append(n["id"])
            internal = {}
            for edge in graph.get("edges", []):
                a, b = nodes.get(edge["from"]), nodes.get(edge["to"])
                if a and b and a["name"] == b["name"]:
                    internal.setdefault(key(level, a["name"]), []).append(
                        (a["id"], b["id"], float(edge.get("weight") or math.hypot(
                            a["x"] - b["x"], a["y"] - b["y"]))))
            for zone_key, ids in by_corridor.items():
                assign, centers, links = _segment(ids, internal.get(zone_key, []), nodes)
                node_cell.update(assign)
                if len(centers) > 1:
                    cells[zone_key] = centers
                    width = min(OPENING_WIDTH_M, max(1.0, zones[zone_key].area / max(
                        1.0, len(centers) * CORRIDOR_CELL_M)))
                    for i, j in links:
                        cell_links.append((zone_key, i, j, round(width, 2), CEILING_HEIGHT_M))

            for edge in graph.get("edges", []):
                a, b = nodes.get(edge["from"]), nodes.get(edge["to"])
                if not a or not b or a["name"] == b["name"]:
                    continue
                ka, kb = key(level, a["name"]), key(level, b["name"])
                if ka not in zones or kb not in zones:
                    continue
                pair = tuple(sorted((ka, kb)))
                if pair in seen:
                    continue
                seen.add(pair)
                corridors = zones[ka].type == "corridor" and zones[kb].type == "corridor"
                width, height = ((OPENING_WIDTH_M, min(OPENING_HEIGHT_M, CEILING_HEIGHT_M))
                                 if corridors else (DOOR_WIDTH_M, DOOR_HEIGHT_M))
                connections[pair] = (pair[0], pair[1], width, height)
                ends = {ka: node_cell.get(a["id"], 0), kb: node_cell.get(b["id"], 0)}
                if ends[pair[0]] or ends[pair[1]]:
                    conn_cells[pair] = (ends[pair[0]], ends[pair[1]])
                # The door of this doorway sits on the room's entry node.
                entry = a if a.get("type") == "entry" else b if b.get("type") == "entry" else None
                if entry is not None and not corridors:
                    conn_doors[pair] = (key(level, entry["name"]), int(entry["id"]))
        stairs, lifts = [], set()
        for edge in cross_edges or ():
            a = key(edge["from_level"].split("/")[-1], edge["from_name"])
            b = key(edge["to_level"].split("/")[-1], edge["to_name"])
            if edge.get("type") == "elevator":
                lifts.update(k for k in (a, b) if k in zones)
            if edge.get("type") != "stair":
                continue
            if a in zones and b in zones:
                stairs.append((a, b))
        return cls(zones, list(connections.values()), stairs, cells, cell_links, conn_cells,
                   conn_doors, sorted(lifts))

    @classmethod
    def from_api(cls, base_url, levels=None):
        base = base_url.rstrip("/")

        def get(path):
            with urllib.request.urlopen(base + path, timeout=30) as resp:
                return json.loads(resp.read())

        if levels is None:
            levels = [lv["id"] for lv in get("/api/building").get("levels", [])]
        floors = {lv: get(f"/api/building/floors/{lv}") for lv in levels}
        try:
            cross = get("/api/building/cross-floor-edges")
        except Exception:  # noqa: BLE001
            cross = []
        return cls.from_floors(floors, cross)

    @classmethod
    def from_files(cls, data_dir, levels=("level0", "level1", "level2")):
        floors = {}
        for level in levels:
            with open(os.path.join(data_dir, level, "floorplan_data.json"), encoding="utf-8") as f:
                floors[level] = json.load(f)
        cross_path = os.path.join(data_dir, "cross_floor_edges.json")
        cross = []
        if os.path.exists(cross_path):
            with open(cross_path, encoding="utf-8") as f:
                cross = json.load(f)
        return cls.from_floors(floors, cross)

    @classmethod
    def load(cls, base_url=None, data_dir=None):
        """From DATA_DIR files if given/present, otherwise from BuildSim."""
        data_dir = data_dir or os.environ.get("BUILDING_DATA_DIR")
        if data_dir and os.path.isdir(data_dir):
            return cls.from_files(data_dir)
        return cls.from_api(base_url or os.environ.get("BUILDSIM_URL", "http://buildsim:9090"))

    def to_json(self):
        return {"zones": [z.to_dict() for z in self.zones.values()],
                "connections": self.connections, "stairs": self.stairs,
                "cells": self.cells, "cell_links": self.cell_links,
                "conn_cells": [[a, b, i, j] for (a, b), (i, j) in self.conn_cells.items()],
                "conn_doors": [[a, b, r, n] for (a, b), (r, n) in self.conn_doors.items()],
                "lifts": self.lifts}

    @classmethod
    def from_json(cls, data):
        zones = {}
        for z in data["zones"]:
            zone = Zone(z["level"], z["name"], z["type"], z["area"], z["height"], z.get("center"))
            zones[zone.key] = zone
        return cls(zones, [tuple(c) for c in data["connections"]],
                   [tuple(s) for s in data.get("stairs", [])],
                   data.get("cells"), data.get("cell_links", ()),
                   {(a, b): (i, j) for a, b, i, j in data.get("conn_cells", [])},
                   {(a, b): (r, n) for a, b, r, n in data.get("conn_doors", [])},
                   data.get("lifts", ()))

    # -- queries ------------------------------------------------------------

    def sensor_zones(self, levels=None):
        """Zones that carry fire sensors and a sprinkler."""
        types = {"room", "corridor"} if SENSOR_SCOPE == "rooms+corridors" else {"room"}
        return [k for k, z in self.zones.items()
                if z.type in types and (levels is None or z.level in levels)]

    def neighbour_map(self, keys=None):
        keys = set(self.zones if keys is None else keys)
        return {k: [n for n in self.neighbours.get(k, []) if n in keys] for k in keys}

    def subgraph(self, keys):
        keys = set(keys)
        zones = {k: z for k, z in self.zones.items() if k in keys}
        conns = [c for c in self.connections if c[0] in keys and c[1] in keys]
        return Topology(
            zones, conns, [s for s in self.stairs if s[0] in keys and s[1] in keys],
            {k: v for k, v in self.cells.items() if k in keys},
            [c for c in self.cell_links if c[0] in keys],
            {p: v for p, v in self.conn_cells.items() if p[0] in keys and p[1] in keys},
            {p: v for p, v in self.conn_doors.items() if p[0] in keys and p[1] in keys},
            [k for k in self.lifts if k in keys])

    def neighbourhood(self, seed, max_zones, rng=None):
        """Breadth-first set of up to max_zones zones around seed."""
        order, frontier, seen = [seed], [seed], {seed}
        while frontier and len(order) < max_zones:
            nxt = []
            for k in frontier:
                nbrs = list(self.neighbours.get(k, []))
                if rng:
                    rng.shuffle(nbrs)
                for n in nbrs:
                    if n not in seen and len(order) < max_zones:
                        seen.add(n)
                        order.append(n)
                        nxt.append(n)
            frontier = nxt
        return order

    def doors(self):
        """[(door id, connection pair, room key, entry node id)] for every
        doorway with a door (corridor-to-corridor openings have none)."""
        return [(door_id(room, entry), pair, room, entry)
                for pair, (room, entry) in sorted(self.conn_doors.items())]

    def resolve(self, name, default_level="level0"):
        """"A109" or "level1/B201" -> zone key (or None)."""
        k = name if "/" in name else key(default_level, name)
        return k if k in self.zones else None

    def distance(self, a, b):
        za, zb = self.zones[a], self.zones[b]
        if not za.center or not zb.center:
            return math.inf
        return math.hypot(za.center[0] - zb.center[0], za.center[1] - zb.center[1])


def _segment(ids, edges, nodes):
    """Split one corridor's walkable nodes into cells of ~CORRIDOR_CELL_M.

    Farthest-point seeds along the corridor graph, then every node joins its
    nearest seed (graph distance). Returns ({node id: cell}, [{"center",
    "share"}], [(cell i, cell j)] for cells that share an edge)."""
    import heapq

    adj = {i: [] for i in ids}
    for a, b, w in edges:
        if a in adj and b in adj:
            adj[a].append((b, w))
            adj[b].append((a, w))

    def dijkstra(sources):
        dist = {s: 0.0 for s in sources}
        owner = {s: n for n, s in enumerate(sources)}
        heap = [(0.0, s) for s in sources]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]:
                continue
            for v, w in adj[u]:
                if d + w < dist.get(v, math.inf):
                    dist[v] = d + w
                    owner[v] = owner[u]
                    heapq.heappush(heap, (d + w, v))
        return dist, owner

    cell_len = CORRIDOR_CELL_M * PLAN_UNITS_PER_M
    if len(ids) < 2 or cell_len <= 0:
        return {i: 0 for i in ids}, [], []
    first, _ = dijkstra([ids[0]])
    end = max(first, key=first.get)
    dist, _ = dijkstra([end])
    extent = max(dist.values())
    count = max(1, min(len(ids), int(round(extent / cell_len)) + (1 if extent > cell_len / 2 else 0)))
    if count <= 1:
        return {i: 0 for i in ids}, [], []
    seeds = [end]
    while len(seeds) < count:
        dist, _ = dijkstra(seeds)
        far = max((i for i in ids if i in dist), key=lambda i: dist[i], default=None)
        if far is None or dist[far] < cell_len / 2:
            break
        seeds.append(far)
    if len(seeds) <= 1:
        return {i: 0 for i in ids}, [], []
    _, owner = dijkstra(seeds)
    for i in ids:   # nodes in a disconnected piece: nearest seed in the plane
        if i not in owner:
            owner[i] = min(range(len(seeds)), key=lambda n: math.hypot(
                nodes[i]["x"] - nodes[seeds[n]]["x"], nodes[i]["y"] - nodes[seeds[n]]["y"]))
    members = {n: [i for i in ids if owner[i] == n] for n in range(len(seeds))}
    centers = [{
        "center": [round(sum(nodes[i]["x"] for i in m) / len(m), 2),
                   round(sum(nodes[i]["y"] for i in m) / len(m), 2)],
        "share": len(m) / len(ids),
    } for n, m in sorted(members.items())]
    links = sorted({tuple(sorted((owner[a], owner[b]))) for a, b, _ in edges
                    if a in owner and b in owner and owner[a] != owner[b]})
    return owner, centers, links
