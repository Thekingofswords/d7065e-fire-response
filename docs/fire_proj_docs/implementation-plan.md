# Implementation plan

Revised to match what has been built (see
[system-architecture.md](system-architecture.md)). Part 0 checks the project
description item by item; part 1 is the state of every component; part 2
records how the items of the previous plan (D1–D6, P2–P6) were done, with
their deviations and measured results; part 3 is what remains. The first
plan's FastAPI services were replaced by the MQTT design; architecture
section 2 explains why.

## Part 0: the project description, item by item

> Deploy smoke, CO, and temperature sensors throughout a building. Train a
> model to distinguish real fires from false alarms caused by cooking, dust,
> or sensor failures. When a fire is detected, the system autonomously
> activates sprinklers, unlocks fire doors, computes evacuation routes,
> tracks occupants, and highlights safe escape paths on the 3D building model.

| Description item | Status | Where |
|---|---|---|
| Smoke, CO, temperature sensors throughout the building | ✅ | 956 zones × 4 sensors (incl. CO₂), three floor gateways, `sensor.py` |
| Smoke sensor process: realistic noise | ✅ | Photoelectric chamber lag, noise, range, drift/spike faults |
| CO sensor process: correlated with smoke (multi-sensor fusion) | ✅ | CO and soot from the same combustion; detectors fuse temperature, smoke and CO (`features.py`) |
| Temperature sensor process: thermal gradients between adjacent rooms | ✅ | Hot-layer door flows between rooms, corridor cells and floors; RTI heat-detector lag |
| Model: real fires vs cooking, dust, sensor failures | ✅ | LightGBM fire probability; four-class cause classifier (normal, fire, nuisance, fault) in the hybrid detector; sensor-health checks |
| Sprinkler actuators: receive commands, report state | ✅ | `actuator_gateway.py` |
| Fire-door actuators: lock/unlock, report position | ✅ | `door_gateway.py`: access-control schedule (locked out of hours), lock/unlock/open/close, confirmed on MQTT and in `/api/doors`; door-lock equipment with a binary position sensor and a lock actuator in BuildSim |
| "Unlocks fire doors" on detection | ✅ | On any alarm the agent releases every access-controlled door (`building/doors/egress/set`) and closes the fire doors unlocked |
| Data pipeline: time-series DB, feature extraction for ML | ✅ | MQTT → db-writer → TimescaleDB (hot/warm) → Parquet (cold); `features.py` shared by training and the agent (TimescaleDB policies: P1) |
| Anomaly detection model: autoencoder or isolation forest trained on "normal" time series | ✅ | Both: `anomaly.py`, `train_anomaly.py`, `DETECTOR=hybrid` |
| Fire-spread prediction model: adjacency graph + temperature gradients | ✅ | `spread_model.py`, `train_spread.py`; `building/risk` |
| Evacuation route planner: BuildSim walkable graph, dynamic re-routing | ✅ | Hazard-weighted routes on BuildSim's graph and doors, re-planned on every change, cross-checked against `/api/graph/route` |
| Autonomous control agent orchestrating detection, response **and evacuation** | ✅ | The agent owns the evacuation plan (phase, phased floors, zones, exits, all-clear); the occupants follow it and report progress |
| Tracks occupants | ✅ | Moving entities, occupancy per room, daily schedule on the building clock, evacuation status and progress |
| Highlights safe escape paths on the 3D model | ✅ | Exits and escape corridors green, one route per viewer |
| Dashboard: highlights red = fire, orange = risk, green = safe | ✅ | From the agent's plan: red burning, orange smoke and predicted spread, green exits and corridors (purple: people awaiting rescue) |
| Dashboard: coverage zones for smoke spread | ✅ | `PUT /api/coverage` from the measured smoke (bridge) |
| Dashboard: route display for evacuation paths | ✅ | `PUT /api/sessions/{id}/route` |

## Part 1: built

