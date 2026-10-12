# Fire-safety system: devices, bus and data

Every room and corridor of the building (956 zones on three floors) has a
fire detector head (heat, smoke, CO, CO₂), a sprinkler and a smoke-purge
damper; every doorway has a door (access control locks offices and
stairwells out of hours, fire doors close on an alarm and every access
lock releases for egress), and the lifts are recalled on a confirmed fire.
The agent detects fires (rules, a trained model, or the rules plus anomaly
detection), predicts which zones the fire will spread to, and owns the
evacuation plan; occupants follow a daily schedule on the building clock and
evacuate floor by floor as the plan says. The services talk over MQTT
(Eclipse Mosquitto, one user per service with an ACL, optional TLS),
everything is stored in TimescaleDB (hot/warm) and archived to Parquet
(cold), and Grafana shows the readings and the agent's decision log; the
BuildSim viewer shows red = fire, orange = risk, green = safe, smoke as
coverage zones and the evacuation routes.

The control loop closes through BuildSim, which holds the shared state:

```
            PUT room layers (truth)                 GET room layers
  fire-simulator ─────────────▶  BuildSim  ◀──────────────── sensor-gateway-level0/1/2
   (physics)     ◀─────────────  (shared   ◀── PUT actuator ──┐  (instrument models)
              GET actuator state  state)        state          │        │
                                    ▲                          │        │ MQTT building/<level>/<room>/sensors
             PUT sensor values,     │                 actuator-gateway  ▼
             alerts (display)       │                          ▲   building-agent ──▶ building/alerts
                          buildsim-bridge                      │        │            building/events
                                    ▲          MQTT .../actuator/<name>/set
                                    └── MQTT (readings, alerts) ┘
  door-gateway   ◀── MQTT door commands (agent, evacuation, operator) ── PUT /api/doors ──▶ BuildSim
                     (access schedule on the building clock; the simulator reads the doors back)
  building-agent ── building/risk (spread prediction), building/evacuation/plan ──▶ occupants
  occupants      ── PUT entities, occupancy, session highlights/routes ──▶ BuildSim
                     (follows the plan; routes avoid fire, smoke, risk and blocked doors;
                      reports building/evacuation/progress; cross-checks /api/graph/route)
  buildsim-bridge ── PUT sensor values, alerts, /api/coverage (smoke zones) ──▶ BuildSim
  db-writer ◀── MQTT (everything, plus sim/truth labels) ──▶ TimescaleDB ──▶ grafana
                                                                        └──▶ archiver ──▶ Parquet
```

