"""Shared fixtures. Run from the repository root or from devices/:

    pip install -r devices/requirements.txt pytest
    pytest devices/tests                 # unit tests (seconds)
    pytest devices/tests -m e2e          # end-to-end + fault injection (~1 min)
    docker compose --profile test run --rm tests    # everything, in Docker

The building comes from BUILDING_DATA_DIR or ../data/abuilding.
"""
import os
import sys

import pytest

DEVICES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DEVICES)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = os.environ.get("BUILDING_DATA_DIR") or os.path.join(DEVICES, "..", "data", "abuilding")


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end tests that start the whole stack")


def pytest_collection_modifyitems(config, items):
    # e2e tests only run when asked for (-m e2e, or RUN_E2E=1).
    if "e2e" in (config.getoption("-m") or "") or os.environ.get("RUN_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="end-to-end: run with -m e2e")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def data_dir():
    if not os.path.isdir(DATA_DIR):
        pytest.skip(f"building data not found at {DATA_DIR} (set BUILDING_DATA_DIR)")
    return os.path.abspath(DATA_DIR)


@pytest.fixture(scope="session")
def topo(data_dir):
    import topology
    return topology.Topology.from_files(data_dir)


@pytest.fixture()
def quiet_sim(monkeypatch):
    """The fire model without its console log."""
    import fire_simulator
    monkeypatch.setattr(fire_simulator, "print", lambda *a, **k: None, raising=False)
    return fire_simulator
