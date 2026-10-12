"""MQTT helper and topic contract shared by every service.

Topics (room names are URL-encoded in topics: some contain "+", which MQTT
reserves as a wildcard):

  building/<level>/<room>/sensors                 readings of one detector head
      {"ts", "temperature", "smoke", "co", "co2", "gateway"}      retained
  building/<level>/<room>/actuator/<name>/set     command to an actuator
      {"state", "reason", "id", "ts", "source"}                   QoS 1
  building/<level>/<room>/actuator/<name>/state   confirmed actuator state
      {"state", "ts", "command_id", "reason"}                     retained, QoS 1
  building/gateways/<gateway>/status              "online"/"offline" (last will)
  building/agent/status                           "online"/"offline" (last will)
  building/alerts                                 current alert list   retained
  building/events                                 event stream: alarms, commands,
      {"ts", "type", "severity", "level", "room", "message", ...}    QoS 1

  sim/truth/<level>/<room>    ground truth + labels from the simulator, for
                              the database only (training, evaluation); no
                              device reads it. Sensors read the physical
                              environment from BuildSim's room layers.  retained
  sim/scenario                current scenario parameters             retained
  sim/clock                   building clock (simclock.py): {"scenario_id",
                              "scenario_time", "sim_time", "week_minute",
                              "clock", "ts"}                          retained
  sim/status                  "online"/"offline" (last will)

  building/doors/<door>/set, /state, /fault   doors (see door_gateway.py)
  building/doors/egress/set                   every access-controlled door
  building/risk               fire-spread prediction (agent): {"zones": {zone: p},
                              "horizon_s", "model", "ts"}             retained
  building/evacuation/plan    the agent's evacuation plan (phase, levels to
                              evacuate, fire/smoke/risk zones, doors, exits,
                              version)                                retained
  building/evacuation/progress  occupants: people out, inside, awaiting rescue
                                                                      retained
  building/<level>/<room>/doors/set           every door of a zone
  building/<level>/doors/set, building/lifts/set
  building/services/<name>/status             lifecycle of non-gateway services

Configuration: MQTT_HOST (default "mosquitto"), MQTT_PORT (1883, or 8883
with TLS), MQTT_USERNAME / MQTT_PASSWORD when the broker requires them (it
does in the compose setup: mosquitto/passwd and mosquitto/acl; the passwords
come from .env, see .env.example). TLS: MQTT_TLS=1 verifies the broker's
certificate against MQTT_CA_FILE (default /certs/ca.crt; mosquitto/make-certs.sh
creates a local CA and the broker certificate, compose.tls.yaml switches every
service over); MQTT_CERT_FILE / MQTT_KEY_FILE add a client certificate.
"""
import json
import os
import threading
import time
import urllib.parse
import uuid

import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_TLS = os.environ.get("MQTT_TLS", "0").strip().lower() in ("1", "true", "yes")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883" if MQTT_TLS else "1883"))
MQTT_CA_FILE = os.environ.get("MQTT_CA_FILE", "/certs/ca.crt")
MQTT_CERT_FILE = os.environ.get("MQTT_CERT_FILE") or None
MQTT_KEY_FILE = os.environ.get("MQTT_KEY_FILE") or None
MQTT_USERNAME = os.environ.get("MQTT_USERNAME") or None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None


def enc(segment):
    return urllib.parse.quote(segment, safe="")


def dec(segment):
    return urllib.parse.unquote(segment)


# -- topic builders -----------------------------------------------------------

def sensors_topic(level, room):
    return f"building/{enc(level)}/{enc(room)}/sensors"


def actuator_set_topic(level, room, actuator):
    return f"building/{enc(level)}/{enc(room)}/actuator/{enc(actuator)}/set"


def actuator_state_topic(level, room, actuator):
    return f"building/{enc(level)}/{enc(room)}/actuator/{enc(actuator)}/state"


def gateway_status_topic(gateway):
    return f"building/gateways/{enc(gateway)}/status"


def service_status_topic(name):
    return f"building/services/{enc(name)}/status"


def truth_topic(level, room):
    return f"sim/truth/{enc(level)}/{enc(room)}"


ALERTS_TOPIC = "building/alerts"
RISK_TOPIC = "building/risk"
EVAC_PLAN_TOPIC = "building/evacuation/plan"
EVAC_PROGRESS_TOPIC = "building/evacuation/progress"
DOOR_STATE_SUB = "building/doors/+/state"
EVENTS_TOPIC = "building/events"
AGENT_STATUS_TOPIC = "building/agent/status"
SCENARIO_TOPIC = "sim/scenario"
CLOCK_TOPIC = "sim/clock"
SIM_STATUS_TOPIC = "sim/status"

SENSORS_SUB = "building/+/+/sensors"
ACTUATOR_SET_SUB = "building/+/+/actuator/+/set"
ACTUATOR_STATE_SUB = "building/+/+/actuator/+/state"
GATEWAY_STATUS_SUB = "building/gateways/+/status"