| File | Service | Role |
|---|---|---|
| `fire_simulator.py` | fire-simulator | Physics for every zone (active zones only); writes room layers and effects to BuildSim, reads sprinkler/damper state back from BuildSim; ground-truth labels on `sim/truth/...` for the database only |
| `sensor.py` | sensor-gateway-* | One gateway per floor: reads the environment from BuildSim's room layers, models every detector on the floor (lag, noise, faults), publishes readings |
| `actuator_gateway.py` | actuator-gateway | Owns the sprinkler/damper equipment in BuildSim: validates commands, writes the state to BuildSim, then confirms it on MQTT |
| `building_agent.py` | building-agent | Detection and decisions for the whole building (rules, trained model, or hybrid), fire doors, egress unlock, lifts, spread risk, the evacuation plan, alerts and events |
| `anomaly.py`, `train_anomaly.py` | (agent) | Anomaly detection (isolation forest + autoencoder on normal data) and cause classification (fire / nuisance / sensor fault) for `DETECTOR=hybrid` |
| `spread_model.py`, `train_spread.py` | (agent) | Fire-spread prediction: which zones become untenable in the next 5 minutes, from room adjacency, door states and temperature gradients |
| `simclock.py` | (all) | The building clock: the simulator publishes it on `sim/clock`, every other service follows it |
| `door_gateway.py` | door-gateway | Owns every door in BuildSim (`PUT /api/doors`, full snapshot): room doors on their entry nodes, fire exits, lift lobbies. Access-control schedule, validates and applies door commands, enforces the door rules, confirms on MQTT; door-lock equipment with position sensors |
| `buildsim_bridge.py` | buildsim-bridge | Registers the detectors in BuildSim and mirrors readings and alerts for the viewer; smoke coverage zones |
| `lifecycle.py`, `healthcheck.py` | (all) | Starting → Syncing → Running ⇄ Degraded → Stopped, published on the status topic; heartbeat file for the Docker health check; watchdog; clean SIGTERM |
| `archive.py` | archiver | Copies finished days from the database to Parquet (cold storage) |
| `../examples/api/scenario/populate_evacuation.py` | occupants | People with a daily schedule; follows the agent's evacuation plan; hazard-weighted routes cross-checked with BuildSim's route API; viewer highlights; progress reports; marks burning dead ends through the door gateway |
| `messages.py` | (all consumers) | Message schemas and validation: corrupt, impossible, duplicate and out-of-order readings are rejected and counted |
| `db_writer.py`, `datastore.py` | db-writer | Records samples, events, commands, actuator states and scenarios in the database |
| `recorder.py` | csv-recorder (optional) | The same samples as CSV files, no database needed |
| `export_dataset.py` | – | Database → training CSV files |
| `topology.py` | (all) | Zones, areas, doorways, stairs and corridor cells derived from BuildSim's floor plans |
| `mqtt_bus.py` | (all) | MQTT client and the topic contract |
| `scenarios.py`, `health.py`, `features.py`, `dataset.py` | (all) | Scenarios and labels, sensor health, features, dataset schema |
| `generate_dataset.py`, `train_model.py` | – | Offline training data (with the agent's sprinkler, purge and fire-door policy) and fire-model training |
| `agent.py`, `actuator.py` | – | Old per-room services, kept for reference, not started |
| `tests/` | tests (profile) | Unit, end-to-end and fault-injection tests, with a fake BuildSim |
| `experiments/` | – | Evaluation scripts: response over time, baseline comparison, scaling, live latency (see `experiments/README.md`) |

## Running it

```bash
docker compose up --build -d
docker compose logs -f building-agent
```

BuildSim is at <http://127.0.0.1:9090>, Grafana at <http://127.0.0.1:3000>
(the "Fire safety" dashboard opens as the home page; `admin` / `fire` to
edit), MQTT at `localhost:1883`, the database at
`postgresql://fire:fire@localhost:5433/fire`. `docker compose ps` shows each
service's health.

**Secrets.** Without a `.env` the development passwords (`<user>-dev`,
`fire`) are used, which is fine on a laptop. Anywhere else: `cp env.example
.env`, change every value, `sh mosquitto/make-passwd.sh` (rewrites
`mosquitto/passwd` from `.env`; the test broker keeps the development users
in `mosquitto/passwd.test`), then `docker compose up -d`. Compose substitutes
`MQTT_PASSWORD_<USER>`, `POSTGRES_PASSWORD` and `GRAFANA_ADMIN_PASSWORD`.

**TLS.** `sh mosquitto/make-certs.sh` (a local CA and a certificate for
`mosquitto`/`localhost`), then

```bash
docker compose -f compose.yaml -f compose.override.yaml -f compose.tls.yaml up -d
```

The broker adds a TLS listener on 8883 (`mosquitto/mosquitto-tls.conf`, the
host sees 8883 instead of 1883) and every service connects with
`MQTT_TLS=1`, verifying the broker against `mosquitto/certs/ca.crt`. Clients
on the host: `MQTT_TLS=1 MQTT_HOST=localhost MQTT_PORT=8883
MQTT_CA_FILE=mosquitto/certs/ca.crt`, or `mosquitto_sub --cafile
mosquitto/certs/ca.crt -p 8883 ...`.

The broker requires a login. The `operator` user (password `operator-dev`)
may read and write everything; each service has its own user that may only
touch its own topics (`mosquitto/acl`). Watch the bus (with the mosquitto
clients installed, or `docker compose exec mosquitto ...`):

```bash
mosquitto_sub -h localhost -u operator -P operator-dev -t 'building/alerts' -v
mosquitto_sub -h localhost -u operator -P operator-dev -t 'building/events' -v
mosquitto_sub -h localhost -u operator -P operator-dev -t 'building/+/+/status' -v   # service states
```

Command a sprinkler or a door by hand (same validation as the agent's commands):

```bash
mosquitto_pub -h localhost -u operator -P operator-dev \
    -t 'building/level0/A109/actuator/sprinkler/set' \
    -m '{"state": "on", "reason": "manual test", "source": "operator"}'
mosquitto_pub -h localhost -u operator -P operator-dev \
    -t 'building/level0/A110/doors/set' \
    -m '{"state": "closed", "lock_state": "locked", "reason": "test", "source": "operator"}'
mosquitto_pub -h localhost -u operator -P operator-dev \
    -t 'building/doors/door-level0-A110-<entry node>/fault' -m '{"jammed": true}'
```

To change a password: set it in `.env` and run `sh mosquitto/make-passwd.sh`,
then `docker compose up -d` (restarts the broker and the services whose
password changed).

The agent adopts a running sprinkler it didn't start (manual release, or
its own restart) and only stops it through its normal "fire out" rules.

## MQTT topics

Room names are URL-encoded in topics (some contain `+`, an MQTT wildcard):
`A103+A112_B+A103` → `A103%2BA112_B%2BA103`.

| Topic | Payload | Flags |
|---|---|---|
| `building/<level>/<room>/sensors` | `{"ts", "temperature", "smoke", "co", "co2", "gateway"}` | retained |
| `building/<level>/<room>/actuator/<name>/set` | `{"state", "reason", "id", "ts", "source"}` | QoS 1 |
| `building/<level>/<room>/actuator/<name>/state` | `{"state", "ts", "command_id", "reason"}` | retained, QoS 1 |
| `building/gateways/<name>/status` | `{"status": "online"/"degraded"/"offline", "state", "detail", "since", "ts"}` (lifecycle; last will: offline) | retained |
| `building/agent/status`, `building/services/<name>/status`, `sim/status` | same, for the agent and the other services | retained |
| `building/doors/<door id>/set` | `{"state"?, "lock_state"?, "reason", "id", "source"}` one door | QoS 1 |
| `building/<level>/<room>/doors/set` | same, every door of a zone | QoS 1 |
| `building/<level>/doors/set` | same, every fire door on a floor | QoS 1 |
| `building/lifts/set` | same, every lift lobby | QoS 1 |
| `building/doors/<door id>/state` | `{"state", "lock_state", "kind", "level", "room", "ts", "command_id", "reason", "source"}` | retained, QoS 1 |
| `building/doors/<door id>/fault` | `{"jammed": true/false}` (fault injection) | QoS 1 |
| `building/doors/egress/set` | `{"lock_state": "unlocked"/"normal", ...}`: the agent releases every access-controlled door for egress on a fire alarm | QoS 1 |
| `building/alerts` | current alert list | retained |
| `building/risk` | `{"zones": {zone: probability}, "horizon_s", "model", "threshold", "ts"}`: zones predicted to become untenable | retained |
| `building/evacuation/plan` | `{"phase": normal/evacuate/all_clear, "evacuate_levels", "fire_zones", "alarm_zones", "smoke_zones", "risk_zones", "exits", "doors", "version", "clock", "ts"}` (agent) | retained |
| `building/evacuation/progress` | `{"phase", "levels", "present", "pre_movement", "evacuating", "evacuated", "awaiting_rescue": {zone: n}, "route_checks", ...}` (occupants) | retained |
| `building/events` | `{"ts", "type", "severity", "level", "room", "message", ...}` | QoS 1 |
| `sim/truth/<level>/<room>` | ground truth + labels, for the database (training and evaluation) only: no device reads it | retained |
| `sim/scenario` | scenario (with door states and sensor-fault schedule) | retained |
| `sim/clock` | the building clock: `{"scenario_id", "scenario_time", "sim_time", "week_minute", "clock": "Mon 07:31", "ts"}` | retained, every tick |

Requested and applied states are separate on purpose: the agent acts on
the confirmed `.../state`, which the actuator gateway publishes only after
BuildSim accepted the state. The simulator reads the state from BuildSim.

BuildSim ids: `fire_detection-<level>-<room>` (sensors `<level>-<room>-temp`,
`-smoke`, `-co`, `-co2`, owned by the bridge) and
`fire_suppression-<level>-<room>` (actuators `<level>-<room>-sprinkler`,
`-damper`, owned by the actuator gateway).

Every consumer validates sensor messages (`messages.py`): not JSON, missing
fields, non-numbers or NaN, physically impossible values, timestamps in the
future and duplicates or out-of-order messages are rejected. The agent
reports rejections as `data_quality` events every
`DATA_QUALITY_REPORT_SECONDS` (30 s).

## Scaling and failure handling

* **Gateways, not one process per device.** One sensor gateway per floor
  (~300 detectors each) and one actuator gateway. Add more by splitting
  `GATEWAY_LEVELS` or zones.
* **Active zones only.** The simulator integrates only zones with a fire,
  smoke, heat or a nuisance source; a quiet building costs almost nothing,
  a large fire ~0.1 s per 10 simulated seconds.
* **Offline detection.** A gateway's MQTT last will marks all its zones
  offline at once (one alert per gateway, not one per room). A single
  device that stops reporting is caught by message age
  (`SENSOR_STALE_SECONDS`). Frozen readings are caught by `health.py`.
* **Agent restart.** Retained readings and states are picked up again; a
  running sprinkler is never switched off by a restart.
* **Stale confirmations.** The broker keeps retained messages across
  `docker compose down`/`up`. When the actuator gateway starts it reads the
  sprinkler and damper states from BuildSim and corrects any retained
  confirmation that disagrees (a fresh BuildSim has every sprinkler off), and
  the agent drops a sprinkler it had adopted from such a stale "on".
* **Lifecycle.** Every service goes Starting → Syncing → Running and publishes
  its state, retained. It drops to *Degraded* when its inputs are bad (agent:
  a sensor gateway offline or degraded, or more than
  `DEGRADED_STALE_FRACTION` (20 %) of the zones stale; gateways: BuildSim
  unreachable, in which case the sensor gateway stops publishing instead of
  sending stale readings with fresh timestamps) and back to Running when they
  recover. While degraded the agent never switches water off and raises a
  critical alert. `docker compose stop` sends SIGTERM: the service finishes
  its tick and publishes "offline" itself. A heartbeat file feeds the Docker
  health check (`docker compose ps`), and a watchdog exits a hung process
  after `WATCHDOG_SECONDS` (120) so the restart policy restarts it.
* **BuildSim restart.** The bridge and the actuator gateway notice (404s or
  their periodic probe) and register their equipment again; the actuator
  gateway re-applies every sprinkler/damper state it holds. While BuildSim is
  unreachable the simulator keeps the last actuator states and the gateways
  keep the last environment.
* **BuildSim load.** It takes one request per sensor value, so the bridge
  forwards only changes beyond each sensor's noise (plus a refresh every
  5 minutes), at most `MAX_REQUESTS_PER_SECOND`. It shows at most 100
  alerts, most severe first.

