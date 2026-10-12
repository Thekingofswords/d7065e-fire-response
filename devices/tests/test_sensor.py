"""Instrument models and the gateway's reading of BuildSim (sensor.py)."""
import random

import common
import sensor


def env(gas=21.0, smoke=0.0, co=0.0, velocity=0.1, fault=0):
    return {common.LAYER_GAS_TEMP: gas, common.LAYER_OBSCURATION: smoke,
            common.LAYER_CO: co, common.LAYER_JET_VELOCITY: velocity,
            common.LAYER_CO2: common.AMBIENT_CO2_PPM, common.LAYER_SENSOR_FAULT: fault}


def test_heat_detector_lags_the_gas():
    inst = sensor.Instruments("A109", random.Random(1), 80.0)
    inst.step(env(gas=200.0, velocity=1.0), 10.0, 0, 0.0)
    t = inst.readings()["temperature"]
    assert 21.5 < t < 150.0          # rising, but far from the gas temperature
    for _ in range(60):
        inst.step(env(gas=100.0, velocity=1.0), 10.0, 0, 0.0)
    assert abs(inst.readings()["temperature"] - 100.0) < 2.0


def test_frozen_temperature_fault():
    inst = sensor.Instruments("A109", random.Random(1), 80.0)
    inst.step(env(gas=30.0, fault=2), 10.0, 0, 0.0)
    stuck = inst.readings()["temperature"]
    for _ in range(20):
        inst.step(env(gas=300.0, velocity=1.0, fault=2), 10.0, 0, 0.0)
        assert inst.readings()["temperature"] == stuck


def test_offline_device_repeats_its_last_reading():
    inst = sensor.Instruments("A109", random.Random(1), 80.0)
    inst.step(env(), 10.0, 0, 0.0)
    last = inst.readings()
    inst.step(env(gas=300.0, fault=5), 10.0, 0, 0.0)
    assert inst.offline and inst.readings() == last


def test_occupants_raise_co2():
    inst = sensor.Instruments("A109", random.Random(1), 80.0)
    for _ in range(60):
        inst.step(env(), 30.0, 5, 0.0)
    assert inst.readings()["co2"] > common.AMBIENT_CO2_PPM + 200


def test_environment_is_read_from_room_layers(monkeypatch):
    layers = [
        {"id": common.LAYER_GAS_TEMP, "values": {"level0/A109": 85.0, "level1/X": 30.0}},
        {"id": common.LAYER_OBSCURATION, "values": {"level0/A109": 12.5}},
        {"id": common.LAYER_VISIBILITY, "values": {"level0/A109": 2.0}},   # not a sensor input
        {"id": common.LAYER_CO, "values": {"level0/A109": "bad"}},
    ]
    monkeypatch.setattr(common, "call", lambda method, path, body=None: layers)
    truth = sensor.read_environment({"level0/A109", "level0/A110"})
    assert truth == {"level0/A109": {common.LAYER_GAS_TEMP: 85.0, common.LAYER_OBSCURATION: 12.5}}
