#!/bin/sh
# A local certificate authority and a broker certificate for the TLS
# listener (mosquitto/tls.conf, compose.tls.yaml). Valid for the names
# "mosquitto" (inside Docker) and "localhost".
#   sh mosquitto/make-certs.sh      # writes mosquitto/certs/{ca,server}.{crt,key}
set -eu
dir="$(dirname "$0")/certs"
mkdir -p "$dir"
cd "$dir"
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=fire-safety local CA" \
  -keyout ca.key -out ca.crt
openssl req -newkey rsa:2048 -nodes -subj "/CN=mosquitto" -keyout server.key -out server.csr
printf 'subjectAltName=DNS:mosquitto,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' > ext.cnf
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 \
  -extfile ext.cnf -out server.crt
rm -f server.csr ext.cnf ca.srl
chmod 0644 ca.crt server.crt server.key
echo "certificates in $dir (keep ca.key private)"
