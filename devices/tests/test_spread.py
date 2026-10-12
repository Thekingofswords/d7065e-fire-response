"""Fire-spread prediction features and baseline (spread_model.py)."""
import spread_model as sm


def _features(temp=21.0, smoke=0.0):
    return {"temperature": temp, "temperature_rate_60": 0.0, "temperature_excess": temp - 21,
            "smoke": smoke, "smoke_rate_60": 0.0, "co": 0.0}


def test_neighbour_of_a_fire_is_at_risk_and_a_closed_door_lowers_it(topo):
    zones = topo.sensor_zones()
    feats = sm.SpreadFeatures(topo, zones)
    fire, (other, pair, kind) = next((z, link) for z in zones for link in feats.links[z]
                                     if link[2] == "door")
    room_features = {z: _features() for z in zones}
    room_features[fire] = _features(temp=300, smoke=40)
    decisions = {fire: {"state": "SUPPRESSING", "p_fire": 1.0}}
    rows = feats.rows(room_features, decisions, {pair: True})
    row = dict(zip(sm.FEATURES, rows[other]))
    assert row["fire_hops"] == 1 and row["nb_fire_open"] == 1.0
    assert sm.baseline_risk(rows[other]) >= 0.6
    assert sm.baseline_risk(rows[fire]) == 0.0, "already burning"
    closed = dict(zip(sm.FEATURES, feats.rows(room_features, decisions, {pair: False})[other]))
    assert closed["nb_max_gradient_open"] < row["nb_max_gradient_open"]
    assert closed["nb_fire_open"] == sm.CLOSED_DOOR


def test_quiet_building_has_no_risk(topo):
    zones = topo.sensor_zones()
    feats = sm.SpreadFeatures(topo, zones)
    model = sm.SpreadModel(path="/nonexistent/spread.txt")
    probs = model.predict(feats.rows({z: _features() for z in zones}, {}))
    assert len(probs) == len(zones)
    assert not model.at_risk(probs)
