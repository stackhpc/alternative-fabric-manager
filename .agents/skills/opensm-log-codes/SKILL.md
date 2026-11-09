---
name: opensm-log-codes
description: Check OpenSM 5.17's real log message formats and error codes from its binary, before matching them in drivers/docker.py or the spec's error table. Use whenever code or a reviewer claims what OpenSM logs, or a new ERR code needs handling.
---

# OpenSM's log formats, from its binary

AFM parses OpenSM's log for sweep markers, PARSE ERRORs and pkey-table
errors. Names and formats differ from upstream source and docs, for example
`P_KeyTable` rather than `PKeyTable`, and most pkey errors name a node
description, not a port GUID. Check the binary, never memory.

## Extract

The pinned image is the one the `opensm` container runs:

```sh
D="${SCRATCH:-/tmp/afm}/osmbin"; mkdir -p "$D"
sudo docker cp opensm:/usr/sbin/opensm "$D/"
sudo docker cp -L opensm:/usr/lib64/libopensm.so.28 "$D/libopensm.so.28.0.0"
sudo chown -R "$USER" "$D"
```

Copying files out of the image is read-only for the fabric; don't run the
binary.

## Look up

```sh
strings "$D"/* | grep -E "ERR (0503|0505|3113):"      # codes and formats
strings "$D"/libopensm.so.28.0.0 | grep -E "KeyTable"  # attribute names
```

- A code whose message names a port GUID can be attributed to a port;
  anything else (node description, node GUID + port number, nothing) must be
  unattributed and hold every job of the sweep.
- Add each new code to `PKEY_ERROR_CODES`, `ENFORCEMENT_ERROR_CODES` or
  `MAD_ERROR_CODES` in drivers/docker.py, to the spec's error table, and a
  test with the binary's exact format.