## Tests

```bash
pip install -r devices/requirements.txt
cd devices
pytest tests                      # unit tests, ~1 min
pytest tests -m e2e -v -s         # end-to-end + fault injection, ~1 min (needs mosquitto)
docker compose --profile test run --rm tests   # everything, inside Docker
```

The unit tests need the building data (`../data/abuilding` or
`BUILDING_DATA_DIR`). The end-to-end tests start a broker (a local
`mosquitto`, or `E2E_MQTT_HOST`), a fake BuildSim and the real services, and
check:

| Test | What it proves |
|---|---|
| fire detected and suppressed through BuildSim | the closed loop: fire → layers → sensors → agent → actuator → BuildSim → simulator → fire decays |
| smoke purge opens dampers | the agent drives the dampers through the same path |
| corrupt sensor data is rejected | garbage, NaN, out-of-range, future and duplicate messages: rejected, reported as `data_quality`, the agent keeps running |
| corrupt actuator command is rejected | invalid states and unknown rooms: `command_rejected`, BuildSim unchanged |
| sensor gateway crash and recovery | `kill -9` a gateway: offline alert from its last will, readings back after restart |
| agent restart | a running sprinkler is adopted, not switched off |
| BuildSim restart | equipment re-registered, actuator states re-applied, doors republished |
| doors published to BuildSim | the door gateway's snapshot is accepted (BuildSim's validation rules, mirrored by the fake) |
| fire alarm closes doors and recalls lifts | the floor's fire doors close (unlocked), lift lobbies are blocked, the simulator reads the doors back |
| door rules are enforced | locked doors cannot be opened, fire exits never locked, unknown sources refused |
| jammed door makes the evacuation re-plan | fault injection on a door: BuildSim marks it blocked, the occupants re-route |
| degraded mode | two gateways offline: the agent publishes Degraded and returns to Running |
| gateway degrades when BuildSim is unreachable | the sensor gateway stops publishing and reports Degraded |
| graceful shutdown | SIGTERM: "offline" published by the service itself |
| broker enforces users and ACL | no login: refused; a sensor gateway user cannot command a sprinkler |
| broker outage degrades and recovers | the broker is killed for 15 s and restarted without its retained state: every service reconnects by itself, the agent goes Degraded while its inputs are stale (water stays on) and is Running again ~12 s after the restart |
| fire alarm unlocks egress doors | at night (`BUILDING_CLOCK_START=mon 22:00`) 500+ office and stairwell doors are locked; the alarm releases them, fire exits are never locked |
| agent plan drives the evacuation | the agent publishes a versioned evacuation plan (every floor for a ground-floor fire, egress unlocked); the occupants follow it on the building clock and report progress; spread risk is published |
| dashboard highlights and coverage | A109 red, exits and escape corridors green, smoke drawn as `/api/coverage` zones; every PUT passes BuildSim's validation (mirrored by the fake) |
| routes agree with BuildSim's route API | the local hazard-weighted planner, run without hazards, finds the same routes as `GET /api/graph/route` |

Unit tests beyond the agent, sensors and physics: `test_spread.py` (spread
features and baseline), `test_anomaly.py` (normal readings score low, a
fire, cooking and a dead sensor score high; check-zone alerts; uses the
trained model), `test_clock.py` (the building clock), `test_tls.py` (a real
broker with the TLS config and generated certificates: verified connection,
unknown CA and plain MQTT refused), and the plan phasing in
`test_agent.py`.

Measured times are written to `tests/e2e_results.json`. Set
`E2E_EVACUATION_SCRIPT` to the evacuation script's path when running the
tests outside the repository layout (the compose `tests` service does).

## Database

Tables (created automatically by db-writer; hypertables with TimescaleDB):

| Table | Contents |
|---|---|
| `samples` | One row per zone per `RECORD_INTERVAL` (quiet zones every `QUIET_EVERY`-th tick): readings, sprinkler, truth, labels, staleness |
| `samples_1m` | Per zone per minute: max/avg temperature and smoke, max CO/CO₂, sprinkler, fire (continuous aggregate with TimescaleDB, a view otherwise) |
| `events` | Room state changes, sensor faults, commands and door commands applied or rejected, service lifecycle, smoke purge, data quality, occupancy, evacuation |
| `commands` | Every actuator and door command sent, with its source |
| `actuator_states` | Every confirmed actuator state |
| `scenarios` | The simulator's scenarios (parameters as JSON) |

Examples:

```sql
-- what happened in A109 in the last 10 minutes
SELECT time, temperature, smoke, co, sprinkler FROM samples
WHERE level = 'level0' AND room = 'A109' AND time > now() - interval '10 minutes' ORDER BY time;

-- every alarm and sprinkler decision today, with the agent's reason
SELECT time, room, type, message FROM events
WHERE severity IN ('warning', 'critical') AND time > current_date ORDER BY time;
```

A whole building writes roughly 450 rows per 2 s while busy (less when
quiet). Storage tiers:

| Tier | Where | Kept |
|---|---|---|
| hot | `samples` (raw), compressed after `COMPRESS_AFTER` (1 day) | `RETENTION_DAYS` (30) |
| warm | `samples_1m` (one row per zone per minute) | as long as the database |
| cold | `./archive/<table>/date=YYYY-MM-DD/*.parquet`, written by the archiver every 6 hours for finished days | forever (files) |

Events are kept `EVENT_RETENTION_DAYS` (365). Compression, retention and the
continuous aggregate need TimescaleDB; on plain PostgreSQL db-writer creates
`samples_1m` as an ordinary view and skips the policies. Read the archive
with pandas or DuckDB, e.g. `duckdb -c "SELECT room, max(smoke) FROM
'archive/samples/*/*.parquet' GROUP BY room"`.

## Dashboard

The **BuildSim viewer** shows the situation as the project description asks:
session highlights (written by the occupants service from the agent's plan)
**red** for burning zones, **orange** for smoke and for zones the spread model
says are at risk (lighter), **green** for the exits and the corridors on the
evacuation routes, purple for rooms with people awaiting rescue; one
evacuee's remaining route (BuildSim's viewer flies the camera to every route
it receives, so by default one route is sent when the evacuation starts and
the camera is then left to you: `ROUTE_DISPLAY` / `--route-display once`;
`cycle` shows a different evacuee every 30 s, `off` none); and **smoke spread as coverage zones**
(`PUT /api/coverage`, written by the bridge from the *measured* smoke: one
disc per zone with ≥ 2 %/m, radius up to the room's size and grey → near
black as the smoke thickens, removed below 1 %/m), plus the room layers,
effects, equipment, doors and the alert list.

