"""Broker TLS (P4): mosquitto/make-certs.sh certificates, the TLS listener
of mosquitto-tls.conf, and mqtt_bus with MQTT_TLS=1: a verified connection
with a service user works, plain TCP to the TLS port and an unknown CA fail.

Needs mosquitto and openssl; skipped otherwise."""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
MOSQ = os.path.join(ROOT, "mosquitto")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def tls_broker():
    binary = shutil.which("mosquitto") or (
        "/usr/sbin/mosquitto" if os.path.exists("/usr/sbin/mosquitto") else None)
    if not binary or not shutil.which("openssl"):
        pytest.skip("needs mosquitto and openssl")
    tmp = tempfile.mkdtemp(prefix="fire-tls-")
    os.chmod(tmp, 0o755)
    shutil.copytree(MOSQ, os.path.join(tmp, "mosquitto"),
                    ignore=shutil.ignore_patterns("certs"))
    subprocess.run(["sh", os.path.join(tmp, "mosquitto", "make-certs.sh")], check=True,
                   capture_output=True)
    certs = os.path.join(tmp, "mosquitto", "certs")
    port = _free_port()
    # The real TLS config, pointed at the temporary files and a free port.
    with open(os.path.join(MOSQ, "mosquitto-tls.conf")) as f:
        conf = f.read()
    conf = (conf.replace("listener 1883", f"listener {_free_port()} 127.0.0.1")
                .replace("listener 8883", f"listener {port} 127.0.0.1")
                .replace("/mosquitto/certs", certs)
                .replace("/mosquitto/config/passwd", os.path.join(tmp, "mosquitto", "passwd.test"))
                .replace("/mosquitto/config/acl", os.path.join(tmp, "mosquitto", "acl"))
                .replace("persistence true", "persistence false")
                .replace("persistence_location /mosquitto/data/\n", ""))
    path = os.path.join(tmp, "broker.conf")
    with open(path, "w") as f:
        f.write(conf)
    for name in os.listdir(os.path.join(tmp, "mosquitto")):
        full = os.path.join(tmp, "mosquitto", name)
        if os.path.isfile(full):
            os.chmod(full, 0o644)
    os.chmod(certs, 0o755)
    for name in os.listdir(certs):
        os.chmod(os.path.join(certs, name), 0o644)
    broker = subprocess.Popen([binary, "-c", path], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
    time.sleep(0.8)
    if broker.poll() is not None:
        pytest.fail(f"broker did not start: {broker.stdout.read().decode()[-500:]}")
    yield port, certs
    broker.kill()
    shutil.rmtree(tmp, ignore_errors=True)


def _client(port, ca, user="agent"):
    code = (
        "import mqtt_bus, sys\n"
        "bus = mqtt_bus.Bus('tls-test')\n"
        "bus.client.connect(bus.host, bus.port, 5)\n"
        "bus.client.loop_start()\n"
        "ok = bus.connected.wait(5)\n"
        "print('CONNECTED' if ok else 'NOT CONNECTED')\n"
    )
    env = dict(os.environ, MQTT_TLS="1", MQTT_HOST="localhost", MQTT_PORT=str(port),
               MQTT_CA_FILE=ca, MQTT_USERNAME=user, MQTT_PASSWORD=f"{user}-dev",
               PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                          text=True, timeout=30)


def test_service_connects_over_verified_tls(tls_broker):
    port, certs = tls_broker
    result = _client(port, os.path.join(certs, "ca.crt"))
    assert "CONNECTED" in result.stdout and "NOT" not in result.stdout, result.stderr[-500:]


def test_unknown_ca_is_refused(tls_broker, tmp_path):
    port, _ = tls_broker
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=other", "-keyout", str(tmp_path / "k"), "-out",
                    str(tmp_path / "c")], check=True, capture_output=True)
    result = _client(port, str(tmp_path / "c"))
    assert "CONNECTED" not in result.stdout.replace("NOT CONNECTED", "")


def test_plain_mqtt_on_the_tls_port_fails(tls_broker):
    port, _ = tls_broker
    import paho.mqtt.client as mqtt
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="plain")
    client.username_pw_set("agent", "agent-dev")
    connected = []
    client.on_connect = lambda c, u, f, rc, p=None: connected.append(rc)
    try:
        client.connect("127.0.0.1", port, 5)
        client.loop_start()
        time.sleep(2)
    except OSError:
        pass
    finally:
        client.loop_stop()
    assert not any(not rc.is_failure for rc in connected)