def parse_room_topic(topic):
    """"building/level0/A109/sensors" -> ("level0", "A109", rest parts)."""
    parts = topic.split("/")
    if len(parts) >= 4 and parts[0] in ("building", "sim"):
        if parts[0] == "sim":  # sim/truth/<level>/<room>
            return dec(parts[2]), dec(parts[3]), parts[4:]
        return dec(parts[1]), dec(parts[2]), [dec(p) for p in parts[3:]]
    return None, None, []


# -- client -------------------------------------------------------------------

def configure_tls(client, ca_file=None, cert_file=None, key_file=None):
    """Encrypt the connection and verify the broker against our CA (hostname
    checked: the broker certificate names mosquitto and localhost)."""
    import ssl

    client.tls_set(ca_certs=ca_file or MQTT_CA_FILE, certfile=cert_file or MQTT_CERT_FILE,
                   keyfile=key_file or MQTT_KEY_FILE, cert_reqs=ssl.CERT_REQUIRED,
                   tls_version=ssl.PROTOCOL_TLS_CLIENT)


class Bus:
    """A paho client that connects in the background, reconnects forever,
    re-subscribes after reconnects and decodes JSON payloads.

        bus = Bus("building-agent", status_topic=AGENT_STATUS_TOPIC)
        bus.subscribe(SENSORS_SUB, on_reading)       # handler(topic, payload)
        bus.start()
        bus.publish(topic, {"state": "on"}, qos=1)
    """

    def __init__(self, name, status_topic=None, host=None, port=None,
                 username=None, password=None):
        self.name = name
        self.status_payload = None   # callable -> dict published on (re)connect
        self.status_topic = status_topic
        self.host = host or MQTT_HOST
        self.port = int(port or MQTT_PORT)
        self.handlers = []
        self.connected = threading.Event()
        client_id = f"{name}-{uuid.uuid4().hex[:6]}"
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        username = username or MQTT_USERNAME
        if username:
            self.client.username_pw_set(username, password or MQTT_PASSWORD)
        self.client.max_queued_messages_set(100000)
        if MQTT_TLS:
            configure_tls(self.client)
        if status_topic:
            self.client.will_set(status_topic, json.dumps({"status": "offline"}), qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def subscribe(self, pattern, handler, qos=1):
        self.handlers.append((pattern, handler, qos))
        self.client.message_callback_add(pattern, self._wrap(handler))
        if self.connected.is_set():
            self.client.subscribe(pattern, qos)

    @staticmethod
    def _wrap(handler):
        def callback(client, userdata, message):
            try:
                payload = json.loads(message.payload) if message.payload else None
            except (ValueError, UnicodeDecodeError):
                payload = message.payload.decode(errors="replace")
            try:
                handler(message.topic, payload)
            except Exception as exc:  # noqa: BLE001
                print(f"handler error on {message.topic}: {exc}", flush=True)
        return callback

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print(f"MQTT connect failed: {reason_code}", flush=True)
            return
        for pattern, _, qos in self.handlers:
            client.subscribe(pattern, qos)
        if self.status_topic:
            status = self.status_payload() if self.status_payload else {
                "status": "online", "ts": time.time()}
            client.publish(self.status_topic, json.dumps(status), qos=1, retain=True)
        self.connected.set()
        print(f"MQTT connected to {self.host}:{self.port} as {self.name}", flush=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()
        print(f"MQTT disconnected ({reason_code}); reconnecting", flush=True)

    def _on_message(self, client, userdata, message):
        pass  # everything goes through message_callback_add

    def start(self, wait=True):
        while True:
            try:
                self.client.connect(self.host, self.port, keepalive=15)
                break
            except OSError as exc:
                print(f"waiting for MQTT broker at {self.host}:{self.port}: {exc}", flush=True)
                time.sleep(2)
        self.client.loop_start()
        if wait:
            self.connected.wait()
        return self

    def publish(self, topic, payload, qos=0, retain=False):
        data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        return self.client.publish(topic, data, qos=qos, retain=retain)

    def clear_retained(self, topic):
        return self.client.publish(topic, b"", qos=1, retain=True)

    def stop(self):
        if self.status_topic:
            try:
                self.client.publish(self.status_topic, json.dumps(
                    {"status": "offline", "state": "stopped", "ts": time.time()}),
                    qos=1, retain=True).wait_for_publish(2)
            except (RuntimeError, ValueError):
                pass
        self.client.loop_stop()
        self.client.disconnect()


def event(type_, message, level=None, room=None, severity="info", **data):
    """Build an event for EVENTS_TOPIC."""
    body = {"ts": time.time(), "type": type_, "severity": severity, "message": message}
    if level:
        body["level"] = level
    if room:
        body["room"] = room
    body.update(data)
    return body
