"""Input validation of sensor messages (messages.py): corrupt data must be
rejected before it reaches the agent, the bridge or the database."""
import math
import time

import pytest

import messages


def good(**kw):
    msg = {"ts": time.time(), "gateway": "g", "temperature": 21.5, "smoke": 0.1,
           "co": 0, "co2": 430}
    msg.update(kw)
    return msg


def test_valid_message_passes():
    clean = messages.parse_sensor(good())
    assert clean["temperature"] == 21.5


@pytest.mark.parametrize("payload, reason", [
    ("garbage", "malformed"),
    ([1, 2, 3], "malformed"),
    (None, "malformed"),
    ({"ts": 1.0}, "missing"),
    (good(smoke="high"), "not_numeric"),
    (good(temperature=math.nan), "not_numeric"),
    (good(co=math.inf), "not_numeric"),
    (good(temperature=True), "not_numeric"),
    (good(smoke=-5), "out_of_range"),
    (good(temperature=5000), "out_of_range"),
    (good(ts=time.time() + 3600), "future"),
])
def test_corrupt_messages_are_rejected(payload, reason):
    with pytest.raises(messages.Invalid) as err:
        messages.parse_sensor(payload)
    assert err.value.reason == reason


def test_duplicates_and_out_of_order_are_dropped_and_counted():
    v = messages.SensorValidator()
    first = good(ts=1000.0)
    assert v.check("level0/A109", first, now=2000.0) is not None
    assert v.check("level0/A109", dict(first), now=2000.0) is None          # duplicate
    assert v.check("level0/A109", good(ts=999.0), now=2000.0) is None       # older
    assert v.check("level0/A110", good(ts=999.0), now=2000.0) is not None   # other zone
    assert v.check("level0/A109", "x", now=2000.0) is None
    counts, examples = v.take_counts()
    assert counts == {"duplicate": 2, "malformed": 1}
    assert v.take_counts()[0] == {}
