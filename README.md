# Alternative fabric manager

An InfiniBand fabric manager for
[networking-mlnx](https://opendev.org/x/networking-mlnx). It implements the
part of NVIDIA UFM's REST API that networking-mlnx's `mlnx_sdn_assist`
mechanism driver uses. It puts each Neutron VLAN network in its own
partition of an [OpenSM](https://github.com/linux-rdma/opensm) subnet
manager, and each port's GUID in its network's partition.

## How it works

networking-mlnx sends each network and port as it changes:

| Request | Effect |
| --- | --- |
| `POST <url>/<domain>/Network`, `PUT` / `DELETE …/Network/<id>` | Create or remove the network's partition. Its partition key is the network's VLAN ID (`provider:segmentation_id`) |
| `POST <url>/<domain>/Port`, `PUT` / `DELETE …/Port/<id>` | Add or remove the port's GUID, the last 8 octets of its DHCP client ID, as a full member of its network's partition |

Each change is recorded in an SQLite database and answered with a job,
which networking-mlnx polls with `GET <url>/app/jobs/<id>`. One thread
applies the recorded state:

1. it writes the whole partitions file, atomically;
2. it sends OpenSM `SIGHUP`. When only the partitions file has changed,
   OpenSM then runs a pkey sweep, which updates the ports' pkey tables,
   without a restart;
3. it waits for OpenSM to log the end of the sweep, and only then completes
   the jobs that the file covers.

networking-mlnx holds a port `DOWN` until its job has completed, so a port
is never reported as in its partition before OpenSM has put it there. A
request that can never be applied is answered with 400, which sets the port
to `ERROR`: a port without a usable client ID, a port on a network without a
partition, or a network whose VLAN ID another network's partition already
uses.

Flat networks share one partition, `[opensm] flat_pkey`, 0x7ffe by default,
outside the VLAN range. networking-mlnx needs "Send flat network creation to
the SDN controller" (https://review.opendev.org/c/x/networking-mlnx/+/1014376)
for the manager to learn of a flat network when it is created; until then its
ports are rejected. A port's GUID can be in only one network's partition.

## Running it

By default (`[opensm] driver = managed`) the manager runs OpenSM itself, in
a container of its own that joins the manager's PID namespace, so OpenSM
dies with the manager however it stops: it never runs with partitions that
no manager keeps up to date. The manager writes `opensm.conf` and
`partitions.conf` before it starts OpenSM, and starts OpenSM again if it
stops.

```sh
docker build -t alternative-fabric-manager .
docker run -d --name alternative-fabric-manager --restart unless-stopped \
  --network host \
  -v /etc/alternative-fabric-manager:/etc/alternative-fabric-manager:ro \
  -v /etc/opensm:/etc/opensm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v afm_data:/var/lib/alternative-fabric-manager \
  alternative-fabric-manager
```

Stop any other OpenSM on the host first: the manager removes any container
called as `[opensm] container` says, `opensm` by default, and starts its own.
The manager's container must be called as `[opensm] manager_container` says,
`alternative-fabric-manager` by default. The Docker socket lets the manager start OpenSM and read its log,
so treat the manager as root on its host.

Copy [etc/afm.conf.sample](etc/afm.conf.sample) to
`/etc/alternative-fabric-manager/afm.conf` and set a token. OpenSM's options
go in `[opensm_conf]`, for example `sm_priority = 15`. Then configure
networking-mlnx in `ml2_conf.ini`:

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

With `driver = docker`, something else runs OpenSM, which must read its
partitions from `config_dir`, bind mounted into its container: mount the
directory rather than the file, so that the atomic rename reaches it. The
manager then only sends it SIGHUP.

### Hardening

The manager configures OpenSM for a fabric shared by tenants who don't trust
each other: M_Key protection, secret SM and SA keys that it keeps in its
database, the SA enhanced trust model, and a default partition in which every
port is only a limited member. [docs/opensm-hardening.md](docs/opensm-hardening.md)
explains each option, why, and what it costs, such as diagnostic tools
needing the M_Key (`alternative-fabric-manager --show-keys`).
`[opensm] hardening` chooses the groups of options in one line, for example
`hardening = m_key, enforcement` for M_Key protection only.

### The partitions template

`partitions.conf` is rendered from a Jinja template, in a sandbox. Set
`[opensm] partitions_template` to use your own, for example to add static
partitions or multicast groups. Whatever it renders is checked before it is
written: the default partition must be defined, every network's partition,
and the flat networks', must have exactly its ports as full members, a port
may be in only one of them and in no other partition, and `ALL`, `ALL_CAS`
and `ALL_VCAS` can't be full members of any partition but a network's. A
file that fails the check fails the jobs and isn't written. A network whose
VLAN ID is a static partition's pkey is rejected when it is sent.

### The default partition

`[opensm] default_partition` is written to the file as is. By default every
port is only a limited member, so tenants can't reach each other there. On a
fabric where ports rely on full membership, `ALL=full`, the checks refuse it
unless `[opensm] shared_default_partition` is true; see
[docs/opensm-hardening.md](docs/opensm-hardening.md) for what that exposes.

On a running fabric, give the default partition's IPoIB broadcast group the
rate and MTU it has now, as `saquery -g` shows them. Otherwise OpenSM
recreates it, which disrupts IPoIB on the default partition across the
fabric.

## Development

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Limitations

- One OpenSM, on the same host. High availability is designed in
  [specs/ha.md](specs/ha.md).
- A job that fails leaves its change in the recorded state, so the next
  change that applies also applies it.

## Container image

GitHub Actions publishes multi-architecture images to
`ghcr.io/stackhpc/alternative-fabric-manager`. Release images are published
from immutable Git tags in the form `<major>.<minor>.<patch>`, for example
`0.1.0`. Update the project version before publishing a new release.

Every build also receives an immutable `sha-<commit>` tag and a
`build-<run-id>-<attempt>` tag. The latter distinguishes rebuilds of the
same commit. Do not deploy a mutable convenience tag; pin deployments to the
release image digest.

The uv version is recorded in `.uv-version`. Pass it to local Docker builds
with `docker build --build-arg UV_VERSION="$(<.uv-version)" .`.

The weekly dependency workflow opens a fresh pull request when updates are
available. It updates the uv image version and digest, Python base-image
digest, and all dependencies in `uv.lock`, then runs the full test suite.
