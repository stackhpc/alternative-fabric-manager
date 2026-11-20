# Alternative fabric manager

The alternative fabric manager (AFM) gives OpenStack tenants isolated
InfiniBand networks. It stands in for NVIDIA UFM as the SDN controller of
[networking-mlnx](https://opendev.org/x/networking-mlnx): it serves the
part of UFM's REST API that networking-mlnx's `mlnx_sdn_assist` mechanism
driver uses, and drives an [OpenSM](https://github.com/linux-rdma/opensm)
subnet manager to match.

In short:

- **Each Neutron VLAN network gets its own InfiniBand partition.** The
  partition key (pkey) is the network's VLAN ID.
- **Each port joins its network's partition.** The port's GUID is read
  from its DHCP client ID.
- **A port is reported up only once OpenSM has applied it.** Until then,
  networking-mlnx holds it `DOWN`.
- **OpenSM is hardened by default** for a fabric shared by tenants who
  don't trust each other.

It runs on a single node with SQLite, or highly available on every OpenSM
node with Galera (see [High availability](#high-availability)).

## How it works

networking-mlnx sends each network and port to AFM as it changes:

| Request | Effect |
| --- | --- |
| `POST <url>/<domain>/Network`, `PUT` / `DELETE …/Network/<id>` | Creates or removes the network's partition, whose pkey is the network's VLAN ID (`provider:segmentation_id`) |
| `POST <url>/<domain>/Port`, `PUT` / `DELETE …/Port/<id>` | Adds or removes the port's GUID, the last 8 octets of its DHCP client ID, as a full member of its network's partition |

AFM records each change in its database and answers with a **job**, which
networking-mlnx polls with `GET <url>/app/jobs/<id>`. A single thread then
applies the recorded state:

1. It writes the whole partitions file, atomically.
2. It sends OpenSM `SIGHUP`. When only the partitions file has changed,
   OpenSM runs a pkey sweep, which updates the ports' pkey tables without
   restarting.
3. It waits for OpenSM to log the end of the sweep. Only then does it
   complete the jobs that the file covers.

**The database is the source of truth.** Every `[opensm] check_interval`
seconds, AFM checks that OpenSM is running and that the partitions file is
the one the recorded state renders to. If the file was edited by hand, or
an apply failed, AFM writes it again and has OpenSM apply it. A failed
job's change stays in the recorded state, so the next apply includes it.

**A 400 is final.** networking-mlnx sets the port to `ERROR` and doesn't
retry, so AFM answers 400 only to requests that can never succeed:

- a port without a usable client ID;
- a port on a network that has no partition;
- a network whose VLAN ID another network's partition already uses.

A port's GUID can be in only one network's partition.

**Flat networks** share one partition, `[opensm] flat_pkey` (0x7ffe by
default, outside the VLAN range). networking-mlnx only tells AFM about a
new flat network once it includes
[Send flat network creation to the SDN controller](https://review.opendev.org/c/x/networking-mlnx/+/1014376).
Until then, ports on flat networks are rejected.

To create InfiniBand networks and boot virtualised and bare metal
instances on them, see
[docs/infiniband-networks.md](docs/infiniband-networks.md).

## Running it

These steps run AFM on a single node. AFM starts OpenSM itself, in a
container of its own (`[opensm] driver = managed`, the default).

1. **Stop any other subnet manager.** Stop the host's own, such as a
   distribution's `opensm` service, and make sure none runs elsewhere on
   the fabric. AFM's OpenSM runs in a container named by
   `[opensm] container` (`opensm` by default), and replaces any existing
   container of that name.

2. **Write the configuration.** Copy
   [etc/afm.conf.sample](etc/afm.conf.sample) to
   `/etc/alternative-fabric-manager/afm.conf`, and set a token. OpenSM's
   own options go in `[opensm_conf]`, for example `sm_priority = 15`.

3. **Build and start the container:**

   ```sh
   docker build -t alternative-fabric-manager .
   docker run -d --init --name alternative-fabric-manager --restart unless-stopped \
     --network host \
     -v /etc/alternative-fabric-manager:/etc/alternative-fabric-manager:ro \
     -v /etc/opensm:/etc/opensm \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v afm_data:/var/lib/alternative-fabric-manager \
     alternative-fabric-manager
   ```

   Keep the container's name as `[opensm] manager_container` says
   (`alternative-fabric-manager` by default). OpenSM's container joins
   this container's PID namespace, so OpenSM stops whenever AFM does, and
   never runs with partitions that nothing keeps up to date. `--init`
   reaps OpenSM's processes when it stops, since they belong to that
   namespace.

4. **Point networking-mlnx at it,** in `ml2_conf.ini`:

   ```ini
   [ml2]
   mechanism_drivers = ...,mlnx_infiniband,mlnx_sdn_assist

   [sdn]
   sync_enabled = True
   url = http://127.0.0.1:8090/ufmRestV3
   domain = cloudx
   token = <token>
   physical_networks = <InfiniBand physical networks>
   ```

5. **Wait about a minute.** On a new database, AFM records the fabric's
   switches by itself, once its checks have seen the same switches
   `[opensm] confirm_checks` times in a row. No job completes until then,
   and the check says "No expected switches yet". See
   [Expected switches](#expected-switches).

Treat AFM as root on its host: it uses the Docker socket to start OpenSM's
container and read its log, and whatever can use that socket controls the
host.

### Other ways to run OpenSM

With `[opensm] driver = docker`, something else runs OpenSM, and AFM only
sends it `SIGHUP`. That OpenSM must read its partitions from `config_dir`,
bind mounted into its container. Mount the directory, not the file, so
that AFM's atomic rename reaches it.

### Expected switches

AFM only completes jobs while OpenSM sees every switch it expects, so that
an OpenSM cut off on part of the fabric can't report it as the whole.
AFM learns the list by itself, and adds new switches the same way.

`--confirm-fabric` records the switches OpenSM sees now. You need it only
to drop a switch that was removed or replaced:

```sh
docker exec alternative-fabric-manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf --confirm-fabric
```

> [!WARNING]
> With the experimental `m_key_per_port` group named in
> `[opensm] hardening`, `--confirm-fabric` also arms the turn-on of
> per-port M_Keys, which re-keys every port. See
> [docs/opensm-hardening.md](docs/opensm-hardening.md), Per-port M_Keys.

### High availability

With `[ha] ha = true`, an AFM instance runs on every OpenSM node, behind
HAProxy, and the instances share the deployment's Galera database. One
leader at a time runs OpenSM; the others serve the API and stand by to
take over. HA mode needs a server database and more mounts:

- [specs/ha.md](specs/ha.md) is the design, with its known limitations;
- [docs/kayobe.md](docs/kayobe.md) deploys it with Kayobe.

In HA mode, `--status` shows who holds the lease, its term, and any
freeze, including an unfreeze still pending and which manager to check.

## Hardening

AFM configures OpenSM for a fabric shared by tenants who don't trust each
other. [docs/opensm-hardening.md](docs/opensm-hardening.md) explains each
option, why it's there, and what it costs.

`[opensm] hardening` chooses groups of options in one line:

| Setting | Meaning |
| --- | --- |
| `all` (the default) | Every group that isn't experimental |
| `-sa_trust` | Everything but the named group |
| `m_key, enforcement` | Only the named groups |
| `all, m_key_per_port` | Everything, plus an experimental group |

The default groups include:
- M_Key protection;
- secret SM and SA keys, kept in AFM's database;
- the SA's enhanced trust model;
- a default partition in which every port is only a limited member.

**Diagnostic tools need the M_Key.** With M_Key protection on, SMP tools
such as `ibnetdiscover` and `smpquery` need `-y $KEY`. Read the key into
a variable with `alternative-fabric-manager --show-keys`, and never print
it.

**Keys expire if they're lost.** The keys AFM sets on ports have an hour's
lease by default, so ports keyed with a lost key recover by themselves.
`[opensm] key_lease_period = 0` makes them permanent instead (see
docs/opensm-hardening.md, Permanent keys).

### Experimental groups

These groups are untested on hardware, and each is named in addition to
`all`, for example `hardening = all, allowed_sm`:

| Group | What it does | Where |
| --- | --- | --- |
| `m_key_per_port` | Gives each port an M_Key of its own, so that a key read from one port no longer opens every switch. A tool's `-y` then needs that port's own key, from `--port-m-key <guid>` | Single-node mode, managed driver. It leaves keys on ports; turning it on or off follows a procedure (Per-port M_Keys) |
| `allowed_sm` | Makes OpenSM ignore every subnet manager outside this host's physical ports, so a tenant's can't win the election. An ignored one on a port whose host may hold the fabric's keys (`--record-sm-port`) fails the fabric check | Single-node mode. See Allowed subnet managers, and its Before enabling checklist |
| `class_keys` | Keys every port's vendor-specific, congestion control and node-to-node classes from a stored seed | Single-node mode. It leaves keys on ports; turn it off by disabling first (Class keys, and its Before enabling checklist) |
| `sa_rate` | Limits each port's SA requests | See SA request rate |

The section names in the last column are in
[docs/opensm-hardening.md](docs/opensm-hardening.md).

## The partitions file

### Your own template

AFM renders `partitions.conf` from a Jinja template, in a sandbox. Set
`[opensm] partitions_template` to use your own, for example to add static
partitions or multicast groups.

AFM checks whatever the template renders before writing it:

- the default partition must be defined;
- each network's partition, and the flat networks' partition, must have
  exactly its ports as full members;
- a port may be in only one of those, and in no other partition;
- `ALL`, `ALL_CAS` and `ALL_VCAS` can't be full members of any partition
  but a network's.

A file that fails the check isn't written, and its jobs fail. A network
whose VLAN ID is a static partition's pkey is rejected when it is sent.

### The default partition

`[opensm] default_partition` is written to the file as it is. By default
every port is only a limited member, so tenants can't reach each other
there. If ports on your fabric rely on full membership (`ALL=full`), the
check refuses it unless `[opensm] shared_default_partition` is true; see
[docs/opensm-hardening.md](docs/opensm-hardening.md) for what that
exposes.

On a running fabric, give the default partition's IPoIB broadcast group
the rate and MTU it has now, as `saquery -g` shows them. Otherwise
OpenSM recreates the group, which disrupts IPoIB on the default partition
across the fabric.

## When a switch port stops enforcing partitions

A switch port enforces partitions with a table of the pkeys it lets
through. If the host behind it has ports in more networks than that table
holds, OpenSM logs `ERR 0507` or `ERR 0513` and **clears the port's
enforcement bit**. The switch then lets any pkey through that port, so the
host behind it can reach other tenants' partitions.

AFM can't fix that by itself, so it fails closed. It records the switch
port, logs "OpenSM cleared a switch port's enforcement", and **no job
completes, for any port on the fabric**, until an operator has dealt with
it. New ports stay `DOWN` meanwhile, and Nova's wait for them fails.

To deal with it (in a container, prefix each command with
`docker exec alternative-fabric-manager`):

1. **List the ports**, as `<switch GUID>:<port>`:

   ```sh
   alternative-fabric-manager --config-file /etc/alternative-fabric-manager/afm.conf \
     --show-enforcement-errors
   ```

   `unattributed` means OpenSM's message named no switch port. Read
   OpenSM's log around its "Clearing Enforcement bit" message to find it.
2. **Find the host behind each port,** with `ibnetdiscover -y $KEY`, or
   with `smpquery -y $KEY -D nodeinfo <directed route>` if LIDs aren't
   assigned. `KEY` is the M_Key (see [Hardening](#hardening)).
3. **Bring the port's pkeys within its table.** Move that host's ports onto
   fewer networks, or take the host out of service. Until then, treat that
   host as able to reach any partition.
4. **Check that the port enforces again** after OpenSM's next sweep:
   `smpquery -y $KEY portinfo -G 0x<switch GUID> <port>` shows
   `PartEnforceInb` and `PartEnforceOutb` set.
5. **Clear the errors,** so that jobs complete again:

   ```sh
   alternative-fabric-manager --config-file /etc/alternative-fabric-manager/afm.conf \
     --clear-enforcement-errors
   ```

   Clear only ports you checked in step 4. Clearing doesn't wait for
   proof: OpenSM may not log the error again for a port it still can't
   enforce, so a port cleared too early can go unnoticed.

## Development

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

CI also runs the tests inside the image, against the installed package,
on SQLite and on MariaDB, and smoke-tests the runtime image. To do the
same locally:

```sh
docker build --build-arg UV_VERSION="$(<.uv-version)" --target test -t afm-test .
docker run --rm afm-test -q
docker build --build-arg UV_VERSION="$(<.uv-version)" -t afm .
.github/scripts/smoke-test.sh afm
```

The uv version is recorded in `.uv-version`; pass it to local Docker
builds as above.

## Container images

GitHub Actions publishes multi-architecture images to
`ghcr.io/stackhpc/alternative-fabric-manager`:

- **Releases** are tagged `<major>.<minor>.<patch>`, for example `0.1.0`,
  from immutable Git tags. The **Release** workflow bumps the version,
  tags it, drafts the release and builds its image; see
  [docs/releasing.md](docs/releasing.md).
- **Every build** also gets an immutable `sha-<commit>` tag, and a
  `build-<run-id>-<attempt>` tag that tells rebuilds of the same commit
  apart.

An image is published only once its amd64 build passes the image tests.
Pin deployments to a release image's digest, never to a mutable
convenience tag.

The weekly dependency workflow opens a fresh pull request when updates are
available. It updates the uv image's version and digest, the Python base
image's digest, and every dependency in `uv.lock`, then runs the full test
suite and the image tests on the proposed commit.