Grafana (<http://127.0.0.1:3000>) is provisioned with the TimescaleDB data
source and one dashboard (`grafana/dashboards/fire.json`): confirmed fires,
alarms, sprinklers on, dampers open, degraded services and rejected messages
as tiles; temperature, smoke and CO of a selected zone (sensor and truth);
the worst smoke per floor; the **decision log** (the agent's state changes
with their reasons, actuator and door commands, rejections, sensor health,
service states, evacuation); the commands with who sent them; people in the
building over the day.

## Collecting data and training a model

**1. Collect data.** Either source, or both:

* *Live*: set `SCENARIO_MODE: random` on fire-simulator. Random fires
  (rooms and corridors), nuisance sources and sensor faults happen all over
  the building and db-writer records them. Export with:

  ```bash
  docker compose run --rm db-writer python export_dataset.py \
      --database-url postgresql://fire:fire@timescaledb:5432/fire --out /recordings/db
  ```

  (mount `-v ./recordings:/recordings`, or run `export_dataset.py` on the host
  against `localhost:5433`).
* *Offline* (much faster): same physics, sensors and agent policy, in random
  neighbourhoods of the real building. From `devices/` on the host:

  ```bash
  pip install -r requirements.txt
  python generate_dataset.py --scenarios 1200 --zones 8 --out ../recordings/offline
  ```

  It reads the floor plans from `../data/abuilding`. 1,250 scenarios take
  about 4 minutes and give ~2 million rows. Controlled scenarios run the
  agent's sprinkler, purge and fire-door policy, as in the live system.

**2. Train.**

```bash
python train_model.py ../recordings/offline ../recordings/db --out ../models/fire_lgbm.txt
```

The script prints event metrics on held-out test scenarios for the model
*and* for the fixed rules on the same data, plus the most important
features. It saves `models/fire_lgbm.txt` and `models/fire_lgbm.json`.

**3. Use the model.** Set `DETECTOR: model` on `building-agent` and run
`docker compose up -d building-agent`. If the model can't be loaded (for
example after a feature change), the agent falls back to the rules and logs
a warning. Retrain whenever the physics or the features change.

**Current model** (`models/fire_lgbm.*`: 1,250 generated scenarios with the
fire-door policy, 51 features including `is_corridor`, release threshold
0.85). Held-out test scenarios, per burning zone, *open loop* (the recorded
data replayed, so smoke from a fire next door keeps coming whatever the
detector says):

| | Fires detected | Release after flaming, p90 | False releases per 100 zone-hours |
|---|---|---|---|
| Model | 90 % | 20 s | 0.97 |
| Rules | 98 % | 40 s | 20.3 (almost all smoke from a neighbouring fire) |

**Anomaly detection and the hybrid** (`models/anomaly.*`, `results/anomaly`):

```bash
python train_anomaly.py ../recordings/offline --out ../models/anomaly.pkl --report ../results/anomaly
```

| Held-out scenarios | |
|---|---|
| Fires / nuisances anomalous within 60 s | 100 % (94) / 100 % (96), median 10 s |
| Sensor faults anomalous within 60 s / 5 min | 19 % / 88 % (42; drift and stuck sensors take time to show) |
| False anomalies on normal data | 0.45 per 100 zone-hours (limit 1) |
| Cause accuracy (rows) | normal 99.5 %, fire 89.9 %, nuisance 92.9 %, fault 79.7 % |
| Hybrid release (anomalous and P(fire) ≥ 0.98) | median 20 s after ignition instead of 215 s for the rules alone, +0.3 false releases per 100 zone-hours (open loop) |

**Spread prediction** (`models/spread_lgbm.*`, `results/spread`, 800
closed-loop scenarios, horizon 5 minutes): AUC 0.88 against 0.67 for the
"neighbour of a fire or of a hot zone" baseline; at the chosen threshold
precision 0.69, recall 0.46, and 68 % of the zones that became untenable
were flagged at least 60 s ahead (baseline 38 %). The most important inputs
are the zone's own smoke rate and the smoke and temperature gradients
through *open* connections.

In the closed loop (`experiments/baseline.py`, 150 random scenarios, 72
fires, safety net and fire doors active) no policy let a fire flash over (13
without the system):

| Policy | Fires released | Release after flaming, median / p90 | Smoke, zone-min > 10 %/m | Releases in zones that never burned |
|---|---|---|---|---|
| No system | 0 | – | 17,770 | 0 |
| Rules, doors as they were | 70 | 37 / 45 s | 6,394 | 2 |
| Rules + fire doors | 70 | 34 / 44 s | 2,542 | 1 |
| Model + fire doors | 72 | −172 / 19 s | 2,223 | 4 |
| Hybrid + fire doors | 72 | −171 / 31 s | 1,503 | 2 |

The learned detectors release earlier (usually while the fire still
smoulders: negative delays), which cuts the smoke. With a broken sensor in
the fire room (`--faults`, below) the rules and the hybrid release every
fire, the model misses one. `DETECTOR: rules` stays the default because it
is the easiest to verify; `hybrid` is the best performer on these runs.

### Recorded columns (one row per zone per tick)

| Column | Meaning |
|---|---|
| `scenario_id`, `scenario_time` | Scenario and simulated seconds since it began |
| `temperature`, `smoke`, `co`, `co2` | Sensor readings (°C, %/m, ppm, ppm): what the agent sees |
| `sprinkler` | 1 if the room's sprinkler was on |
| `true_*` | Physical truth: heat release, gas temperature, obscuration, visibility, CO, CO₂, O₂, smoke-layer height |
| `fire` | **Target**: 1 if this room is burning (phase 1–4) |
| `fire_phase` | 0 idle, 1 smouldering, 2 growing, 3 fully developed, 4 decaying, 5 extinguished, 6 burnt out |
| `fire_origin` | 1 if the fire started in this room |
| `nuisance_source` | 0 none, 1 cooking, 2 steam, 3 dust, 4 heater, 5 smoking |
| `sensor_fault` | 0 none, 1 smoke drift, 2 temperature stuck, 3 smoke spikes, 4 CO dead, 5 sensor offline |
| `stale_signals` | Bitmask of signals whose sensor stopped reporting, as the agent saw it (1 temperature, 2 smoke, 4 CO, 8 CO₂) |

Each scenario's parameters are in `scenarios.jsonl` next to the CSVs.

### Random scenarios

40 % fire, 30 % nuisance only, 10 % fire plus nuisance, 10 % quiet, 10 %
sensor fault. 15 % of fires also get a broken sensor in the burning room
(before ignition), since that is the case a detector most needs to survive.
The draw varies the room, growth class, smouldering time
(0–15 min), fuel load, number of open doors, ventilation rate, occupancy,
and nuisance strength and timing. Every scenario starts with a quiet
`SCENARIO_SETTLE_SECONDS` (300 s) period; the training script skips it.

### Things to keep in mind

* Split by scenario, never by row (`train_model.py` does this).
* The model learns the *simulator's* physics. Randomising the parameters
  helps, but a model trained only on simulated data is not validated for
  real buildings. For example, it relies heavily on CO, which real CO cells
  measure less cleanly.
* Smoking and cooking are deliberately hard negatives: they make smoke and
  CO like a smouldering fire.

## Building agent

Per room, each tick: `NORMAL → ALARM → SUPPRESSING → COOLDOWN`.