| # | Component | Files | State | Tests |
|---|---|---|---|---|
| 1 | Topology | `devices/topology.py` | Zones, doorways (with their entry nodes), stairs, lift lobbies and corridor cells from BuildSim's floor plans or `data/abuilding` | `test_topology.py` |
| 2 | Message bus, schemas, security | `mqtt_bus.py`, `messages.py`, `mosquitto/` (`mosquitto.conf`, `mosquitto-tls.conf`, `acl`, `passwd`, `passwd.test`, `make-passwd.sh`, `make-certs.sh`), `env.example`, `compose.tls.yaml` | Topic contract, reconnecting client with login and optional TLS, last wills; sensor-message validation; one broker user per service with an ACL; passwords from `.env` | `test_messages.py`, `test_broker_enforces_users_and_acl`, `test_tls.py` |
| 3 | Lifecycle | `lifecycle.py`, `healthcheck.py` | Starting → Syncing → Running ⇄ Degraded → Stopped in every service; status topic; heartbeat health check; watchdog; clean SIGTERM | `test_lifecycle.py`, `test_degraded_mode`, `test_gateway_degrades_when_buildsim_is_unreachable`, `test_graceful_shutdown` |
| 4 | Building clock | `simclock.py`, `fire_simulator.py` | The simulator publishes `sim/clock` (simulated time, week minute, "Mon 07:31"); agent, door gateway and occupants follow it with a continuous fallback | `test_clock.py` |
| 5 | Fire simulator | `fire_simulator.py`, `scenarios.py` | Zone fire model (doors, corridor cells, stairs, purge, sprinklers, nuisances); writes layers and effects to BuildSim, reads actuator and door states back | `test_physics.py`, `test_closing_doors_changes_the_physics` |
| 6 | Sensor gateways | `sensor.py` | One per floor; read room layers from BuildSim; instrument models and device faults; Degraded when BuildSim is unreachable | `test_sensor.py`, e2e |
| 7 | Actuator gateway | `actuator_gateway.py` | Owns the suppression equipment; validates, writes BuildSim, then confirms; re-registers after a BuildSim restart | `test_actuator.py`, e2e |
| 8 | Door gateway | `door_gateway.py` | Owns `/api/doors`; access-control schedule; door rules; door, zone, floor, lift and egress commands; confirms after BuildSim accepted the snapshot; door-lock equipment; fault injection | `test_doors.py`, `test_door_rules_are_enforced`, `test_fire_alarm_unlocks_egress_doors` |
| 9 | Building agent | `building_agent.py`, `features.py`, `health.py` | Rules / LightGBM / hybrid detector, state machine, safety net, purge, egress unlock, fire doors, lifts, spread risk, evacuation plan, Degraded mode, building clock | `test_agent.py`, `test_health.py`, e2e |
| 10 | Anomaly detection | `anomaly.py`, `train_anomaly.py`, `models/anomaly.*` | Isolation forest + autoencoder on normal data; cause classifier | `test_anomaly.py`, `results/anomaly` |
| 11 | Spread prediction | `spread_model.py`, `train_spread.py`, `models/spread_lgbm.*` | 20 graph features, LightGBM or baseline rule | `test_spread.py`, `results/spread` |
| 12 | BuildSim bridge | `buildsim_bridge.py` | Registers detectors, mirrors readings (deadbands, rate cap) and alerts; smoke coverage zones | e2e |
| 13 | Occupants | `examples/api/scenario/populate_evacuation.py` (service `occupants`) | Daily schedule on the building clock; follows the agent's plan (phased floors, all-clear); hazard- and risk-weighted routes, cross-checked with `/api/graph/route`; viewer highlights; progress reports | `test_schedule.py`, `test_agent_plan_drives_the_evacuation`, `test_routes_agree_with_buildsims_route_api`, `test_jammed_door_makes_the_evacuation_replan` |
| 14 | Storage | `datastore.py`, `db_writer.py`, `archive.py`, `recorder.py`, `export_dataset.py`, TimescaleDB | Samples, events, commands, states, scenarios; compression, retention, per-minute aggregate; Parquet archive | live run + `latency.py`; archive run |
| 15 | Dashboard | BuildSim viewer (highlights, coverage, routes), `grafana/` | Red/orange/green, smoke coverage, routes; Grafana tiles, time series, decision log | `test_dashboard_highlights_and_coverage` |
| 16 | Training | `generate_dataset.py`, `train_model.py`, `train_anomaly.py`, `train_spread.py`, `models/` | Offline scenarios with the full agent policy (incl. fire doors); scenario-grouped splits; held-out reports | `models/*.json`, `results/` |
| 17 | Tests | `devices/tests/` | 98 unit tests, 22 end-to-end and fault-injection tests (including a broker outage) (fake BuildSim with BuildSim's door, coverage and highlight validation, real broker users and ACL); `docker compose --profile test run --rm tests` | — |
| 18 | Experiments | `devices/experiments/` | Response over time, baseline and sensor-fault comparison (none / rules without fire doors / rules / model / hybrid), scaling, live latency | — |

## Part 2: done since the previous plan

### D1. Locked doors and unlocking them on a fire ✅

* **Built**: access-control schedule in the door gateway on the building
  clock (`ACCESS_HOURS` 07:00–18:00 on weekdays; offices and stairwells
  locked and closed out of hours; a deterministic `SECURE_SHARE` of 5 % of
  rooms always; fire exits never). The agent publishes
  `building/doors/egress/set` `{"lock_state": "unlocked"}` on any alarm and
  `"normal"` `DOOR_RESET_SECONDS` after the last one. Door-lock equipment
  (`door-<id>`, `door_lock`, binary position sensor, lock actuator) for fire
  exits, stairwell and secure doors. The occupants honour only blocked and
  jammed doors in everyday life (badges), every locked door in an
  evacuation; people can always leave their own room (panic hardware).
* **Deviation**: secure rooms are chosen by a hash, or listed in
  `SECURE_ROOMS`, instead of a `config/locked_doors.json`.
* **Result**: at a night-time alarm 765 doors unlock within one tick; fire
  exits are never locked (`test_fire_alarm_unlocks_egress_doors`).

### D2. Anomaly detection ✅

* **Built**: `anomaly.py` (scaler, isolation forest, MLP autoencoder 38 →
  16 → 8 → 16 → 38 on the zone's own features and sensor-health flags,
  trained on normal periods only; score = the larger of the two normalised
  scores) and a LightGBM cause classifier (normal / fire / nuisance / fault,
  51 features plus the anomaly score); `train_anomaly.py`;
  `DETECTOR=hybrid` (rules plus: anomaly classified as fire → alarm, water
  above the trained confidence; any other anomaly → "check zone" alert).
* **Deviations**: the cause classifier lives in `train_anomaly.py`, not in
  `train_model.py`; `anomaly_score` is a feature of the cause classifier
  (the supervised stage) rather than of the fire model, so the fire model
  stays independent of the anomaly model.
* **Result** (held-out): fires and nuisances 100 % anomalous within 60 s;
  sensor faults 19 % within 60 s and 88 % within 5 minutes (drift and stuck
  sensors develop slowly, below the 98 % target); 0.45 false anomalies per
  100 normal zone-hours; cause accuracy 99.5 / 89.9 / 92.9 / 79.7 %
  (normal / fire / nuisance / fault). In the closed loop the hybrid releases
  all 72 fires, mostly while they still smoulder, with the least smoke of
  all policies (1,503 zone-minutes) and 2 releases in zones that never
  burned.

### D3. Fire-spread prediction ✅

* **Built**: `spread_model.py` (20 features over the adjacency graph,
  weighted by door openness from the door gateway's confirmed states),
  `train_spread.py` (closed-loop scenarios, labels from each zone's future),
  `building/risk` from the agent.
* **Deviation**: evaluation in `train_spread.py` (`results/spread`) instead
  of a separate `experiments/spread.py`; pre-emptive purge for at-risk zones
  was not added (the purge already opens at 3 %/m).
* **Result**: AUC 0.88 (baseline 0.67); precision 0.69, recall 0.46 at the
  chosen threshold; 68 % of the zones that become untenable flagged ≥ 60 s
  ahead (baseline 38 %), below the 80 % aimed for.

### D4. The agent orchestrates the evacuation ✅

* **Built**: `BuildingPolicy.evacuation_plan` (phases normal → evacuate →
  all_clear; phased floors that never shrink; full evacuation on a confirmed
  fire or after 300 s), published versioned and retained; the occupants
  follow it (`--trigger plan`, the default with a broker), move only the
  plan's floors, end with the agent's all-clear, and report
  `building/evacuation/progress`; people awaiting rescue raise a critical
  alert.
* **Result**: `test_plan_is_phased_then_full_then_all_clear`,
  `test_agent_plan_drives_the_evacuation`.

### D5. Dashboard: red = fire, orange = risk, green = safe, smoke coverage ✅

* **Built**: highlight colours from the plan in the occupants service; smoke
  coverage zones in the bridge (`PUT /api/coverage`, hysteresis 2 / 1 %/m,
  radius up to the room size, grey → near black); the fake BuildSim
  validates coverage and highlights as BuildSim does.
* **Result**: `test_dashboard_highlights_and_coverage` (no PUT rejected).
  Viewer screenshots belong to P1.

### D6. Route planner cross-check ✅

* **Built**: on every re-plan, up to 10 evacuees' first same-floor legs are
  recomputed without hazards on the same door snapshot and compared with
  `GET /api/graph/route` (`route_check` events, totals in the progress
  report). Justification in the architecture's decision table.
* **Result**: every check agreed (`test_routes_agree_with_buildsims_route_api`).

### P2. Retrain and rerun ✅

Data regenerated (1,250 scenarios, now with the agent's fire-door policy in
the controlled runs), fire model, anomaly model and spread model retrained,
`response.py`, `baseline.py` (with and without `--faults`, five policies)
and `scaling.py` rerun; README and architecture tables updated from this
build.

### P3. One clock ✅

`simclock.py`: the simulator publishes `sim/clock`; the agent
(`PolicyClock`: never backwards, bounded steps), the door schedule and the
occupants follow it, falling back to their own clock without a jump.
`test_clock.py`.

### P4. Broker TLS and secrets ✅

TLS listener on 8883 (`mosquitto-tls.conf`, `make-certs.sh`,
`compose.tls.yaml`, `MQTT_TLS=1` in `mqtt_bus.py` with certificate and
hostname verification); passwords from `.env` (`env.example`,
`make-passwd.sh`; development defaults otherwise; test broker on
`passwd.test`). `test_tls.py` runs a real broker with the TLS config.

### P5. Report artefacts ✅

Architecture updated: new requirements FR-10…FR-15, NFR-11, SEC-02 with
their tests; containers and topics; a second dynamic diagram (alarm to
evacuation); decision table (anomaly stage, spread model, evacuation
ownership and routing, access control, transport security); results.

### P6. False releases with the fire doors ✅

Cause: cooking behind closed fire doors warmed rooms by 9–12 °C/min with
~5 ppm CO and set off the rate-of-rise rule (10 of 11 releases). A rate of
rise now needs evidence of combustion: CO ≥ 10 ppm, a rise ≥ 16.6 °C/min or
≥ 50 °C (flaming fires show all three). Rules with fire doors: 11 → 1
release in zones that never burned (0.1 per 100 zone-hours), all 70 fires
still released in time; with a broken sensor 60/60 released, 18 → 10
precautionary releases.

## Part 3: live run (P1) ✅

Run on the real BuildSim with Docker Compose on 8 October 2026 (evidence in
`results/p1` and `results/latency`, numbers in architecture section 19):
daytime fire with occupants (Run A, with a sensor-gateway kill and a 30 s
BuildSim outage), night-time fire with locked doors (Run B), the hybrid
detector (Run C), TLS (Run D), and the test suite inside Docker (116 passed,
3 TLS tests skipped for lack of a broker in the test image).

Defects the live run found, all fixed with tests:

| Defect | Cause | Fix | Test |
|---|---|---|---|
| Viewer camera kept flying to the evacuees | BuildSim's viewer frames every route it receives; a new route went out every 5 s | One route per evacuation by default (`ROUTE_DISPLAY=once`; `cycle`, `off`) | `test_route_is_sent_once_so_the_camera_stays_free` |
| 7 of 8 people never got out | Spread risk flickered across the building; every change re-planned and reset the stair timer | Risk only within 3 links of an alarm; spread-only re-plans at most every 10 s; stair progress kept across re-plans | `test_replanning_on_the_stairs_keeps_the_progress` |
| People froze in the viewer | A few upper-floor rooms extend past the floor page; BuildSim rejected the whole entity list | Positions kept on the page | `test_entity_positions_stay_on_the_floor_page` |
| A sprinkler "adopted" at start-up | A retained "on" from a previous run survived `docker compose down` | Actuator gateway takes the states from BuildSim at start and corrects stale confirmations; the agent drops such an adoption | `test_stale_sprinkler_confirmation_is_corrected` |
| Day/night and detector settings ignored | `BUILDING_CLOCK_START` and `DETECTOR` were fixed in the compose file | Read from the shell (`${BUILDING_CLOCK_START:-mon 07:30}`, `${DETECTOR:-rules}`) | live runs B and C |
| Route cross-check found nothing to check at night | People start in locked offices; BuildSim's route API has no panic-hardware exit | Compare from the first node outside the person's room; also check after door changes | `test_routes_agree_with_buildsims_route_api` |

Nothing from the plan remains open.

## Known limitations

* The learned models learn the simulator's physics and must be retrained
  when it changes (about 15 minutes for all three); the anomaly model is
  pickled with a pinned scikit-learn version.
* Slowly developing sensor faults (drift, a stuck sensor) become anomalous
  only after minutes.
* Spread prediction flags two thirds of the zones a minute ahead; at the
  chosen threshold it misses about half of the onsets.
* Occupant CO₂ is modelled in the sensor and does not see the purge damper.
* Room types for the occupancy schedule are guessed from floor areas
  (`--room-types` overrides them); secure rooms are chosen by a hash unless
  listed.
