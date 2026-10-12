# Experiments

Scripts that produce the evidence for the report's evaluation section. Each
writes CSV files (pgfplots-friendly), a `summary.md` table and a PNG figure
into its `--out` folder. Run them from `devices/`:

```bash
pip install -r requirements.txt          # numpy, lightgbm, matplotlib, psycopg
cd devices
python experiments/response.py  --out ../results/response
python experiments/baseline.py  --out ../results/baseline --scenarios 150 \
    --policies none,rules_no_doors,rules,model,hybrid
python experiments/baseline.py  --out ../results/faults   --scenarios 60 --faults \
    --policies none,rules_no_doors,rules,model,hybrid
python experiments/scaling.py   --out ../results/scaling
python experiments/latency.py   --out ../results/latency  --since "1 hour"   # needs a live run
```

The model evaluations live with their training scripts (run from
`devices/`, they write the same kind of summary):

```bash
python generate_dataset.py --scenarios 1250 --zones 8 --out ../recordings/offline
python train_model.py   ../recordings/offline --out ../models/fire_lgbm.txt
python train_anomaly.py ../recordings/offline --out ../models/anomaly.pkl --report ../results/anomaly
python train_spread.py  --scenarios 800 --out ../models/spread_lgbm.txt --report ../results/spread
```

The building is read from `../data/abuilding` (or `BUILDING_DATA_DIR`), the
trained model from `../models/fire_lgbm.txt` (or `--model`), the anomaly
model from `../models/anomaly.pkl` (or `ANOMALY_MODEL_PATH`). Without a
model, its policy is skipped. Any policy can be run without the
agent's fire-door policy by adding `_no_doors` (e.g. `--policies
none,rules_no_doors,rules`).

| Script | Question it answers | Runs on |
|---|---|---|
| `response.py` | What does one fire look like with and without the system? Gas temperature in the fire room and smoke along the corridor, in the room across it and upstairs, over 40 simulated minutes, for no system / rules agent without the fire-door policy (`rules_no_doors`) / rules agent / model agent. | Offline (same physics, instruments and policy as the live services) |
| `baseline.py` | Over many random scenarios, how much better is the system than no system, and the model than the rules? Flashovers, release delay, fire spread, smoke exposure, false sprinkler releases, water used. `--faults`: the same with a broken sensor in the fire room. | Offline, all CPU cores |
| `scaling.py` | Where does it break as the building grows? Agent decision time, physics tick time and BuildSim bridge load for 1×–16× the building, against their budgets. | Offline |
| `train_anomaly.py` (D2) | Are fires, nuisances and sensor faults anomalous, how soon, how many false anomalies on normal data, how well is the cause classified, and what does the hybrid add to the rules' releases? | Generated data |
| `train_spread.py` (D3) | Which zones become untenable within 5 minutes: AUC, precision/recall, share flagged ≥ 60 s ahead, against the neighbour baseline | Closed-loop simulation |
| `latency.py` | How fast is the live system end to end? Ignition/flaming → sprinkler command, command → accepted by BuildSim and confirmed (sprinklers, dampers, doors, lifts), sprinkler → heat release falling, sensor outage, rejected corrupt messages and door commands, evacuation times. | The TimescaleDB of a live `docker compose` run |

`closed_loop.py` is the shared offline runner: `FireModel` → `sensor.Instruments`
→ `BuildingPolicy` → sprinklers/dampers back into the model, the loop the
live services form through BuildSim and MQTT, minus the network.

## A live run for `latency.py`

```bash
docker compose up -d --build
# let the fixed fire play out (about 2 real minutes), then a sensor crash:
docker compose kill sensor-gateway-level1
sleep 30
docker compose start sensor-gateway-level1
sleep 60
python experiments/latency.py --since "15 minutes" --out ../results/latency
```

For many fires, set `SCENARIO_MODE: random` for `fire-simulator` in
`compose.override.yaml` and leave it running for an hour.

The end-to-end tests (`pytest tests -m e2e`) measure the same things against
a fake BuildSim and write them to `tests/e2e_results.json`.
