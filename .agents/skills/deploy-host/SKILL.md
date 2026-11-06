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
  first (`[opensm] hardening`; see `docs/opensm-hardening.md`): M_Key
  protection locks out SMP tools without the key, and SM_Key protection,
  which HA mode needs, keeps other subnet managers from taking over.
- **The mode**: single-node (`[database] database`, an SQLite file in the
  `afm_data` volume) or HA (`[ha] ha = true`, with `[database] connection`
  to MariaDB or Galera).
- **Where the configuration is**, usually
  `/etc/alternative-fabric-manager/afm.conf`, mode 600.

AFM's database holds the fabric's OpenSM keys. Never drop or recreate it,
and never print the keys or the database's passwords.

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
   HA: `mariadb-dump` of AFM's database, with its own credentials.
2. **Build**, with `--network host` if the host's Docker has no bridge:
   ```sh
   sudo docker build -q --network host -t alternative-fabric-manager:<tag> .
   ```
3. **HA only: upgrade the schema**, if the branch adds an Alembic revision:
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
   sudo docker rm alternative-fabric-manager
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
5. **On a new database, record the fabric's switches**, or no job ever
   completes ("No expected switches"):
   ```sh
   sudo docker exec alternative-fabric-manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --confirm-switches
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
  from `--show-keys` into a variable, never to the screen;
- send SMP Sets, or disable switch ports, on a fabric others share;
- connect to eswitchd's socket (tcp://127.0.0.1:60001) by hand: it exits on
  messages it can't decode.