* **Rules detector**: smoke ≥ 8 %/m or CO ≥ 50 ppm raises the alarm. Heat
  (≥ 68 °C, or a rise ≥ 8.3 °C/min) releases the sprinkler. A rate of rise
  counts only with evidence of combustion: CO ≥ `RATE_MIN_CO` (10 ppm), a
  fast rise (≥ `RATE_FAST`, 16.6 °C/min) or a hot room (≥ `RATE_HOT_C`,
  50 °C); cooking in a room whose fire doors have closed warms it by
  9–12 °C/min with ~5 ppm CO, and that used to release the sprinkler
  (`baseline.py`, plan item P6). A rate-of-rise trigger is *not* treated as a
  fire when a neighbouring room is hotter and also heat-alarmed: that is hot
  smoke spilling in. Only a building-level agent can do this.
* **Model detector**: fire probability per room. It releases above the
  trained threshold and alarms above half of it.
* **Hybrid detector** (`DETECTOR=hybrid`, `anomaly.py`): the rules, plus
  anomaly detection as a first stage (an isolation forest and an
  autoencoder trained on normal periods only; score ≥ 1 = anomalous,
  threshold for ≤ 1 false anomaly per 100 zone-hours) and a cause classifier
  (LightGBM: normal / fire / nuisance / sensor fault, with the anomaly score
  as a feature) as the second. An anomaly classified as fire raises the alarm
  (and releases water above the trained confidence, when one met the
  false-release budget); any other anomaly gives a **"Check <room>"** alert
  instead of water. The rules' decisions always stand.
* **Spread prediction** (`spread_model.py`): every tick, the probability that
  each zone becomes untenable (burning, > 60 °C or > 10 %/m smoke) within 5
  minutes, from the room adjacency, the door states (closed doors count 3 %),
  temperature gradients and rates, smoke around the zone and the distance to
  the nearest confirmed fire. LightGBM (`train_spread.py`) or a transparent
  baseline rule. Published on `building/risk`; drawn orange in the viewer;
  the occupants avoid those zones.
* **Evacuation plan**: the agent owns it (`building/evacuation/plan`,
  retained, versioned): `evacuate` on any alarm, phased (the alarm floor and
  the floors above first; every floor on a confirmed fire or after
  `FULL_EVACUATION_SECONDS`, 300 s; the floors never shrink during one
  evacuation), then `all_clear` once no zone has been in alarm for
  `ALL_CLEAR_SECONDS` (180 s). The plan lists fire, alarm, smoke and risk
  zones, the usable exits and the door state. The occupants report progress
  back; people sheltering or trapped become a critical
  "people awaiting rescue" alert.
* **Building clock**: the agent's decision time follows `sim/clock`
  (`simclock.PolicyClock`: never backwards, at most a few ticks per step, so
  a restarted simulator cannot make a sprinkler's minimum discharge pass in
  an instant), falling back to its own clock when the simulator is silent.
* **Safety net**: a room at `SAFETY_TEMP_C` (90 °C) gets water whatever the
  detector says (only from a healthy temperature sensor).
* Conditions must hold for `ALARM_CONFIRM_TICKS` readings. The sprinkler
  stops once the room is cool and no longer heating, after
  `MIN_DISCHARGE_SECONDS` of water.
* **Smoke purge**: a zone's damper opens when its smoke reaches
  `PURGE_ON_SMOKE` (3 %/m) and closes once it has stayed below
  `PURGE_OFF_SMOKE` (0.5 %/m), after at least `PURGE_MIN_SECONDS`. With a
  faulty smoke sensor it follows the zone's alarm state. Each opening and
  closing is a `smoke_purge` event.
* Publishes one alert per room to the viewer (`/api/alerts`).

## Sensor faults

`health.py` checks every room's sensors each tick:

* **Frozen**: a reading that has not changed at all for
  `SENSOR_FROZEN_SECONDS` (180 simulated s). Working sensors always jitter.
  Readings at the end of the range are exempt (0 %/m smoke in clean air, a
  maxed-out heat detector), so a sensor stuck at 0 can't be detected this
  way.
* **Offline**: the sensor's BuildSim timestamp is older than
  `SENSOR_STALE_SECONDS` (20 real s). A crashed sensor process leaves its
  last value in BuildSim, which would otherwise look plausible.

What the agent does with a faulty sensor:

| Situation | Behaviour |
|---|---|
| Any fault | Warning alert "Sensor fault in <room>" in the viewer |
| Temperature faulty (rules) | Releases on smoke ≥ `FALLBACK_SMOKE_THRESHOLD` (10 %/m) **and** CO ≥ `FALLBACK_CO_THRESHOLD` (30 ppm) |
| Temperature faulty | Can't declare the fire out: needs falling CO and smoke instead, and `FAULT_DISCHARGE_FACTOR` (2×) the minimum water time. If CO is also faulty, the sprinkler is not stopped automatically |
| All fire sensors faulty ("blind" room) | Alarms when a neighbour alarms, and gets water when a neighbour has a confirmed fire, or when a neighbour sees ≥ `BLIND_NEIGHBOUR_SMOKE` (2 %/m) smoke or ≥ `BLIND_NEIGHBOUR_CO` (15 ppm) CO, whatever the detector (`BLIND_ROOM_RELEASE`). With the fire doors closed, smoke leaking through the door gaps is the only sign of a fire in a blind room |
| Model detector | Faulty signals are blanked; the model was trained with `<signal>_ok` inputs and learns to rely on the rest |

60 fires with a frozen or offline temperature sensor in the burning room
before ignition (`python experiments/baseline.py --faults --scenarios 60`):

| | Fires released | Flashovers | Released in zones that never burned |
|---|---|---|---|
| No system | 0 | 4 | 0 |
| Rules, doors left as they are | 60 | 0 | 24 |
| Rules + fire doors | 60 | 1 | 10 |
| Model + fire doors | 59 | 1 | 31 |
| Hybrid + fire doors | 60 | 1 | 21 |

Closing the fire doors hides a fire in a room whose sensors are offline:
before the blind-zone rule (water for a blind room when a neighbour sees
≥ 2 %/m smoke or ≥ 15 ppm CO) the rules missed 6 of 25 such fires and 5
flashed over; with it, 1 of 60 fires flashed over. The fallbacks (smoke
+ CO, and precautionary water for blind rooms and their neighbours) catch
almost every fire, at the price of water in rooms that never burned.

Smoke drift, smoke spikes
and a dead CO cell don't freeze, so the health check doesn't catch them; the
model has seen them in training.

## Experiments

`experiments/` holds the evaluation scripts (response over time with and
without the system, baseline comparison, scaling, live latency from the
database). See `experiments/README.md`. Results on the current build:

* Demo fire in A109 (`results/response`): no system → flashover, smoke
  100 %/m in the corridor and upstairs for the whole run; rules agent with
  the doors left as they were → released 30 s after flaming, peak 44 %/m in
  the corridor, clear after 890 s; rules agent with the fire-door policy →
  the doors close at the smoke alarm, the corridor peaks at 0.5 %/m and
  everything is clear after 350 s.
* 150 random scenarios (`results/baseline`): smoke exposure 17,770
  zone-minutes above 10 %/m without the system, 6,394 with the rules agent
  and the doors left as they were, 2,542 with the fire doors, 1,503 with the
  hybrid detector; 13 flashovers without the system, none with it. Releases
  in zones that never burned: 1 for the rules with fire doors (0.1 per 100
  zone-hours; 11 before the rate-of-rise rule needed evidence of
  combustion), 2 for the hybrid, 4 for the model.
