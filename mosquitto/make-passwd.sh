#!/bin/sh
# Regenerate mosquitto/passwd from the MQTT_PASSWORD_<USER> values in .env
# (hashed with the broker's own mosquitto_passwd, run in its image).
#   sh mosquitto/make-passwd.sh            # from the repository root
# The test broker keeps the development users in mosquitto/passwd.test.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env (copy env.example to .env first)" >&2; exit 1; }
: > mosquitto/passwd.new
for user in simulator sensors actuators doors agent bridge recorder evacuation archiver operator; do
  var="MQTT_PASSWORD_$(echo "$user" | tr a-z A-Z)"
  value=$(grep -E "^${var}=" .env | tail -n 1 | cut -d= -f2-)
  [ -n "$value" ] || { echo "missing $var in .env" >&2; exit 1; }
  printf '%s:%s\n' "$user" "$value" >> mosquitto/passwd.new
done
docker run --rm -v "$PWD/mosquitto:/m" eclipse-mosquitto:2.0.18 \
  sh -c 'mosquitto_passwd -U /m/passwd.new && chmod 0644 /m/passwd.new'
mv mosquitto/passwd.new mosquitto/passwd
echo "mosquitto/passwd regenerated; restart the broker: docker compose restart mosquitto"
