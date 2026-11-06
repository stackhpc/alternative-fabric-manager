---
name: tempest-sdn
description: Run the networking-mlnx tempest plugin (API, InfiniBand port and pkey-isolation scenario tests) against DevStack with SDN sync to AFM, and read the results from Neutron's and AFM's side. Use to validate an AFM deployment on a DevStack host end to end.
---

# SDN tempest against AFM

DevStack's ML2 sends every network and port to AFM (`[sdn] url
http://127.0.0.1:8090/ufmRestV3`). The plugin's tests create VLAN networks,
InfiniBand ports and guests with virtual functions, and check that guests on
one network communicate and guests on different networks can't.

## Run

```sh
T="${SCRATCH:-/tmp/afm}/tempest"   # holds tempest-sdn.conf: tempest.conf with the plugin's [networking_mlnx] options
cd /opt/stack/tempest && source <devstack>/openrc admin admin >/dev/null
timeout 7200 .tox/tempest/bin/tempest run --serial --config-file "$T/tempest-sdn.conf" \
  --regex networking_mlnx_tempest_plugin > "$T/run.log" 2>&1
grep -E "^(Totals| - Passed| - Skipped| - Failed)|\.\.\. FAILED" "$T/run.log"
```

It takes 10-20 minutes; run it in the background. After changing the
plugin, reinstall it into tempest's venv first:
`/opt/stack/tempest/.tox/tempest/bin/pip install -q --no-deps --force-reinstall <plugin checkout>`.

Expected on a single compute host with SDN sync: 16 passed, 4 skipped. The
skips are deliberate: no unsynced physical network configured,
`mlnx_infiniband` binds VLAN ports, SDN restricts client IDs, and live
migration needs a second host.

## Read a failure

1. From AFM's side: did every job complete, and quickly?
   ```sh
   sudo sqlite3 /var/lib/docker/volumes/afm_data/_data/afm.sqlite \
     "select status, operation, count(*) from jobs where created_at > '<start>' group by 1,2;"
   sudo docker logs --since <start> alternative-fabric-manager 2>&1 | grep -E "Rejected|ERROR|waits"
   ```
2. A port stuck in BUILD whose job never appears: AFM answered its request
   with 400 (`Rejected request:` in AFM's log), which networking-mlnx treats
   as final. Find why: usually an ordering race between different objects.
3. A guest whose VF never reaches ACTIVE, while AFM's jobs completed, is
   usually guest-side; rerun before suspecting AFM.

Neutron's view: `sudo journalctl -u devstack@neutron-api --since <start> | grep <port id>`.