* Anomaly detection, cause classification and spread prediction: see
  "Collecting data and training a model" (`results/anomaly`,
  `results/spread`).
* Scaling: the agent needs 0.03 s per decision for 956 zones (0.08 s with
  the hybrid detector) and 0.6 s for 15,296 (16 buildings), so the 2 s
  decision interval would break at roughly 45,000 zones. The real limit is BuildSim's one-request-per-value API: 1,900
  requests/s would be needed without deadbands; the bridge's deadbands and
  150 requests/s cap keep the viewer a few seconds behind during a fire.
* **Live run on the real BuildSim** (Docker, `results/p1`, `results/latency`):
  sprinkler commanded 5.8 s (wall) after flaming, heat release halved 6.2 s
  after the water; sprinkler and damper commands confirmed in ~10 ms, doors
  and lifts in ~1 s (batched snapshots); a killed sensor gateway and a 30 s
  BuildSim outage recovered without intervention; TimescaleDB compression,
  retention and aggregate jobs scheduled; the egress unlock at a night-time
  alarm, the hybrid detector and TLS all worked; 116 tests passed inside
  Docker (3 TLS tests skipped there).
* Live loop (`results/latency` before the Docker run, fake BuildSim, x10 time): command → accepted
  by BuildSim and confirmed in ~10 ms for sprinklers, dampers and doors;
  gateway crash → offline alert in 1.5 s; BuildSim restart → equipment,
  actuator states and doors back in 2 s; 20 people evacuated, mean 54 s,
  last out after 105 s (simulated). The end-to-end tests
  (`tests/e2e_results.json`, 12 October): sprinkler applied ≤ 102 simulated
  s after flaming (an upper bound: measured from the test start, so it
  includes the services' start-up at x10), 726 egress doors unlocked at the
  alarm, a versioned plan followed by the occupants, 15 of 15 routes agreeing
  with BuildSim's route API, readings back 11.5 s after a broker outage. All
  120 tests (98 unit, 22 end-to-end) pass locally.

## Occupants and evacuation

`examples/api/scenario/populate_evacuation.py` runs as the `occupants`
service (60 people, Monday 07:30 at start), or on the host:

```bash
pip install requests paho-mqtt
MQTT_USERNAME=operator MQTT_PASSWORD=operator-dev \
    python3 examples/api/scenario/populate_evacuation.py http://127.0.0.1:9090
```

**Daily schedule** (`--schedule on`, on the simulated clock): everyone has an
office; people arrive between 07:00 and 09:00, go for lunch around
11:30–12:30 (half to the floor's lunch room, half out), visit meeting rooms
and colleagues, and leave between 16:00 and 18:00; nights and weekends the
building is empty. Room types come from the plan (rooms of 25 m² and more
are meeting rooms, the largest room on each floor is the lunch room, the
rest offices; `--room-types FILE.json` overrides). The number of people
present is published as an `occupancy` event every 15 simulated minutes.
The sensor gateways count these people for their CO₂ model.

**Evacuation** follows the building agent's plan (`--trigger plan`, the
default when the broker is reachable): it starts when the plan says
`evacuate`, moves only the people on the plan's floors (phased evacuation;
the others stay put until their floor is added), and ends with the agent's
`all_clear`. Without a broker it falls back to the agent's fire alerts
(`--trigger alerts`). The daily schedule follows the building clock
(`sim/clock`) when it is available, so people, door schedule and agent agree
on the time. Then it:

1. plans everyone's way out with a hazard-weighted shortest path over
   BuildSim's walkable graph (all floors, stairs only, lifts never): a metre
   through fire counts as 26 m, through smoke as 5 m, through a zone at risk
   of spread as 2 m. People detour around a fire when they can and take the
   least-exposed way when they can't. BuildSim's own `/api/graph/route`
   cannot weigh hazards, so it is used as a check instead: on every re-plan
   a few evacuees' routes, recomputed without hazards on the same door
   snapshot, are compared with BuildSim's (`route_check` events; they agree
   to within 5 %),
2. honours BuildSim's door snapshot: a blocked, locked or jammed door cannot
   be passed (except outwards from the room a person is in), a closed but
   unlocked fire door can; routes are re-planned whenever the doors change,
3. marks burning dead-end rooms "blocked" through the door gateway (source
   `evacuation`); a dead end is never on anyone's route, so this cannot cut
   anyone off,
4. lets people in a room that is *not* burning shelter in place when their
   only way out passes more than `--shelter-fire-metres` (3 m) of fire,
5. shows the situation in every open viewer (see Dashboard), moves people
   after a pre-movement delay, publishes `building/evacuation/progress` every
   5 s (people in pre-movement, evacuating, out, awaiting rescue per zone)
   and the `evacuation_replan`, `route_check` and `evacuation_complete`
   events.

Useful options: `--trigger manual --fire-rooms A109 --fire-at 15` (no
simulator or agent needed), `--events "5:fire:A1500B,15:fire:A1508"`, `--trigger
layers` (simulator truth instead of the agent's alerts), `--schedule off`
(everyone in from the start), `--doors api` (no door gateway: the script
writes `/api/doors` itself), `--people`, `--exits`, `--premovement 30:90`,
`--time-scale` (keep equal to `TIME_SCALE`).

## Doors

**Access control** (on the building clock, `ACCESS_HOURS` 07:00–18:00 on
weekdays): office and stairwell doors are locked (and closed) out of hours,
a hash-chosen `SECURE_SHARE` (5 %) of rooms (stores, server rooms) always;
fire exits never. Badge holders pass locked doors in everyday life (the
occupants honour only blocked and jammed doors then). On **any** fire alarm
the agent publishes `building/doors/egress/set` `{"lock_state": "unlocked"}`:
every office and stairwell door unlocks for egress (closed fire doors stay
closed, but unlocked), until `DOOR_RESET_SECONDS` after the last alarm, when
`"normal"` returns them to the schedule. Fire exits, stairwell doors and
secure rooms are also registered as BuildSim equipment (`door-<id>`, type
`door_lock`, category `access_control`) with a binary position sensor and a
lock actuator, kept in step with the snapshot.

The door gateway owns BuildSim's door collection (a `PUT /api/doors`
replaces it whole, so there is exactly one writer) and republishes the full
snapshot after every change and every 30 s (BuildSim restarts):

* one door per doorway (804): kind `door`, on the room's walkable-graph entry
  node, initial state from the scenario (`DOOR_OPEN_PROB`),
* the fire exits (`EXIT_ROOMS`): kind `fire_exit`,
* the lift lobbies (cross-floor elevator links): kind `door`, room only.

In BuildSim a locked, jammed or blocked door removes its entry node from
walkable routes; a closed but unlocked door stays routable. The fire
simulator reads the snapshot back every tick: an open door lets smoke and
flames through, a closed one only leaks (`CLOSED_DOOR_LEAKAGE`).

Rules the gateway enforces, whoever sends the command: a jammed door cannot
be operated (only a fault reset clears it); a locked door cannot be opened
without unlocking it; fire exits are never locked or blocked; only
`DOOR_COMMAND_SOURCES` may command doors (and the broker ACL limits who can
publish at all). `"state": "normal"` returns a door to its scenario state.

