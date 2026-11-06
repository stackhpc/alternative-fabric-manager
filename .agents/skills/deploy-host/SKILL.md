---
name: deploy-host
description: Rebuild and redeploy AFM on the infiniband-testing host, which runs it in HA mode, safely - back up the database that holds the fabric's keys, rebuild the image, recreate the container with its HA mounts, and verify the lease, OpenSM under the watchdog, and the fabric check. Use when asked to deploy, redeploy or try a branch on the host.
---

# Redeploy AFM on infiniband-testing

AFM there manages the OpenSM of a **shared** fabric, in HA mode (approved
2026-11-06: M_Key and SM_Key protection), as a single instance:

- container `alternative-fabric-manager`, image `alternative-fabric-manager:ha`;
- its database in the `afm-mariadb` container (MariaDB 10.11, port 3398,
  volume `afm_mariadb`); passwords in /etc/alternative-fabric-manager/
  mariadb-root.pw and mariadb-afm.pw (root-only, never print them);
- config /etc/alternative-fabric-manager/afm.conf (mode 600), with
  `[database] connection`, `[ha] ha = true`, and
  `hardening = m_key, sm_key, enforcement`;
- the watchdog's tmpfs /run/alternative-fabric-manager.

The database holds the fabric's keys. Never drop or recreate it, and never
print the keys.

## Rollback, kept ready

- `alternative-fabric-manager-single`: the stopped single-node container,
  with the SQLite database in the `afm_data` volume, unchanged since the
  switch to HA;
- `$CLAUDE_JOB_DIR/tmp/afm_data.backup/afm.conf.pre-ha`: its config.

To roll back: stop and rename the HA container, rename the single one back
to `alternative-fabric-manager`, restore afm.conf.pre-ha, and start it.

## Steps

1. **Back up the database**:
   ```sh
   APW=$(sudo cat /etc/alternative-fabric-manager/mariadb-afm.pw)
   sudo docker exec afm-mariadb mariadb-dump -uafm -p"$APW" -h127.0.0.1 -P3398 afm \
     | sudo tee "$CLAUDE_JOB_DIR/tmp/afm_data.backup/afm.sql.$(date +%s)" >/dev/null
   ```
2. **Build**; the host's Docker has no bridge network:
   ```sh
   cd /opt/stack/alternative-fabric-manager
   sudo docker build -q --network host -t alternative-fabric-manager:ha .
   ```
3. **Upgrade the schema**, if the branch adds an Alembic revision:
   ```sh
   sudo docker run --rm --network host \
     -v /etc/alternative-fabric-manager:/etc/alternative-fabric-manager:ro \
     alternative-fabric-manager:ha --config-file /etc/alternative-fabric-manager/afm.conf --db-upgrade
   ```
4. **Recreate the container.** Stopping it steps the leader down and
   releases the lease; OpenSM's container dies with it. `--init` reaps the
   OpenSM processes the shared PID namespace reparents to the manager:
   ```sh
   sudo docker stop -t 30 alternative-fabric-manager && sudo docker rm alternative-fabric-manager
   sudo docker run -d --init --name alternative-fabric-manager --restart unless-stopped \
     --stop-timeout 30 --network host \
     -v /etc/alternative-fabric-manager:/etc/alternative-fabric-manager:ro \
     -v /etc/opensm:/etc/opensm -v /var/run/docker.sock:/var/run/docker.sock \
     -v afm_data:/var/lib/alternative-fabric-manager \
     -v /run/alternative-fabric-manager:/run/alternative-fabric-manager \
     -v /proc:/host/proc:ro \
     alternative-fabric-manager:ha --config-file /etc/alternative-fabric-manager/afm.conf
   ```
5. **Verify**, within a minute:
   - the log shows "Took the lease", "SUBNET UP" and "Leading, term N":
     `sudo docker logs alternative-fabric-manager 2>&1 | grep -E "lease|SUBNET UP|Leading|ERROR" | grep -vi "key 0x"`;
   - the watchdog is OpenSM's parent: `sudo docker top opensm -o pid,args`;
   - the lease's counter moves, and the applied row's check passes in the
     current term:
     ```sh
     q() { sudo docker exec afm-mariadb mariadb -uafm -p"$APW" -h127.0.0.1 -P3398 afm -N -e "$1"; }
     q "select holder_node, term, counter, released from lease"
     q "select term, check_ok, check_detail, contested, pkey_errors from applied"
     ```
   If no leader appears, the node row's `reach` tells whether the node sees
   its own port up; a `stop` file in /run/alternative-fabric-manager with
   an `[opensm] <defunct>` process means a stop that wasn't verified. Roll
   back while you diagnose: the fabric has no SM meanwhile.
6. Then run the SDN tempest suite (the `tempest-sdn` skill).

## Never

- print the keys (`--show-keys` output, opensm.conf), or the database
  passwords;
- run SMP tools without `-y $KEY`, or any SMP Set, on this fabric;
- connect to eswitchd on tcp://127.0.0.1:60001.
