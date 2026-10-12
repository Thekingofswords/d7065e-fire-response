"""A small in-memory stand-in for BuildSim's REST API, for tests.

Implements only what the fire-safety services use: health, equipment
(bulk create, list with filters, sensor value / actuator state get and put),
room layers, effects, alerts, entities, occupancy, doors (with the derived
"blocked" flag), global coverage and one viewer session ("e2e-viewer":
highlights, coverage, route), validated the way BuildSim validates them
(rejections are kept in `rejected`), and, given a data directory, the building,
floor and cross-floor endpoints and same-floor walkable routes by node id
that skip blocked doors. `restart()` wipes the runtime state the way a real
BuildSim restart does.

    server = FakeBuildSim().start()     # picks a free port
    server.url                          # http://127.0.0.1:<port>
    python fake_buildsim.py 19090       # standalone
"""
import heapq
import json
import math
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Clients killed mid-request (fault-injection tests) are expected.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class FakeBuildSim:
    def __init__(self, port=0, data_dir=None):
        self.lock = threading.Lock()
        self.data_dir = data_dir or os.environ.get("BUILDING_DATA_DIR")
        self.floors = {}
        self.blocked_paths = set()   # GET paths that answer 503 (fault injection)
        self.restart()
        handler = self._handler()
        self.httpd = _Server(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.requests = 0

    def restart(self):
        with self.lock:
            self.equipment = {}      # id -> equipment dict
            self.sensors = {}        # sensor id -> sensor dict (inside equipment)
            self.actuators = {}      # actuator id -> actuator dict
            self.layers = []
            self.effects = []
            self.alerts = []
            self.entities = []
            self.occupancy = {}
            self.doors = []
            self.coverage = []
            self.sessions = {"e2e-viewer": {"highlights": [], "coverage": [], "route": None,
                                            "version": 0}}
            self.history = {"highlights": [], "coverage": []}   # every accepted PUT
            self.rejected = []       # (path, error) of rejected PUTs

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()

    # -- helpers for assertions ------------------------------------------------

    def actuator_state(self, actuator_id):
        with self.lock:
            a = self.actuators.get(actuator_id)
            return a and a.get("state")

    def layer_value(self, layer_id, zone, default=None):
        with self.lock:
            for layer in self.layers:
                if layer.get("id") == layer_id:
                    return layer.get("values", {}).get(zone, default)
        return default

    def door(self, door_id):
        with self.lock:
            return next((dict(d) for d in self.doors if d["id"] == door_id), None)

    def door_error(self, d):
        """The checks BuildSim's PUT /api/doors makes (the whole PUT fails)."""
        if d.get("kind", "door") not in ("door", "entrance", "fire_exit"):
            return f"door {d.get('id')} kind must be door, entrance, or fire_exit"
        if d.get("state", "closed") not in ("open", "closed", "blocked"):
            return f"door {d.get('id')} state must be open, closed, or blocked"
        if d.get("lock_state", "unlocked") not in ("unlocked", "locked", "jammed"):
            return f"door {d.get('id')} lock_state must be unlocked, locked, or jammed"
        if not (d.get("room") or d.get("entry_node_id") is not None or d.get("position")):
            return f"door {d.get('id')} requires room, entry_node_id, or position"
        if not self.data_dir:
            return None
        try:
            floor = self.floor(d.get("level"))
        except (OSError, TypeError):
            return f"door {d.get('id')} references unknown level {d.get('level')!r}"
        if d.get("entry_node_id") is not None:
            entry = next((n for n in floor["walkable_graph"]["nodes"]
                          if n["id"] == d["entry_node_id"] and n["type"] == "entry"), None)
            if entry is None:
                return f"door {d['id']} entry_node_id {d['entry_node_id']} is not an entry node"
            if d.get("room") and d["room"].lower() != entry["name"].lower():
                return f"door {d['id']} room {d['room']!r} does not match entry node room"
        if d.get("room") and d["room"] not in {r["name"] for r in floor["rooms"]}:
            return f"door {d['id']} references unknown room {d['room']!r}"
        return None

    def _room_error(self, item):
        if not self.data_dir or not item.get("room"):
            return None
        try:
            rooms = {r["name"] for r in self.floor(item.get("level") or "level0")["rooms"]}
        except (OSError, TypeError):
            return f"unknown level {item.get('level')!r}"
        return None if item["room"] in rooms else f"unknown room {item['room']!r}"

    def highlight_error(self, items):
        if not isinstance(items, list):
            return "highlights must be a list"
        for h in items:
            if not 0 <= float(h.get("opacity", 0.8)) <= 1:
                return "opacity must be between 0 and 1"
            colour = h.get("color", "#ffcc00")
            if not (isinstance(colour, str) and colour.startswith("#") and len(colour) in (4, 7)):
                return f"bad color {colour!r}"
            error = self._room_error(h)
            if error:
                return error
        return None

    def coverage_error(self, items):
        if not isinstance(items, list):
            return "coverage must be a list"
        if len({c.get("id") for c in items}) != len(items):
            return "coverage ids must be unique"
        for c in items:
            if not c.get("id"):
                return "coverage id required"
            if not float(c.get("radius", 0)) > 0:
                return "radius must be positive"
            if not 0 <= float(c.get("opacity", 0.15)) <= 1:
                return "opacity must be between 0 and 1"
            if not c.get("room") and c.get("center") is None:
                return "room or center required"
            error = self._room_error(c)
            if error:
                return error
        return None

    def floor(self, level):
        if level not in self.floors:
            with open(os.path.join(self.data_dir, level, "floorplan_data.json"),
                      encoding="utf-8") as f:
                self.floors[level] = json.load(f)
        return self.floors[level]

    def route(self, level, start, goal):
        graph = self.floor(level)["walkable_graph"]
        nodes = {n["id"]: n for n in graph["nodes"]}
        blocked = set()
        for d in self.doors:
            if d.get("blocked") and d.get("level") == level:
                if d.get("entry_node_id") is not None:
                    blocked.add(d["entry_node_id"])
                elif d.get("room"):
                    blocked |= {i for i, n in nodes.items()
                                if n["type"] == "entry" and n["name"] == d["room"]}
        adj = {}
        for e in graph["edges"]:
            w = e.get("weight") or 1.0
            adj.setdefault(e["from"], []).append((e["to"], w))
            adj.setdefault(e["to"], []).append((e["from"], w))
        dist, prev, heap = {start: 0.0}, {}, [(0.0, start)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == goal:
                break
            if d > dist.get(u, math.inf):
                continue
            for v, w in adj.get(u, ()):
                if v in blocked and v not in (start, goal):
                    continue
                if d + w < dist.get(v, math.inf):
                    dist[v], prev[v] = d + w, u
                    heapq.heappush(heap, (d + w, v))
        if goal not in dist:
            return None
        path = [goal]
        while path[-1] != start:
            path.append(prev[path[-1]])
        return {"path": [{"room_id": i, "name": nodes[i]["name"], "x": nodes[i]["x"],
                          "y": nodes[i]["y"]} for i in reversed(path)],
                "distance": dist[goal]}

    # -- HTTP --------------------------------------------------------------------

    def _handler(self):
        sim = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def _route(self):
                parsed = urllib.parse.urlparse(self.path)
                parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
                return parts, urllib.parse.parse_qs(parsed.query)

            def do_GET(self):  # noqa: N802
                sim.requests += 1
                parts, query = self._route()
                if urllib.parse.urlparse(self.path).path in sim.blocked_paths:
                    return self._send({"error": "unavailable"}, 503)
                with sim.lock:
                    if parts == ["healthz"]:
                        return self._send({"status": "ok"})
                    if parts == ["api", "room-layers"]:
                        return self._send(sim.layers)
                    if parts == ["api", "effects"]:
                        return self._send(sim.effects)
                    if parts == ["api", "alerts"]:
                        return self._send(sim.alerts)
                    if parts == ["api", "entities"]:
                        return self._send(sim.entities)
                    if parts == ["api", "occupancy"]:
                        return self._send(sim.occupancy)
                    if parts == ["api", "doors"]:
                        return self._send(sim.doors)
                    if parts == ["api", "sessions"]:
                        return self._send([{"id": i} for i in sim.sessions])
                    if parts == ["api", "coverage"]:
                        return self._send(sim.coverage)
                    if len(parts) == 3 and parts[:2] == ["api", "sessions"] \
                            and parts[2] in sim.sessions:
                        return self._send(sim.sessions[parts[2]])
                    if sim.data_dir and parts == ["api", "building"]:
                        return self._send({"name": "fake", "levels": [
                            {"id": lv} for lv in sorted(os.listdir(sim.data_dir))
                            if lv.startswith("level")]})
                    if sim.data_dir and parts[:3] == ["api", "building", "floors"]:
                        return self._send(sim.floor(parts[3]))
                    if sim.data_dir and parts == ["api", "building", "cross-floor-edges"]:
                        with open(os.path.join(sim.data_dir, "cross_floor_edges.json")) as f:
                            return self._send(json.load(f))
                    if sim.data_dir and parts == ["api", "graph", "route"] and "from" in query:
                        r = sim.route(query.get("level", ["level0"])[0],
                                      int(query["from"][0]), int(query["to"][0]))
                        return self._send(r) if r else self._send({"error": "no path"}, 404)
                    if parts == ["api", "equipment"]:
                        items = list(sim.equipment.values())
                        for field in ("level", "room", "type", "category"):
                            if field in query:
                                items = [e for e in items if e.get(field) == query[field][0]]
                        return self._send(items)
                    if len(parts) == 3 and parts[:2] == ["api", "equipment"]:
                        e = sim.equipment.get(parts[2])
                        return self._send(e) if e else self._send({"error": "not found"}, 404)
                    if len(parts) == 3 and parts[:2] == ["api", "sensors"]:
                        s = sim.sensors.get(parts[2])
                        return self._send(s) if s else self._send({"error": "not found"}, 404)
                    if len(parts) == 3 and parts[:2] == ["api", "actuators"]:
                        a = sim.actuators.get(parts[2])
                        return self._send(a) if a else self._send({"error": "not found"}, 404)
                return self._send({"error": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                sim.requests += 1
                parts, _ = self._route()
                body = self._body()
                if parts == ["api", "equipment", "bulk"] and isinstance(body, list):
                    created = 0
                    with sim.lock:
                        for item in body:
                            if item["id"] in sim.equipment:
                                continue
                            sim.equipment[item["id"]] = item
                            for s in item.get("sensors", []):
                                sim.sensors[s["id"]] = s
                            for a in item.get("actuators", []):
                                sim.actuators[a["id"]] = a
                            created += 1
                    return self._send({"created": created})
                return self._send({"error": "not found"}, 404)

            def do_PUT(self):  # noqa: N802
                sim.requests += 1
                parts, _ = self._route()
                body = self._body()
                with sim.lock:
                    if parts == ["api", "room-layers"]:
                        sim.layers = body or []
                        return self._send({})
                    if parts == ["api", "effects"]:
                        sim.effects = body or []
                        return self._send({})
                    if parts == ["api", "alerts"]:
                        sim.alerts = body or []
                        return self._send({})
                    if parts == ["api", "entities"]:
                        sim.entities = body or []
                        return self._send({})
                    if parts == ["api", "occupancy"]:
                        sim.occupancy = body or {}
                        return self._send({})
                    if parts == ["api", "doors"]:
                        doors = body or []
                        if len(doors) > 2000 or len({d.get("id") for d in doors}) != len(doors):
                            return self._send({"error": "invalid doors"}, 400)
                        for d in doors:
                            error = sim.door_error(d)
                            if error:
                                return self._send({"error": error}, 400)
                            d["blocked"] = d.get("state") == "blocked" or \
                                d.get("lock_state") in ("locked", "jammed")
                        sim.doors = doors
                        return self._send({})
                    if parts == ["api", "coverage"]:
                        error = sim.coverage_error(body)
                        if error:
                            sim.rejected.append(("/api/coverage", error))
                            return self._send({"error": error}, 400)
                        sim.coverage = body
                        sim.history["coverage"].append(body)
                        return self._send({})
                    if len(parts) == 4 and parts[:2] == ["api", "sessions"]:
                        session = sim.sessions.get(parts[2])
                        if session is None:
                            return self._send({"error": "session not found"}, 404)
                        kind = parts[3]
                        error = {"highlights": sim.highlight_error,
                                 "coverage": sim.coverage_error}.get(kind, lambda b: None)(body)
                        if error:
                            sim.rejected.append((self.path, error))
                            return self._send({"error": error}, 400)
                        session[kind] = body
                        session["version"] += 1
                        if kind == "highlights":
                            sim.history["highlights"].append(body)
                        return self._send({"version": session["version"]})
                    if len(parts) == 4 and parts[:2] == ["api", "sensors"] and parts[3] == "value":
                        s = sim.sensors.get(parts[2])
                        if not s:
                            return self._send({"error": "not found"}, 404)
                        s["value"] = (body or {}).get("value")
                        if "binary_value" in (body or {}):
                            s["binary_value"] = bool(body["binary_value"])
                        return self._send(s)
                    if len(parts) == 4 and parts[:2] == ["api", "actuators"] and parts[3] == "state":
                        a = sim.actuators.get(parts[2])
                        if not a:
                            return self._send({"error": "not found"}, 404)
                        a["state"] = (body or {}).get("state")
                        return self._send(a)
                return self._send({"error": "not found"}, 404)

        return Handler


if __name__ == "__main__":
    server = FakeBuildSim(int(sys.argv[1]) if len(sys.argv) > 1 else 19090)
    print(f"fake BuildSim on {server.url}", flush=True)
    server.httpd.serve_forever()