The agent's door policy: on an alarm the hold-open devices on that floor
release (`DOOR_RELEASE=level`; `zone` for only the alarmed zones; `off`), so
the fire doors (`FIRE_DOORS=all`, or `stairs` for stairwell and lift-lobby
doors only) close but stay unlocked; on a confirmed fire the lift lobbies
are blocked (`LIFT_RECALL`). Everything returns to normal
`DOOR_RESET_SECONDS` (120 simulated s) after the last alarm. A
`lock_state` command overrides the schedule until `"normal"`.

## Room layers (BuildSim heatmaps)

| Layer id | Unit | Meaning |
|---|---|---|
| `fire_hrr` | kW | Heat release rate of the fire in the room |
| `gas_temperature` | °C | Gas temperature at ceiling detectors |
| `ceiling_jet_velocity` | m/s | Gas velocity at the detectors (drives detector lag) |
| `smoke_obscuration` | %/m | Light obscuration per metre in the smoke layer |
| `visibility` | m | Visibility of a reflective sign (3/K) |
| `co`, `co2` | ppm | Gas concentrations in the smoke layer |
| `o2` | % | Oxygen concentration in the smoke layer |
| `smoke_layer_height` | m | Height of the smoke layer interface above the floor |

Only zones that are not at ambient are included. The ground-truth labels (fire phase, origin, nuisance source, sensor fault) travel on `sim/truth/...` instead; the agent never reads them, and sensors use only `sensor_fault`, to simulate the fault.

## Fire model

Per room, a simplified two-zone model built from standard fire-engineering
correlations:

1. **Incipient**: optional smouldering period, with little heat but smoky
   and CO-rich combustion, so smoke detectors respond before heat detectors.
2. **Growth**: t² fire (slow/medium/fast/ultrafast).
3. **Limits**: heat release is capped by the fuel (`HRR_PER_AREA_KW_M2` × floor
   area) and by the air the doors let in (Kawagoe, 1500·A·√H kW).
4. **Temperature**: MQH hot-layer correlation with thermal lag, plus the
   Alpert ceiling jet at the detector position.
5. **Flashover** at a 600 °C hot layer; the room then burns at its limit until
   70 % of the fuel load is used, then decays.
6. **Smoke**: the plume fills a hot ceiling layer that descends. Once it is
   below the door soffit, it spills into connected rooms and out through the
   external doors. The door jet entrains room air on the way
   (`DOOR_JET_ENTRAINMENT`), so smoke is cooler and thinner, and slower to
   move on, after every doorway. Soot, CO and CO₂ yields rise when the fire
   is short of air.
   * **Doors**: in each scenario a share of the doors (`DOOR_OPEN_PROB`) is
     open; the rest are closed and only leak (`CLOSED_DOOR_LEAKAGE`, 1 % of
     the area). The fire room's door is open in fixed mode. Rooms behind a
     closed door get far less smoke, later.
   * **Corridors** are split into cells of about `CORRIDOR_CELL_M` (4 m)
     along their walkable graph, and each door opens into its own cell, so
     smoke runs down the corridor and reaches the nearest doors first. The
     corridor's sensor reports its worst cell; the viewer draws smoke per
     cell.
   * **Stairs**: hot smoke rises up the stairwells (`cross_floor_edges`) to
     the floor above with a stack flow, Cd·A·√(2·g·H·ΔT/T), and spreads on
     that floor from there. Cold smoke stops rising.
   * **Removal**: background ventilation (`VENTILATION_ACH`), soot settling
     on surfaces (`SOOT_DEPOSITION_SECONDS`), and the smoke-purge damper
     (`PURGE_ACH` air changes per hour when open). Without the purge, smoke
     lingers for an hour, as in a real building with the HVAC off.
7. **Spread**: a neighbouring room ignites when its hot layer reaches
   `SPREAD_IGNITION_TEMP_C`, or after flames have projected through the shared
   door for `FLAME_SPREAD_DELAY_SECONDS` following flashover.
8. **Sprinklers**: heat release decays as exp(−t/43 s) (NIST), only 25 % as
   effectively after flashover. The water spray cools the gas but does not
   remove smoke. Turning the water off too early lets the fire regrow.
9. **Nuisance sources**: cooking (smoke, heat, a little CO), steam (heavy
   aerosol, no CO), dust (short burst), heater (heat only), smoking (light
   smoke with CO).

Fixed scenario (fast fire in A109, rules agent, 50 % of doors open): the
sprinkler releases at 160 s, smoke reaches the corridor and the rooms with
open doors over the next 2–4 minutes, reaches level 1 up the stairs next to
A104, and is purged to below 1 %/m everywhere within about 10 simulated
minutes. Without sprinkler and purge the smoke front reaches 7 doorways
away after about 7 minutes and gets thinner with every doorway.

A fast fire in a 25 m² office with an open door flashes over after about 6
simulated minutes. Post-flashover it reaches roughly 900 °C, ~10 % CO₂, ~1 %
CO and under 10 % O₂ in the hot layer.

## Settings

Shared by all processes (set once in `x-sim-env` in `compose.override.yaml`):

| Variable | Default | |
|---|---|---|
| `TIME_SCALE` | 5 | Simulated seconds per real second |
| `BUILDING_CLOCK_START` | mon 07:30 | Building clock when the simulator starts (`sim/clock`) |
| `MQTT_HOST`, `MQTT_PORT` | mosquitto, 1883 (8883 with TLS) | Message bus |
| `MQTT_TLS`, `MQTT_CA_FILE`, `MQTT_CERT_FILE`, `MQTT_KEY_FILE` | 0, `/certs/ca.crt`, –, – | TLS to the broker (`compose.tls.yaml`) |
| `SENSOR_SCOPE` | rooms+corridors | Zones with detectors and sprinklers (`rooms` for rooms only) |
| `PLAN_UNITS_PER_M` | 5 | Floor-plan scale, used for zone areas |
| `AMBIENT_TEMP_C` | 21 | |
| `CEILING_HEIGHT_M` | 2.7 | |
| `DOOR_WIDTH_M` / `DOOR_HEIGHT_M` | 0.9 / 2.0 | Room doorways (corridor openings: `CORRIDOR_OPENING_WIDTH_M` 1.8) |
| `VENTILATION_ACH` | 2 | Background air changes per hour |

Fire simulator:

