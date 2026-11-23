# InfiniBand networks and instances

This guide creates InfiniBand networks, and boots virtualised and bare
metal instances on them. It assumes a cloud whose networking-mlnx syncs to
the alternative fabric manager (AFM), configured as in
[kayobe.md](kayobe.md) or as in networking-mlnx's DevStack guide.

networking-mlnx's
[partition keys guide](https://opendev.org/x/networking-mlnx/src/branch/master/doc/source/partition-keys.rst)
says what it guarantees about a port and its partition.

The commands below use `ibphysnet` as the InfiniBand physical network, as
kayobe.md configures it. Use the name your deployment lists in
networking-mlnx's `[sdn] physical_networks`.

## Virtualised and bare metal

AFM puts a port in its network's partition by the port's GUID, its 64-bit
InfiniBand identifier. It reads the GUID from the port's DHCP client ID,
however the port is attached.

What differs is who binds the port, and where its client ID comes from.
Neither needs the user to know a GUID:

| | Virtualised instances | Bare metal nodes (Ironic) |
| --- | --- | --- |
| The port | `vnic_type` `direct`, on a virtual function: create it first, then `openstack server create --port` | `vnic_type` `baremetal`, on the node's adapter: `openstack server create --network` is enough |
| Bound by | `mlnx_infiniband` (VIF type `ib_hostdev`) | `mlnx_sdn_assist` |
| Its client ID | Derived by `mlnx_infiniband` from the port's MAC address when Nova binds it, as the virtual function's GUID is | Copied by Ironic from its port's `extra` `client-id`, which inspection records |

**Users never set a client ID on a Neutron port themselves,** in either
case. Only administrators and services can set one on a network that
`mlnx_sdn_assist` syncs, since the controller takes the GUID from it.

**A bare metal port needs a client ID to bind.** `mlnx_sdn_assist` only
binds a bare metal port that has one, and no option turns that off: the
client ID is where AFM reads the GUID to put in the partition. A port
without one is left to the other drivers, and fails to bind on
`ibphysnet`.

## Creating networks

With partitioning, each VLAN network on `ibphysnet` gets a partition of
its own. Its partition key (pkey) is the network's VLAN ID, and only that
network's ports are full members of it. Create one network per group of
hosts that should reach each other.

### A VLAN network

As an administrator:

1. **Create the network:**

   ```sh
   openstack network create infiniband-100 \
     --provider-network-type vlan \
     --provider-physical-network ibphysnet \
     --provider-segment 100 \
     --mtu 2044 \
     --disable-port-security \
     --project <project ID>
   ```

   - `--provider-network-type vlan` gives the network a partition of its
     own.
   - `--provider-physical-network ibphysnet` puts it on the InfiniBand
     physical network, which networking-mlnx syncs to AFM.
   - `--provider-segment 100` is the VLAN ID, and so the partition key:
     here 100, or `0x0064`. AFM rejects a VLAN ID that is a reserved or
     static partition's pkey. If another network's partition still has
     that pkey, the new network waits until that network is deleted.
   - `--mtu 2044` is IPoIB's datagram-mode MTU on a 2048-byte InfiniBand
     MTU. By default AFM gives each network partition's IPoIB broadcast
     group that MTU (`[opensm] ipoib_mtu = 4`, IBTA's code for 2048
     bytes).
   - `--disable-port-security`: the network's ports inherit the network's
     disabled port security.
   - `--project` gives the network to one project. To share it with
     several, leave it out and follow
     [Sharing a network](#sharing-a-network).

2. **Create its subnet:**

   ```sh
   openstack subnet create infiniband-100 \
     --network infiniband-100 \
     --subnet-range 172.26.0.0/24 \
     --allocation-pool start=172.26.0.10,end=172.26.0.250 \
     --gateway none \
     --no-dhcp
   ```

   - `--no-dhcp`, because nothing serves DHCP inside a partition (see
     [Limitations](#limitations)). Instances get their IPoIB addresses
     from the config drive, or statically.
   - `--gateway none`, because a router's port couldn't reach the
     instances either: AFM never puts one in a partition.
   - `--allocation-pool` is the range Neutron assigns the ports' addresses
     from.

**To check it,** boot an instance on it (see
[Booting instances](#booting-instances)): its port goes `ACTIVE` only once
AFM has put the port's GUID in the network's partition.

### Sharing a network

To share a network with several projects, create it without `--project`,
then run this for each project:

```sh
openstack network rbac create --type network --action access_as_shared \
  --target-project <project ID> <network ID>
```

### A flat network

A flat network on `ibphysnet` has no partition of its own. AFM puts the
ports of every flat network in one shared partition, `[opensm] flat_pkey`
(0x7ffe by default), so they can all reach each other. Use VLAN networks
to keep projects apart.

Before you start:

- networking-mlnx must include
  [Send flat network creation to the SDN controller](https://review.opendev.org/c/x/networking-mlnx/+/1014376).
  Until then, AFM rejects ports on flat networks.
- Neutron must allow flat networks on `ibphysnet` (see
  [kayobe.md](kayobe.md#for-both) for the `flat_networks` setting).

Then, as an administrator:

1. **Create the network** as for a VLAN network, but with
   `--provider-network-type flat` and no `--provider-segment`:

   ```sh
   openstack network create infiniband-flat \
     --provider-network-type flat \
     --provider-physical-network ibphysnet \
     --mtu 2044 \
     --disable-port-security \
     --project <project ID>
   ```

2. **Create its subnet** exactly as for a VLAN network, with
   `--network infiniband-flat`.

You can create it with openstack-config instead: see
[kayobe.md](kayobe.md#creating-the-network-with-openstack-config).

**To check it:** a port on a flat network goes `ACTIVE` without waiting
for AFM, so its status doesn't show that it is in the partition yet.

## Bare metal ports

A node's InfiniBand port needs an Ironic port with two settings:

- a `client-id` in its `extra`, which ends in the port's GUID;
- `ibphysnet` as its physical network.

Inspection records the client ID.
[kayobe.md](kayobe.md#for-bare-metal-nodes) covers what inspection needs,
and how to set the physical network on each Kayobe release.

### Checking an inspected port

As an administrator, list the node's ports:

```
$ openstack baremetal port list --node <node> --long -f yaml
...
- Address: 0c:42:a1:06:68:f6
  Extra: client-id='ff:00:00:00:00:00:02:00:00:02:c9:00:0c:42:a1:03:00:06:68:f6'
  Local Link Connection: {}
  Physical Network: ibphysnet
  PXE boot enabled: false
```

Check both settings: `Extra` has a `client-id` ending in the port's GUID
(here `0c:42:a1:03:00:06:68:f6`), and `Physical Network` is `ibphysnet`.

### Enrolling a port by hand

Instead of inspection, you can enroll a port yourself. Set both settings:

1. **Find the port's GUID,** from `ibstat` on the node: it is the port's
   `Port GUID`. On ConnectX adapters the GUID is usually the port's MAC
   address with `03:00` in the middle, as in the example above, but read
   it rather than assume it.

2. **Create the Ironic port:**

   ```sh
   openstack baremetal port create <MAC address> --node <node UUID> \
     --physical-network ibphysnet \
     --extra client-id=ff:00:00:00:00:00:02:00:00:02:c9:00:<GUID, 8 octets>
   ```

   The client ID is 20 octets, and the last 8 are the port's GUID.

## Booting instances

### A virtualised instance

A virtualised instance needs a `direct` port, created first.

1. **Create the port:**

   ```sh
   openstack port create --network infiniband-100 --vnic-type direct ib0
   ```

   `--vnic-type direct` asks for a virtual function, which
   `mlnx_infiniband` binds. Don't skip this step: with only `--network`,
   Nova creates a `normal` port, which nothing binds on `ibphysnet`, and
   the instance fails to boot.

2. **Boot the instance with the port:**

   ```sh
   openstack server create --flavor <flavor> --image <image> \
     --network <Ethernet network> --port ib0 <name>
   ```

   `--port ib0` attaches the port you created. When Nova binds it,
   `mlnx_infiniband` derives the port's client ID from its MAC address.
   Name the instance's other networks, such as an Ethernet one, as usual.

3. **Check the port:**

   ```sh
   openstack port show ib0 -c status -c binding_vnic_type
   ```

   On a VLAN network the port stays `DOWN` until AFM has put its GUID in
   the network's partition, then goes `ACTIVE`. On a flat network it goes
   `ACTIVE` without waiting.

In Terraform, the same port and instance are:

```hcl
data "openstack_networking_network_v2" "infiniband" {
  name = "infiniband-100"
}

resource "openstack_networking_port_v2" "infiniband" {
  name           = "ib0"
  network_id     = data.openstack_networking_network_v2.infiniband.id
  admin_state_up = true
  binding {
    vnic_type = "direct"
  }
}

resource "openstack_compute_instance_v2" "instance" {
  # ...
  network {
    port = openstack_networking_port_v2.infiniband.id
  }
}
```

### A bare metal instance

The node needs an Ironic port with a client ID first (see
[Bare metal ports](#bare-metal-ports)).

1. **Boot the instance on the network:**

   ```sh
   openstack server create --flavor baremetal --image <image> \
     --network <Ethernet network> --network infiniband-100 <name>
   ```

   A bare metal instance just names the network. Nova creates its port,
   and Ironic binds it as `baremetal`, with the client ID from the node's
   Ironic port.

   A port created first and passed with `--port` works the same: Ironic
   makes it `baremetal`, whatever its `vnic_type`, and adds the client ID.

   No driver adds a client ID for a bare metal port. If the node's Ironic
   port has no `client-id`, `mlnx_sdn_assist` doesn't bind the Neutron
   port, and Ironic fails the deployment.

2. **Check the port:** as for a virtualised instance, on a VLAN network
   it stays `DOWN` until AFM has put its GUID in the network's partition,
   then goes `ACTIVE`. On a flat network it goes `ACTIVE` without
   waiting. Find it with `openstack port list --server <name>`.

### Virtualised and bare metal on one network

One network can hold both kinds of instance: AFM puts each port in the
network's partition by its GUID, however the port is attached.

1. **Create one VLAN network,** as in [A VLAN network](#a-vlan-network).
2. **Boot the virtualised instances** on it, each with a `direct` port, as
   in [A virtualised instance](#a-virtualised-instance).
3. **Boot the bare metal instances** on it with `--network`, as in
   [A bare metal instance](#a-bare-metal-instance).

Their ports are then full members of the same partition, and can reach
each other.

## When a port stays DOWN or goes to ERROR

On a VLAN network, a port that stays `DOWN` hasn't been put in its
partition yet. A port in `ERROR` was either rejected by AFM, which never
puts it in a partition, or its job didn't complete within `[opensm]
job_timeout`. In the second case AFM still applies it later, but Neutron
keeps the port in `ERROR`. An administrator can look in AFM's log for
why:

```sh
docker logs --since <start> alternative-fabric-manager 2>&1 \
  | grep -E "Rejected|ERROR|waits" | grep -vi "key 0x"
```

(On Kayobe, the container is `alternative_fabric_manager`, on each
controller.)

- **`Rejected request:`** AFM answered 400, which networking-mlnx treats
  as final, and the port goes to `ERROR`: for example, a port without a
  usable client ID. The [README](../README.md#how-it-works) lists every
  reason.
- **`waits:`** the request may succeed later, and the port stays `DOWN`
  meanwhile. For example, a port whose GUID is still another port's
  waits until that port is deleted.
- **No message about the port, and it stays `DOWN`:** no job completes,
  for any port, until AFM has recorded the fabric's switches ("No
  expected switches yet"), or while a switch port has stopped enforcing
  partitions. See the README's
  [Expected switches](../README.md#expected-switches) and
  [When a switch port stops enforcing partitions](../README.md#when-a-switch-port-stops-enforcing-partitions).

## Limitations

- **No DHCP inside a partition.** AFM only puts ports with a client ID in
  a partition, so a DHCP agent's port is never in one. kolla's DHCP agent
  has no IPoIB interface driver anyway. Use the config drive or static
  addresses.
- **Flat networks share one partition.** Every port on any flat
  InfiniBand network can reach every other. Use VLAN networks to keep
  projects apart.
