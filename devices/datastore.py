"""Recording the MQTT bus: a shared aggregator plus database and CSV sinks.

The aggregator subscribes to everything on the bus and, every
RECORD_INTERVAL seconds, turns the latest state into one *sample* per zone:
readings, sprinkler state, simulator truth and labels, staleness (the
dataset.py schema). Zones with nothing going on are sampled only every
QUIET_EVERY-th tick, which keeps a whole building's worth of data
manageable while still recording plenty of quiet rooms as negatives.
Events, commands, confirmed actuator states and scenarios are stored as
they arrive.

Sinks:
  PostgresSink   TimescaleDB / PostgreSQL (db_writer.py)
  CsvSink        scenario_<id>.csv files (recorder.py)
"""
import datetime
import json
import os
import threading
import time

import common
import dataset
import health
import lifecycle
import messages
import mqtt_bus

RECORD_INTERVAL = float(os.environ.get("RECORD_INTERVAL", "2"))
QUIET_EVERY = int(os.environ.get("QUIET_EVERY", "5"))
STALE_SECONDS = float(os.environ.get("SENSOR_STALE_SECONDS", "20"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    time                    timestamptz NOT NULL,
    scenario_id             bigint,
    scenario_time           real,
    level                   text NOT NULL,
    room                    text NOT NULL,
    temperature             real,
    smoke                   real,
    co                      real,
    co2                     real,
    sprinkler               smallint,
    true_fire_hrr           real,
    true_gas_temperature    real,
    true_smoke_obscuration  real,
    true_visibility         real,
    true_co                 real,
    true_co2                real,
    true_o2                 real,
    true_smoke_layer_height real,
    fire_phase              smallint,
    fire                    smallint,
    fire_origin             smallint,
    nuisance_source         smallint,
    sensor_fault            smallint,
    stale_signals           smallint
);
CREATE INDEX IF NOT EXISTS samples_room_time ON samples (level, room, time DESC);
CREATE INDEX IF NOT EXISTS samples_scenario ON samples (scenario_id, scenario_time);

CREATE TABLE IF NOT EXISTS events (
    time      timestamptz NOT NULL,
    type      text NOT NULL,
    severity  text,
    level     text,
    room      text,
    message   text,
    data      jsonb
);
CREATE INDEX IF NOT EXISTS events_time ON events (time DESC);
CREATE INDEX IF NOT EXISTS events_room ON events (level, room, time DESC);

CREATE TABLE IF NOT EXISTS commands (
    time        timestamptz NOT NULL,
    level       text,
    room        text,
    actuator    text,
    state       text,
    command_id  text,
    reason      text,
    source      text
);

CREATE TABLE IF NOT EXISTS actuator_states (
    time        timestamptz NOT NULL,
    level       text,
    room        text,
    actuator    text,
    state       text,
    command_id  text,
    reason      text
);

CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id  bigint PRIMARY KEY,
    started      timestamptz NOT NULL,
    kind         text,
    params       jsonb
);

