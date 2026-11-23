---
name: deploy-host
description: Deploy or redeploy AFM on a DevStack host set up as networking-mlnx's DevStack guide describes, with networking-mlnx's SDN driver pointing at it, in single-node or HA mode - back up the database that holds the fabric's keys, build the image, recreate the container, and verify OpenSM, the fabric check and, in HA mode, the lease. Use when asked to deploy, redeploy or try a branch of AFM on a DevStack host.
---

# Deploy AFM on a DevStack host

The host runs DevStack as networking-mlnx's DevStack guide describes
(`devstack/README.rst` in networking-mlnx: OVN with the `mlnx_infiniband`
mechanism driver and the `neutron_ml2_mlnx` plugin), on a host with a port
on the InfiniBand fabric. AFM runs beside it in a container, and replaces
UFM as networking-mlnx's SDN controller: the guide's "SDN mechanism
driver" section, with `[sdn] url` pointing at AFM.

Find out before changing anything:

- **Who else uses the fabric.** If others share it, agree the hardening
  first (`[opensm] hardening` and `key_lease_period`, and any hardening
  options in `[opensm_conf]`; see `docs/opensm-hardening.md`): M_Key
  protection locks out SMP tools without the key, and SM_Key protection,
  which HA mode needs, keeps other subnet managers from taking over.
  Deploying a newer release counts as a hardening change when its release
  notes add a group to `all` and the deployment uses `all` or removals
  only: agree it, or opt out of the new group with `-group` in the same
  change (the older release refuses `-group` for a group it doesn't know,
  so a rollback must remove it again). Likewise the first release with an
  hour's `key_lease_period` by default, on a deployment that doesn't set
  it: agree the lease, or set `key_lease_period = 0` to keep permanent
  keys.
- **The mode**: single-node (`[database] database`, an SQLite file in the
  `afm_data` volume) or HA (`[ha] ha = true`, with `[database] connection`
  to MariaDB or Galera).
- **Where the configuration is**, usually
  `/etc/alternative-fabric-manager/afm.conf`, mode 600.

AFM's database holds the fabric's OpenSM keys. Never drop or recreate it,
and never print the keys or the database's passwords.

## Per-port M_Keys

If `[opensm] hardening` names `m_key_per_port`, or the database records
ports (`--port-key-states` lists any), the deploy differs (see
`docs/opensm-hardening.md`, Per-port M_Keys):

- **Backups:** the database holds `m_key_base`, which every port's key
  derives from. Keep a backup taken after the start that stored the base
  (the first start of a release with the group) and after any turn-on;
  an older one can lose the ports.
- **Naming the group turns them on.** On a deployment that names
  `m_key_per_port` with per-port M_Keys off, the manager arms the turn-on
  by itself once every precondition holds (the base stored by an earlier
  start, links recorded, and every recorded port behind an up link
  answering the stored `m_key`), and then restarts OpenSM, which re-keys
  every port, at a passing check with no open job. So on such a
  deployment, every redeploy may be the one that turns them on: deploy
  it only with the fabric owner's agreement. `--port-key-states` shows
  the target and what blocks the arming.
- **Never begin or end them in a routine redeploy:** don't name the group,
  don't remove it, and don't run `--retry-port-key-turn-on`, which lets a
  failed turn-on be armed again, as part of a deploy. Each re-keys every
  port and needs the fabric owner's agreement. `--confirm-fabric` only
  records the expected switches, so it's safe on any deployment.
- **Check the states before and after**:
  ```sh
  sudo docker exec alternative-fabric-manager alternative-fabric-manager \
    --config-file /etc/alternative-fabric-manager/afm.conf --port-key-states
  ```
  The first line (`target ..., OpenSM last started ...`) and each port's
  key (`m_key`, `derived` or `unknown`) should be the same after the
  deploy as before, once the start has settled.
- **A release without the group** (an older one, or a branch from before
  it) can't render OpenSM's cache: turn per-port M_Keys off first, and
  wait until `--port-key-states` shows every port, HCAs included, holding
  `m_key`. Restoring an older database is a downgrade too.
- **The expected switches keep themselves:** with ports recorded, the
  prober follows every port that answers as a switch, and the fabric
  check counts the expected ones it finds. The manager translates the
  list between `ibswitches`' node GUIDs and the prober's port 0 GUIDs by
  itself. "Missing switches" for a switch that was removed or replaced
  needs `--confirm-fabric`, once the hardware change is done.
- **In HA mode the group is experimental too**: name it only after every
  node runs a release with configuration epochs, and see
  docs/opensm-hardening.md, In HA mode.

## Wire networking-mlnx to AFM

In DevStack's `local.conf`, as the networking-mlnx guide's SDN section
shows, with `mlnx_infiniband` as well, since its ports are InfiniBand:

```ini
[[local|localrc]]
enable_plugin neutron_ml2_mlnx https://opendev.org/x/networking-mlnx
Q_ML2_PLUGIN_MECHANISM_DRIVERS=ovn,mlnx_infiniband,mlnx_sdn_assist

[[post-config|/$Q_PLUGIN_CONF_FILE]]
[sdn]
sync_enabled = True
url = http://127.0.0.1:8090/ufmRestV3
domain = cloudx
token = <AFM's [api] token>
physical_networks = <the InfiniBand physical networks>
bind_normal_ports = False
```