| Variable | Default | |
|---|---|---|
| `SCENARIO_MODE` | fixed | `random` for data collection |
| `SCENARIO_LOG` | – | Optional JSON-lines file for scenario parameters (the database stores them anyway) |
| `CORRIDOR_FIRE_SHARE` | 0.15 | Random mode: share of fires starting in a corridor |
| `SCENARIO_SETTLE_SECONDS` | 300 | Quiet start of each random scenario |
| `START_FIRE_ROOMS` | `A109` | Fixed mode; any room or corridor, `level1/A2306` for other floors |
| `FIRE_START_DELAY_SECONDS` | 0 | Fixed mode, simulated delay before ignition |
| `INCIPIENT_SECONDS` | 120 | Fixed mode, smouldering time (0 = flaming at once) |
| `FIRE_GROWTH` | fast | Fixed mode, or a number α in kW/s² |
| `FUEL_LOAD_MJ_PER_M2` | 420 | Fixed mode |
| `EXTERNAL_DOORS` | 0 | Fixed mode, open windows in rooms (doorways come from the topology) |
| `HRR_PER_AREA_KW_M2` | 250 | |
| `SPREAD_IGNITION_TEMP_C` | 500 | |
| `FLAME_SPREAD_DELAY_SECONDS` | 90 | |
| `SPRINKLER_DECAY_TIME_SECONDS` | 43 | |
| `DOOR_OPEN_PROB` | 0.5 | Fixed mode: share of doors standing open (random mode draws 0.2–0.9) |
| `DOOR_SEED` | 1 | Fixed mode: which doors are open |
| `FIRE_ROOM_DOOR_OPEN` | 1 | Fixed mode: the fire room's doors are open |
| `CLOSED_DOOR_LEAKAGE` | 0.01 | Leakage of a closed door, fraction of its area |
| `CLOSED_DOOR_SPREAD_FACTOR` | 6 | A closed door holds flames back this many times longer |
| `DOOR_JET_ENTRAINMENT` / `OPENING_ENTRAINMENT` / `STAIR_ENTRAINMENT` | 1.0 / 0.3 / 1.0 | Air entrained per unit of smoke flowing through a door / corridor opening / stairwell |
| `CORRIDOR_CELL_M` | 4 | Length of a corridor cell |
| `STAIR_FLOW_AREA_M2`, `FLOOR_HEIGHT_M` | 0.8, 3.5 | Stairwell stack flow |
| `PURGE_ACH` | 10 | Air changes per hour of a fully open smoke-purge damper |
| `SOOT_DEPOSITION_SECONDS` | 1800 | Time constant of soot settling on surfaces |
| `RANDOM_SEED` | – | Makes random scenarios reproducible |

Building agent:

| Variable | Default | |
|---|---|---|
| `DETECTOR` | rules | `rules`, `model` or `hybrid` |
| `MODEL_PATH` | `/models/fire_lgbm.txt` | Thresholds are read from the matching `.json` |
| `ANOMALY_MODEL_PATH` | `/models/anomaly.pkl` | Hybrid detector (`train_anomaly.py`) |
| `HYBRID_FIRE_ALARM`, `HYBRID_FIRE_RELEASE` | 0.5, from the model | P(fire) of an anomaly that alarms / releases |
| `SPREAD_MODEL_PATH`, `SPREAD_RISK_THRESHOLD` | `/models/spread_lgbm.txt`, from `.json` | Spread prediction (baseline rule without a model) |
| `SPREAD_MAX_HOPS` | 3 | Spread risk only within this many doors/openings/stairs of a zone in alarm (the model was trained on neighbourhoods around fires and over-predicts far away) |
| `EGRESS_UNLOCK` | true | Release the access control on any fire alarm |
| `FULL_EVACUATION_SECONDS`, `ALL_CLEAR_SECONDS` | 300, 180 | Evacuation plan: phased → every floor; all-clear after the last alarm |
| `PLAN_SMOKE_THRESHOLD` | 10 %/m | Smoke zones listed in the plan |
| `MODEL_RELEASE_THRESHOLD`, `MODEL_ALARM_THRESHOLD` | from `.json` | Override the trained thresholds |
| `SMOKE_ALARM_THRESHOLD` | 8 %/m | Rules |
| `CO_ALARM_THRESHOLD` | 50 ppm | Rules |
| `FIRE_TEMP_THRESHOLD` | 68 °C | Rules, fixed-temperature release |
| `RATE_OF_RISE_THRESHOLD` | 8.3 °C/min | Rules |
| `RATE_MIN_CO`, `RATE_FAST`, `RATE_HOT_C` | 10 ppm, 16.6 °C/min, 50 °C | Evidence of combustion a rate-of-rise release needs (any one) |
| `ATTRIBUTION_MARGIN_C` | 2 | Rules, neighbour attribution |
| `SAFETY_TEMP_C` | 90 °C | Always releases (healthy temperature sensor only) |
| `SENSOR_FROZEN_SECONDS` | 180 | Simulated seconds without change before a sensor counts as frozen |
| `SENSOR_STALE_SECONDS` | 20 | Real seconds without a new value before a sensor counts as offline |
| `FALLBACK_SMOKE_THRESHOLD`, `FALLBACK_CO_THRESHOLD` | 10 %/m, 30 ppm | Rules release when the temperature sensor is faulty |
| `FAULT_DISCHARGE_FACTOR` | 2 | Longer minimum discharge with a faulty temperature sensor |
| `BLIND_ROOM_RELEASE` | true | Water for a room with no working fire sensors next to a confirmed fire |
| `MIN_DISCHARGE_SECONDS` | 300 | Simulated seconds of water before stopping |
| `CLEAR_TEMP_THRESHOLD` | 35 °C | |
| `SMOKE_PURGE` | true | Let the agent drive the smoke-purge dampers |
| `PURGE_ON_SMOKE`, `PURGE_OFF_SMOKE` | 3, 0.5 %/m | Open / close the damper |
| `PURGE_MIN_SECONDS` | 120 | Simulated seconds a damper stays open at least |
| `DOOR_RELEASE` | level | Doors closed on an alarm: `level`, `zone` or `off` |
| `LIFT_RECALL` | true | Block the lift lobbies on a confirmed fire |
| `DOOR_RESET_SECONDS` | 120 | Simulated seconds after the last alarm before doors and lifts return to normal |
| `DEGRADED_STALE_FRACTION` | 0.2 | Share of stale zones that puts the agent in Degraded |

Gateways: `GATEWAY_NAME`, `GATEWAY_LEVELS`, `TICK_SECONDS` (2), `ENV_STALE_SECONDS` (10: BuildSim unreadable this long -> stop publishing, Degraded).
Bridge: `MAX_REQUESTS_PER_SECOND` (150), `BRIDGE_REFRESH_SECONDS` (300), `BRIDGE_WORKERS` (8),
`BRIDGE_SETTLE_SECONDS` (20: a slow drift below the deadband is still shown after this long).
Recording: `DATABASE_URL`, `RECORD_DIR` (CSV), `RECORD_INTERVAL` (2 s), `QUIET_EVERY` (5).
Agent: `COMMAND_RESEND_SECONDS` (10) until the actuator gateway confirms.
Doors: `FIRE_DOORS` (all | stairs | none), `EXIT_ROOMS`, `DOOR_COMMAND_SOURCES`, `DOOR_REPUBLISH_SECONDS` (30),
`ACCESS_CONTROL` (true), `ACCESS_HOURS` (07:00-18:00), `SECURE_ROOMS` / `SECURE_SHARE` (0.05), `DOOR_EQUIPMENT` (true).
Bridge coverage: `COVERAGE_ON_SMOKE` (2 %/m), `COVERAGE_OFF_SMOKE` (1), `COVERAGE_SECONDS` (3).
Every service: `MQTT_USERNAME`, `MQTT_PASSWORD`, `HEARTBEAT_FILE` (/tmp/heartbeat),
`HEALTH_MAX_AGE_SECONDS` (30), `WATCHDOG_SECONDS` (120, 0 = off).
Storage: `COMPRESS_AFTER` (1 day), `RETENTION_DAYS` (30), `EVENT_RETENTION_DAYS` (365).

Sensor units: smoke is obscuration in **%/m**. Each room has temperature,
smoke, CO and CO₂ sensors.
