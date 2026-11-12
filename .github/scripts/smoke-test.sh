#!/usr/bin/env bash
# Start the runtime image with its own entrypoint and default config path,
# and drive one request through the API, the database, the partitions file
# and the job loop, with the none driver standing in for OpenSM.
#
# Usage: smoke-test.sh <image>
# SMOKE_NETWORK=host runs it on the host's network instead of publishing
# the port, for hosts whose Docker bridge doesn't work.
set -euo pipefail

image="$1"
docker="${DOCKER:-docker}"
port="${SMOKE_PORT:-8090}"
name="afm-smoke-$$"
token="c21va2U6c21va2U="
base="http://127.0.0.1:$port"
work="$(mktemp -d)"

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "Smoke test failed; the container's log follows" >&2
    $docker logs "$name" >&2 || true
  fi
  $docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

# On the host's network, listen only on loopback; behind a published port,
# on every address of the container's own network.
listen=0.0.0.0
[[ "${SMOKE_NETWORK:-}" == host ]] && listen=127.0.0.1

# The partitions file goes in the state volume: the image has no /etc/opensm.
cat > "$work/afm.conf" <<CONF
[api]
host = $listen
port = $port
token = $token

[database]
database = /var/lib/alternative-fabric-manager/afm.sqlite

[opensm]
driver = none
config_dir = /var/lib/alternative-fabric-manager
CONF
chmod a+rx "$work"
chmod a+r "$work/afm.conf"

if [[ "${SMOKE_NETWORK:-}" == host ]]; then
  network=(--network host)
else
  network=(-p "127.0.0.1:$port:$port")
fi
$docker run -d --name "$name" "${network[@]}" \
  -v "$work/afm.conf:/etc/alternative-fabric-manager/afm.conf:ro" \
  "$image" >/dev/null

# Behind a published port, docker-proxy accepts the connection and then
# resets it until the API listens, so retry every error, not just refusals.
curl -fsS --retry 30 --retry-all-errors --retry-delay 1 "$base/healthz"
echo

code="$(curl -sS -o /dev/null -w '%{http_code}' "$base/ufmRestV3/app/jobs/1")"
if [[ "$code" != 401 ]]; then
  echo "Expected 401 without credentials, got $code" >&2
  exit 1
fi

# A network as networking-mlnx sends it, as in tests/conftest.py
job="$(curl -fsS -X POST -H "Authorization: Basic $token" \
  -H 'Content-Type: application/json' \
  -d '{"id": "c13bba05-eb07-45ba-ace2-765706b2d701", "name": "net1",
       "provider:network_type": "vlan",
       "provider:physical_network": "ibphysnet",
       "provider:segmentation_id": 100, "network_qos_policy": null}' \
  "$base/ufmRestV3/cloudx/Network")"
job="${job//\"/}"
echo "Created the network: $job"

status=""
for _ in $(seq 30); do
  status="$(curl -fsS -H "Authorization: Basic $token" "$base/ufmRestV3/$job")"
  [[ "$status" == *'"Status":"Completed"'* ]] && break
  sleep 1
done
echo "$status"
if [[ "$status" != *'"Status":"Completed"'* ]]; then
  echo "The job didn't complete" >&2
  exit 1
fi

partitions=/var/lib/alternative-fabric-manager/partitions.conf
if ! $docker exec "$name" grep -q '=0x0064' "$partitions"; then
  echo "partitions.conf lacks the network's partition" >&2
  $docker exec "$name" cat "$partitions" >&2
  exit 1
fi
echo "Smoke test passed"