On a running DevStack, set the same in `/etc/neutron/plugins/ml2/ml2_conf.ini`
and restart `devstack@neutron-api` (and its RPC and periodic workers).
Changing a shared DevStack's configuration needs its owner's agreement.

## Deploy or redeploy

1. **Back up the database.** Single-node:
   `sudo cp -a /var/lib/docker/volumes/afm_data/_data/afm.sqlite <backup>`.
   HA: `mariadb-dump` of AFM's database, with its own credentials, read
   from `[database] connection` into a variable, never printed. Either way
   the backup holds the fabric's keys: keep it in a root-only directory,
   mode 600, such as `/var/backups/afm`.
2. **Build**, with `--network host` if the host's Docker has no bridge:
   ```sh
   sudo docker build -q --network host \
     --build-arg UV_VERSION="$(<.uv-version)" -t alternative-fabric-manager:<tag> .
   ```
3. **HA only: create or upgrade the schema**, on a new database, or if
   the branch adds an Alembic revision:
   ```sh
   sudo docker run --rm --network host \
     -v /etc/alternative-fabric-manager:/etc/alternative-fabric-manager:ro \
     alternative-fabric-manager:<tag> \
     --config-file /etc/alternative-fabric-manager/afm.conf --db-upgrade
   ```
   An SQLite database upgrades itself.
4. **Recreate the container.** Stopping it stops OpenSM too, so the fabric
   has no subnet manager until the new one starts; in HA mode the instance
   steps down and releases the lease first. Name the container as
   `[opensm] manager_container` says:
   ```sh
   sudo docker stop -t 30 alternative-fabric-manager
   # Renamed rather than removed, to roll back by starting it again
   sudo docker rename alternative-fabric-manager alternative-fabric-manager-<old tag>
   sudo docker run -d --init --name alternative-fabric-manager \
     --restart unless-stopped --stop-timeout 30 --network host \
     -v /etc/alternative-fabric-manager:/etc/alternative-fabric-manager:ro \
     -v /etc/opensm:/etc/opensm -v /var/run/docker.sock:/var/run/docker.sock \
     -v afm_data:/var/lib/alternative-fabric-manager \
     alternative-fabric-manager:<tag> \
     --config-file /etc/alternative-fabric-manager/afm.conf
   ```
   HA mode also needs the watchdog's tmpfs and the host's processes:
   `-v /run/alternative-fabric-manager:/run/alternative-fabric-manager`
   and `-v /proc:/host/proc:ro`.
5. **On a new database, wait for the expected switches**: the manager
   records them once its checks have seen the same switches for
   `[opensm] confirm_checks` checks in a row (a minute by default), and no
   job completes until then ("No expected switches yet: recording them
   once the fabric has been stable for N checks"). Confirming the fabric
   is optional: it records them at once, and does nothing else.
   ```sh
   sudo docker exec alternative-fabric-manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --confirm-fabric
   ```

## Verify

Within a minute:

- **OpenSM started**: the log shows "SUBNET UP", and, in HA mode, "Took the
  lease" and "Leading, term N":
  `sudo docker logs alternative-fabric-manager 2>&1 | grep -E "lease|SUBNET UP|Leading|ERROR" | grep -vi "key 0x"`.
- **The fabric check passes**: the `applied` row's `check_detail` is "The
  fabric is complete", with no `pkey_errors` or `enforcement_errors`, read
  with `sqlite3` on the SQLite file, or `mariadb` in HA mode.
- **HA mode**: the watchdog is OpenSM's parent
  (`sudo docker top opensm -o pid,args`), and the `lease` row's counter
  moves.

With per-port M_Keys recorded, the fabric check runs the prober rather
than `ibswitches`, and its `check_detail` names any lost port or switch
port without a recorded neighbour. Use it and `--port-key-states` to
verify, rather than `ibswitches -y`/`smpquery -y`, which reach only ports
holding the key given. If you need an SMP tool, take that port's key with
`KEY=$(... --port-m-key <guid>)`, never displayed. A start that fails
with the group named is retried at every check ("Failed to start OpenSM,
retrying at every check"), not ended: read that line.

If no HA leader appears, the `nodes` row's `reach` tells whether the node
sees its own port up. If OpenSM doesn't start, read the `opensm`
container's log. Roll back while you diagnose: the fabric has no subnet
manager meanwhile.

Then run networking-mlnx's tempest plugin against it (the `tempest-sdn`
skill).

## Never

- print the keys (`--show-keys` output, `opensm.conf`) or the database
  passwords;
- run SMP tools on a protected fabric without `-y $KEY`, where `KEY` comes
  from `--show-keys` (matching `$1 == "m_key"`, not `/^m_key/`, which also
  matches `m_key_base`), or with per-port M_Keys from `--port-m-key
  <guid>`, into a variable, never to the screen;
- write OpenSM's cache volume (`/var/cache/opensm`) by hand, paste M_Key
  lines from OpenSM's log, which holds keys, or `docker inspect` the
  helper prober container (`<[opensm] container>-prober`), whose
  configuration holds keys while it runs;
- send SMP Sets, or disable switch ports, on a fabric others share;
- connect to eswitchd's socket (tcp://127.0.0.1:60001) by hand: it exits on
  messages it can't decode.
