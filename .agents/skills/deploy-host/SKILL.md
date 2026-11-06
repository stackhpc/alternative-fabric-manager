---
name: deploy-host
description: Rebuild and redeploy AFM on the infiniband-testing host, safely - back up the database that holds the fabric's M_Key, rebuild the image, recreate the container, and verify the first sweep and fabric check. Use when asked to deploy, redeploy or try a branch on the host.
---

# Redeploy AFM on infiniband-testing

AFM there manages the OpenSM of a **shared** fabric. Its database, in the
`afm_data` volume, holds the fabric's M_Key: losing it locks the managers
out of the switch ports. Never drop, recreate or "reset" that database.

## Steps

1. **Back up the database**, named after the commit being replaced:
   ```sh
   sudo cp -a /var/lib/docker/volumes/afm_data/_data/afm.sqlite \
     "$CLAUDE_JOB_DIR/tmp/afm_data.backup/afm.sqlite.$(git -C /opt/stack/alternative-fabric-manager rev-parse --short HEAD)"
   ```
2. **Check the host config** has `hardening = m_key, enforcement` under
   `[opensm]` in /etc/alternative-fabric-manager/afm.conf (mode 600). Without
   it, the default `all` hardening applies, which wasn't approved here.
3. **Build**; the host's Docker has no bridge network:
   ```sh
   cd /opt/stack/alternative-fabric-manager
   sudo docker build -q --network host -t alternative-fabric-manager:dev .
   ```
4. **Recreate the container**; OpenSM's container dies with it and is
   started again by the new manager:
   ```sh
   sudo docker rm -f alternative-fabric-manager
   sudo docker run -d --name alternative-fabric-manager --restart unless-stopped \
     --network host \
     -v /etc/alternative-fabric-manager:/etc/alternative-fabric-manager:ro \
     -v /etc/opensm:/etc/opensm -v /var/run/docker.sock:/var/run/docker.sock \
     -v afm_data:/var/lib/alternative-fabric-manager \
     alternative-fabric-manager:dev \
     --config-file /etc/alternative-fabric-manager/afm.conf
   ```
5. **Verify**: the log shows "SUBNET UP" and an applied generation, and the
   applied row shows a passing check and no errors:
   ```sh
   sudo docker logs alternative-fabric-manager 2>&1 | grep -E "INFO alt|WARNING|ERROR" \
     | grep -vi "key 0x" | tail
   sudo sqlite3 /var/lib/docker/volumes/afm_data/_data/afm.sqlite \
     "select generation, check_ok, check_detail, pkey_errors, enforcement_errors from applied;"
   ```
   `check_detail` should be "The fabric is complete". "No expected switches"
   means the database is new or was replaced: run `--confirm-switches` with a
   one-off `docker run` of the image, same volumes, and the
   `--confirm-switches` flag; it prints the switch GUIDs it recorded.
6. Then run the SDN tempest suite (the `tempest-sdn` skill).

## Never

- print the keys (`--show-keys` output, opensm.conf);
- run SMP tools without `-y $KEY`, or any SMP Set, on this fabric;
- connect to eswitchd on tcp://127.0.0.1:60001.
