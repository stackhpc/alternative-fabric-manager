# InfiniBand networks and instances

This guide creates InfiniBand networks and boots virtualised and bare
metal instances on them, in a cloud whose networking-mlnx syncs to the
alternative fabric manager (AFM): configured as in
[kayobe.md](kayobe.md), or as in networking-mlnx's DevStack guide.
networking-mlnx's
[partition keys guide](https://opendev.org/x/networking-mlnx/src/branch/master/doc/source/partition-keys.rst)
says what it guarantees about a port and its partition.

## Virtualised and bare metal

AFM puts a port in its network's partition by the GUID in the port's DHCP
client ID, however the port is attached. What differs is who binds the
port and where the client ID comes from. Neither needs the user to know a
GUID:

| | Virtualised instances | Bare metal nodes (Ironic) |
| --- | --- | --- |
| The port | `vnic_type` `direct`, on a virtual function: create it first, then `openstack server create --port` | `vnic_type` `baremetal`, on the node's adapter: `openstack server create --network` is enough |
| Bound by | `mlnx_infiniband` (VIF type `ib_hostdev`) | `mlnx_sdn_assist` |
| Its client ID | Derived by `mlnx_infiniband` from the port's MAC address when Nova binds it, as the virtual function's GUID is | Copied by Ironic from its port's `extra` `client-id`, which inspection records |

Users never set a client ID on a Neutron port themselves, in either case:
only administrators and services can set one on a network that
`mlnx_sdn_assist` syncs, since the controller takes the GUID from it.

`mlnx_sdn_assist` only binds a bare metal port that has a client ID, and no
option turns that off: the client ID is where AFM reads the GUID to put in
the partition. A port without one is left to the other drivers, and fails
to bind on `ibphysnet`.

## Bare metal ports

A node's InfiniBand port needs an Ironic port with a `client-id` in its
`extra` and `ibphysnet` as its physical network. Inspection records the
client ID; [kayobe.md](kayobe.md#for-bare-metal-nodes) covers what
inspection needs, and how to set the physical network on each Kayobe
release. After inspection, the port looks like this, its client ID ending
in its GUID:

```
$ openstack baremetal port list --node <node> --long -f yaml
...
- Address: 0c:42:a1:06:68:f6
  Extra: client-id='ff:00:00:00:00:00:02:00:00:02:c9:00:0c:42:a1:03:00:06:68:f6'
  Local Link Connection: {}
  Physical Network: ibphysnet
  PXE boot enabled: false
```

To enroll a port by hand instead, set both: the client ID is 20 octets,
the last 8 the port's GUID.

```sh
openstack baremetal port create <MAC address> --node <node UUID> \
  --physical-network ibphysnet \
  --extra client-id=ff:00:00:00:00:00:02:00:00:02:c9:00:<GUID, 8 octets>
```

On ConnectX adapters the port's GUID is usually its MAC address with
`03:00` in the middle, as above, but read it from `ibstat` on the node
rather than assume it.

## Creating networks

With partitioning, each VLAN network on `ibphysnet` gets a partition of
its own, whose partition key is its VLAN ID, and only that network's ports
are full members of it. Create one per group of hosts that should reach
each other, as an administrator:

```sh
openstack network create infiniband-100 \
  --provider-network-type vlan \
  --provider-physical-network ibphysnet \
  --provider-segment 100 \
  --mtu 2044 \
  --disable-port-security \
  --project <project ID>
openstack subnet create infiniband-100 \
  --network infiniband-100 \
  --subnet-range 172.26.0.0/24 \
  --allocation-pool start=172.26.0.10,end=172.26.0.250 \
  --gateway none \
  --no-dhcp
```

2044 is IPoIB's datagram-mode MTU on a 2048-byte InfiniBand MTU.
Instances get their IPoIB addresses from the config drive, or statically:
nothing serves DHCP inside a partition (see Limitations). `--project`
gives the network to one project; to share it with several, leave it out
and add `openstack network rbac create --type network --action
access_as_shared --target-project <project ID> <network ID>` for each. The
ports inherit the network's disabled port security.

A flat network on `ibphysnet` has no partition of its own: AFM puts the
ports of every flat network in one shared partition, `[opensm] flat_pkey`,
so they can all reach each other. It needs networking-mlnx's "Send flat
network creation to the SDN controller"
(https://review.opendev.org/c/x/networking-mlnx/+/1014376), and Neutron
must allow flat networks on `ibphysnet`. Create it as above with
`--provider-network-type flat` and no `--provider-segment`, or with
Kayobe (see [kayobe.md](kayobe.md#creating-the-network-with-kayobe)).

## Booting instances

A bare metal instance just names the network. Nova creates its port, and
Ironic binds it as `baremetal` with the client ID from the node's Ironic
port:

```sh
openstack server create --flavor baremetal --image <image> \
  --network <Ethernet network> --network infiniband-100 <name>
```

A port created first and passed with `--port` works the same: Ironic
makes it `baremetal`, whatever its `vnic_type`, and adds the client ID. No
driver adds one for a bare metal port: if its Ironic port has no
`client-id`, `mlnx_sdn_assist` doesn't bind it, and Ironic fails the
deployment.

A virtualised instance needs a `direct` port, created first: with
`--network`, Nova creates a `normal` port, which nothing binds on
`ibphysnet`, and the instance fails to boot. `mlnx_infiniband` derives the
port's client ID from its MAC address when Nova binds it.

```sh
openstack port create --network infiniband-100 --vnic-type direct ib0
openstack server create --flavor <flavor> --image <image> \
  --network <Ethernet network> --port ib0 <name>
```

or in Terraform:

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

On a VLAN network, either kind of port stays `DOWN` until AFM has put its
GUID in the network's partition. On a flat network it goes `ACTIVE` without
waiting.

## Limitations

- **No DHCP inside a partition.** AFM only puts ports with a client ID in
  a partition, so a DHCP agent's port is never in one, and kolla's DHCP
  agent has no IPoIB interface driver anyway. Use the config drive or
  static addresses.
- **Flat networks share one partition**, so every port on any flat
  InfiniBand network can reach every other. Use VLAN networks to keep
  projects apart.