-- TimescaleDB, when available: time-partitioned hypertables.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
        CREATE EXTENSION IF NOT EXISTS timescaledb;
        PERFORM create_hypertable('samples', 'time', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('events', 'time', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('commands', 'time', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('actuator_states', 'time', if_not_exists => TRUE, migrate_data => TRUE);
    END IF;
END
$$;
"""

# Storage tiers (TimescaleDB only; plain PostgreSQL gets an ordinary view):
#   hot   raw samples, compressed after COMPRESS_AFTER, dropped after
#         RETENTION_DAYS (archive.py copies them to Parquet first)
#   warm  samples_1m: one row per zone per minute (continuous aggregate),
#         kept as long as the database lives
COMPRESS_AFTER = os.environ.get("COMPRESS_AFTER", "1 day")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
EVENT_RETENTION_DAYS = int(os.environ.get("EVENT_RETENTION_DAYS", "365"))

AGGREGATE_COLUMNS = """
    level, room,
    max(temperature) AS temperature_max, avg(temperature) AS temperature_avg,
    max(smoke) AS smoke_max, avg(smoke) AS smoke_avg,
    max(co) AS co_max, max(co2) AS co2_max,
    max(sprinkler) AS sprinkler, max(true_fire_hrr) AS hrr_max,
    max(true_smoke_obscuration) AS true_smoke_max, max(fire) AS fire,
    count(*) AS samples"""

TIMESCALE_POLICIES = [
    # (description, statement)
    ("compress samples", """
        DO $$ BEGIN
          IF NOT (SELECT compression_enabled FROM timescaledb_information.hypertables
                  WHERE hypertable_name = 'samples') THEN
            ALTER TABLE samples SET (timescaledb.compress,
              timescaledb.compress_segmentby = 'level, room',
              timescaledb.compress_orderby = 'time DESC');
          END IF;
        END $$"""),
    ("compression policy", f"SELECT add_compression_policy('samples', "
                           f"INTERVAL '{COMPRESS_AFTER}', if_not_exists => TRUE)"),
    ("retention samples", f"SELECT add_retention_policy('samples', "
                          f"INTERVAL '{RETENTION_DAYS} days', if_not_exists => TRUE)"),
    ("retention events", f"SELECT add_retention_policy('events', "
                         f"INTERVAL '{EVENT_RETENTION_DAYS} days', if_not_exists => TRUE)"),
    ("continuous aggregate", f"""
        CREATE MATERIALIZED VIEW IF NOT EXISTS samples_1m
        WITH (timescaledb.continuous) AS
        SELECT time_bucket(INTERVAL '1 minute', time) AS bucket, {AGGREGATE_COLUMNS}
        FROM samples GROUP BY bucket, level, room WITH NO DATA"""),
    ("aggregate policy", """
        SELECT add_continuous_aggregate_policy('samples_1m',
          start_offset => INTERVAL '2 hours', end_offset => INTERVAL '1 minute',
          schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE)"""),
]

PLAIN_VIEW = f"""
    CREATE OR REPLACE VIEW samples_1m AS
    SELECT date_trunc('minute', time) AS bucket, {AGGREGATE_COLUMNS}
    FROM samples GROUP BY 1, level, room"""


def setup_storage(url):
    """Compression, retention and the per-minute aggregate. Each step runs
    on its own (autocommit): a failure is logged and never stops recording."""
    import psycopg
    done = []
    with psycopg.connect(url, autocommit=True) as conn:
        timescale = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
        steps = TIMESCALE_POLICIES if timescale else [("per-minute view", PLAIN_VIEW)]
        for name, statement in steps:
            try:
                conn.execute(statement)
                done.append(name)
            except Exception as exc:  # noqa: BLE001
                print(f"storage setup: {name} failed: {exc}", flush=True)
    print(f"storage tiers ({'TimescaleDB' if timescale else 'PostgreSQL'}): "
          f"{', '.join(done) or 'none'}", flush=True)
    return done


SAMPLE_COLUMNS = ["time"] + [c for c in dataset.COLUMNS if c != "wall_time"]

TRUTH_KEYS = dataset.TRUTH_LAYERS + [
    common.LAYER_FIRE_PHASE, common.LAYER_FIRE_ORIGIN, common.LAYER_NUISANCE,
    common.LAYER_SENSOR_FAULT,
]


class Recorder:
    """Keeps the latest bus state and produces samples and records."""

    def __init__(self, name, sinks):
        self.sinks = sinks
        self.lock = threading.Lock()
        self.readings = {}    # zone -> sensor message
        self.truth = {}       # zone -> truth message
        self.sprinklers = {}  # zone -> "on"/"off"
        self.gateways = {}
        self.clock = {}
        self.records = []     # (table, row) awaiting a flush
        self.tick = 0
        self.validator = messages.SensorValidator()
        self.life = lifecycle.Lifecycle(name, status_topic=mqtt_bus.service_status_topic(name))
        self.bus = self.life.bus
        sub = self.bus.subscribe
        sub(mqtt_bus.SENSORS_SUB, self._on_sensors, qos=0)
        sub("sim/truth/+/+", self._on_truth, qos=0)
        sub(mqtt_bus.CLOCK_TOPIC, self._on_clock, qos=0)
        sub(mqtt_bus.SCENARIO_TOPIC, self._on_scenario)
        sub(mqtt_bus.ACTUATOR_STATE_SUB, self._on_state)
        sub(mqtt_bus.ACTUATOR_SET_SUB, self._on_command)
        sub(mqtt_bus.EVENTS_TOPIC, self._on_event)
        sub(mqtt_bus.GATEWAY_STATUS_SUB, self._on_gateway)
        sub("building/doors/+/state", self._on_door_state)
        for pattern in ("building/doors/+/set", "building/+/+/doors/set",
                        "building/+/doors/set", "building/lifts/set"):
            sub(pattern, self._on_door_command)

    # -- handlers -------------------------------------------------------------

    def _on_sensors(self, topic, payload):
        level, room, _ = mqtt_bus.parse_room_topic(topic)
        if not level:
            return
        zone = f"{level}/{room}"
        clean = self.validator.check(zone, payload)   # corrupt / duplicate: not stored
        if clean is not None:
            self.readings[zone] = clean

    def _on_truth(self, topic, payload):
        level, room, _ = mqtt_bus.parse_room_topic(topic)
        zone = f"{level}/{room}"
        if isinstance(payload, dict):
            self.truth[zone] = payload
        else:
            self.truth.pop(zone, None)

    def _on_clock(self, topic, payload):
        if isinstance(payload, dict):
            self.clock = payload

    def _on_gateway(self, topic, payload):
        status = payload.get("status") if isinstance(payload, dict) else str(payload)
        self.gateways[mqtt_bus.dec(topic.split("/")[2])] = status

    def _on_scenario(self, topic, payload):
        if isinstance(payload, dict) and "id" in payload:
            self._record("scenarios", {"scenario_id": int(payload["id"]), "started": time.time(),
                                       "kind": payload.get("kind"), "params": payload})

    def _on_state(self, topic, payload):
        level, room, rest = mqtt_bus.parse_room_topic(topic)
        if not (level and len(rest) >= 2 and isinstance(payload, dict)):
            return
        zone = f"{level}/{room}"
        if rest[1] == "sprinkler":
            self.sprinklers[zone] = payload.get("state")
        self._record("actuator_states", {
            "time": payload.get("ts", time.time()), "level": level, "room": room,
            "actuator": rest[1], "state": payload.get("state"),
            "command_id": payload.get("command_id"), "reason": payload.get("reason")})

    def _on_command(self, topic, payload):
        level, room, rest = mqtt_bus.parse_room_topic(topic)
        if not (level and len(rest) >= 2):
            return
        payload = payload if isinstance(payload, dict) else {"state": payload}
        self._record("commands", {
            "time": payload.get("ts", time.time()), "level": level, "room": room,
            "actuator": rest[1], "state": payload.get("state"), "command_id": payload.get("id"),
            "reason": payload.get("reason"), "source": payload.get("source")})

    def _on_door_state(self, topic, payload):
        if not isinstance(payload, dict):
            return
        door_id = mqtt_bus.dec(topic.split("/")[2])
        self._record("actuator_states", {
            "time": payload.get("ts", time.time()), "level": payload.get("level"),
            "room": payload.get("room"), "actuator": f"door:{door_id}",
            "state": f"{payload.get('state')}/{payload.get('lock_state')}",
            "command_id": payload.get("command_id"), "reason": payload.get("reason")})

    def _on_door_command(self, topic, payload):
        payload = payload if isinstance(payload, dict) else {"state": payload}
        parts = [mqtt_bus.dec(p) for p in topic.split("/")]
        level = room = None
        target = "/".join(parts[1:-1])
        if len(parts) == 5:          # building/<level>/<room>/doors/set
            level, room, target = parts[1], parts[2], "doors"
        elif len(parts) == 4 and parts[2] == "doors":
            level, target = parts[1], "doors (floor)"
        self._record("commands", {
            "time": payload.get("ts", time.time()), "level": level, "room": room,
            "actuator": target, "state": "/".join(str(payload[k]) for k in ("state", "lock_state")
                                                  if k in payload),
            "command_id": payload.get("id"), "reason": payload.get("reason"),
            "source": payload.get("source")})

    def _on_event(self, topic, payload):
        if not isinstance(payload, dict):
            return
        data = {k: v for k, v in payload.items()
                if k not in ("ts", "type", "severity", "level", "room", "message")}
        self._record("events", {
            "time": payload.get("ts", time.time()), "type": payload.get("type", "unknown"),
            "severity": payload.get("severity"), "level": payload.get("level"),
            "room": payload.get("room"), "message": payload.get("message"), "data": data})

    def _record(self, table, row):
        with self.lock:
            self.records.append((table, row))

    # -- sampling ---------------------------------------------------------------

    def samples(self, now):
        self.tick += 1
        scenario_id = int(self.clock.get("scenario_id", 0) or 0)
        scenario_time = float(self.clock.get("scenario_time", 0.0) or 0.0)
        rows = []
        for zone, msg in list(self.readings.items()):
            truth = self.truth.get(zone)
            interesting = truth is not None or self.sprinklers.get(zone) == "on"
            if not interesting and self.tick % QUIET_EVERY:
                continue
            try:
                values = {s: float(msg[s]) for s in health.SIGNALS}
            except (KeyError, TypeError, ValueError):
                continue
            stale = now - float(msg.get("ts", 0)) > STALE_SECONDS or \
                self.gateways.get(msg.get("gateway")) == "offline"
            row = dataset.make_row(
                zone, scenario_id, scenario_time, values,
                self.sprinklers.get(zone) == "on", truth or {},
                wall_time=f"{now:.1f}",
                stale_mask=health.mask_from_signals(health.SIGNALS if stale else ()))
            rows.append(row)
        return rows

    def run(self):
        self.life.start()
        self.life.running()
        print(f"recording every {RECORD_INTERVAL}s (quiet zones every {QUIET_EVERY} ticks) "
              f"to {', '.join(type(s).__name__ for s in self.sinks)}", flush=True)
        total = 0
        while not self.life.stopping:
            started = time.time()
            self.life.beat()
            try:
                rows = self.samples(started)
                with self.lock:
                    records, self.records = self.records, []
                for sink in self.sinks:
                    sink.write(rows, records)
                total += len(rows)
                self.life.ok()
                if self.tick % 30 == 1:
                    print(f"{total} samples recorded ({len(rows)} this tick, "
                          f"{len(self.truth)} zones with hazards, {len(records)} records)", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"record tick failed: {exc}", flush=True)
                self.life.degraded(f"cannot record: {exc}")
                for sink in self.sinks:
                    getattr(sink, "reset", lambda: None)()
            self.life.sleep(max(0.0, RECORD_INTERVAL - (time.time() - started)))
        self.life.stop()


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------

class CsvSink:
    def __init__(self, directory):
        self.writer = dataset.ScenarioWriter(directory)
        self.directory = directory

    def write(self, rows, records):
        if rows:
            self.writer.write(rows)
            self.writer.flush()
        scenarios = [r for t, r in records if t == "scenarios"]
        if scenarios:
            os.makedirs(self.directory, exist_ok=True)
            with open(os.path.join(self.directory, "scenarios.jsonl"), "a", encoding="utf-8") as f:
                for s in scenarios:
                    f.write(json.dumps(s["params"]) + "\n")


class PostgresSink:
    def __init__(self, url):
        import psycopg

        self.psycopg = psycopg
        self.url = url
        self.conn = None

    def connect(self):
        while self.conn is None:
            try:
                self.conn = self.psycopg.connect(self.url, autocommit=False)
                with self.conn.cursor() as cur:
                    cur.execute(SCHEMA)
                self.conn.commit()
                print("connected to the database, schema ready", flush=True)
                if not getattr(self, "storage_ready", False):
                    setup_storage(self.url)
                    self.storage_ready = True
            except Exception as exc:  # noqa: BLE001
                print(f"waiting for the database: {exc}", flush=True)
                self.conn = None
                time.sleep(3)

    def reset(self):
        try:
            if self.conn:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn = None

    def write(self, rows, records):
        self.connect()
        with self.conn.cursor() as cur:
            if rows:
                cols = SAMPLE_COLUMNS
                with cur.copy(f"COPY samples ({', '.join(cols)}) FROM STDIN") as copy:
                    for r in rows:
                        values = [datetime.datetime.fromtimestamp(
                            float(r["wall_time"]), datetime.timezone.utc)]
                        for c in cols[1:]:
                            v = r[c]
                            values.append(None if v == "" else v)
                        copy.write_row(values)
            for table, row in records:
                if table == "scenarios":
                    cur.execute(
                        "INSERT INTO scenarios (scenario_id, started, kind, params) "
                        "VALUES (%s, to_timestamp(%s), %s, %s) ON CONFLICT (scenario_id) DO NOTHING",
                        (row["scenario_id"], row["started"], row["kind"], json.dumps(row["params"])))
                elif table == "events":
                    cur.execute(
                        "INSERT INTO events (time, type, severity, level, room, message, data) "
                        "VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s)",
                        (row["time"], row["type"], row["severity"], row["level"], row["room"],
                         row["message"], json.dumps(row["data"])))
                elif table in ("commands", "actuator_states"):
                    cols = [c for c in row if c != "time"]
                    cur.execute(
                        f"INSERT INTO {table} (time, {', '.join(cols)}) VALUES "
                        f"(to_timestamp(%s), {', '.join(['%s'] * len(cols))})",
                        [row["time"]] + [row[c] for c in cols])
        self.conn.commit()
