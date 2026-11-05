# OpenSM hardening

OpenSM's defaults suit a fabric whose hosts all trust each other. A cloud's
fabric doesn't: a bare metal tenant has full control of its InfiniBand
adapter, and can send any subnet management packet (SMP) or subnet
administration (SA) request, or run a subnet manager of its own. A guest on
a virtual function has less control, but can still make SA requests.

So with the managed driver, the manager writes a hardened set of options into
`opensm.conf`, in groups. `[opensm] hardening` names the groups, separated by
commas, `all` by default, or `none`: one option chooses what to harden, for
example `hardening = m_key, enforcement` on a fabric where only M_Key
protection is wanted. A single OpenSM option can still be overridden in
`[opensm_conf]`, for example `m_key_lease_period = 120`, and wins over the
groups.

| Group | Option | OpenSM default | Hardened | Stops |
| --- | --- | --- | --- | --- |
| `m_key` | `m_key` | `0` | Random, shared | A host reading or changing port settings with SMPs |
| `m_key` | `m_key_protection_level` | `0` | `2` | As above |
| `m_key` | `m_key_lease_period` | `0` | `60` | A fabric staying locked after its M_Key is lost |
| `sm_key` | `sm_key` | `0x1` | Random, shared | A rogue subnet manager taking over by handover |
| `sa_key` | `sa_key` | `0x1` | Random, shared | Any client being trusted by the SA with the well-known key |
| `sa_trust` | `sa_enhanced_trust_model` | `FALSE` | `TRUE` | Untrusted clients using SA requests beyond the minimal set |
| `sa_trust` | `sa_etm_allow_untrusted_proxy_requests` | `TRUE` | `FALSE` | A client acting for another port |
| `sa_trust` | `sa_etm_allow_untrusted_guidinfo_rec` | `TRUE` | `FALSE` | A client adding alias GUIDs to its port |
| `sa_trust` | `sa_etm_allow_guidinfo_rec_by_vf` | `TRUE` | `FALSE` | A virtual function adding alias GUIDs |
| `sa_trust` | `sa_etm_max_num_mcgs` | `0` (no limit) | `128` | One port exhausting the SA's multicast groups |
| `sa_trust` | `sa_etm_max_num_srvcs` | `0` (no limit) | `32` | One port exhausting the SA's service records |
| `sa_trust` | `sa_etm_max_num_event_subs` | `0` (no limit) | `32` | One port exhausting the SA's event subscriptions |
| `enforcement` | `part_enforce` | `both` | `both` | Packets crossing partitions at a switch port |
| `enforcement` | `allow_both_pkeys` | `FALSE` | `FALSE` | A port being both a full and a limited member of a partition |
| `enforcement` | `sa_check_sgid_spoofing` | `TRUE` | `TRUE` | A client claiming another port's GID in an SA request |
| `babbling_ports` | `babbling_port_policy` | `FALSE` | `TRUE` | A port flooding the subnet manager with traps |

The `enforcement` group is OpenSM's defaults already. It is written anyway,
so that `opensm.conf` shows the options and nothing weakens them by
accident.

## Choosing what to harden

| Fabric | `hardening` |
| --- | --- |
| Only OpenStack's hosts, guests and bare metal nodes (the default) | `all` |
| Shared with hosts outside OpenStack that run SA diagnostics or ConnectX-3 SR-IOV | `m_key, sm_key, enforcement`, see the costs below |
| Shared with hosts whose operators can't use the M_Key yet | `enforcement`, and plan M_Key protection: without it a bare metal tenant can reconfigure switch ports |

## The keys

The manager generates `m_key`, `sm_key` and `sa_key` on first start, as
random 64-bit values, and keeps them in its database. Every OpenSM of the
fabric must use the same keys: in high availability, a standby that took
over with another M_Key couldn't configure the ports. Keeping them in the
database gives every manager the same keys, and keeps them across restarts.
Only the keys of the chosen groups are written. A key set in
`[opensm_conf]` wins over the generated one.

`opensm.conf` holds the keys, so the manager writes it readable only by
root. To print them:

```sh
docker exec alternative-fabric-manager alternative-fabric-manager --show-keys
```

## M_Key protection

The M_Key is what stops a host from reconfiguring the fabric. Without it, any
host can send an SMP that changes a port's settings, including a switch
port's pkey table, which is what switches enforce partitions with. A bare
metal tenant could then add its own switch port to any partition.

With `m_key_protection_level 2`, a port answers a SubnGet, and accepts a
SubnSet, only if the SMP carries its M_Key. OpenSM sets the M_Key on every
port when it configures it.

`m_key_lease_period 60` is the recovery path. A port that receives an SMP
with the wrong M_Key starts its lease timer; if no SMP with the right M_Key
arrives within 60 seconds, the port clears its protection. While the master
OpenSM runs, it sends SMPs with the right M_Key at every sweep, by default
every 10 seconds, so the lease doesn't run out. It runs out only if no
subnet manager has the M_Key, for example after the manager's database was
lost, and then a new subnet manager can take over within a minute rather
than never. A lease of `0` means protection never clears: losing the M_Key
then locks the fabric until every port is reset.

Cost: diagnostic tools that send SMPs from a host, such as `ibnetdiscover`,
`iblinkinfo`, `smpquery`, `ibportstate`, `ibhosts` and `sminfo`, need the
M_Key, with `-y <m_key>`. Tools that only read the local adapter, such as
`ibstat`, don't.

## SM_Key

Subnet managers authenticate each other with the SM_Key when one hands over
to another. With OpenSM's well-known default, any host running OpenSM could
negotiate a handover from the master. With a secret SM_Key, only the
fabric's own subnet managers can. A rogue subnet manager can still try to
win the election, which is why the M_Key matters most: without it, the rogue
can't configure the ports.

## The SA enhanced trust model

A client that sends the SA_Key is trusted, and one that sends `0` is not.
With the enhanced trust model, untrusted clients get only what an IPoIB or
RDMA application needs: path records between two given ports, multicast
membership, service records, event subscriptions and class port info.
Everything else, such as dumping every node or port record, needs the
SA_Key.

- **No proxy requests:** an untrusted client can't join a multicast group,
  or register a service, for another port.
- **No alias GUIDs:** an untrusted client, and a virtual function even with
  the SA_Key, can't add an alias GUID to its port. With an alias GUID, a port
  could take another port's identity, for example the GUID that a network's
  partition lists.
- **Registration limits:** a port can register at most 128 multicast
  groups, 32 services and 32 event subscriptions, so one port can't exhaust
  the SA for everyone.

Cost:

- **ConnectX-3:** its SR-IOV gives virtual functions alias GUIDs, which the
  hypervisor's driver adds with untrusted GUIDInfoRecord requests. On a
  fabric with ConnectX-3 virtual functions, set
  `sa_etm_allow_untrusted_guidinfo_rec = TRUE`. ConnectX-4 and later give
  virtual functions virtual ports instead, which this doesn't affect.
- **Diagnostics:** `saquery` dumps need `--sa_key` or fail.
- **Applications** that register more than the limits, which is unusual,
  need higher limits.

## Partition enforcement

`part_enforce both` makes switches check the partition of every packet in
both directions at the port, which is what makes a partition a boundary.
`allow_both_pkeys FALSE` keeps a port from being both a full and a limited
member of the same partition, which the manager's partitions never need.

## Babbling ports

With `babbling_port_policy TRUE`, OpenSM disables a port that keeps sending
it traps, so a misbehaving or hostile host can't flood the subnet manager.

## The default partition

The shipped `[opensm] default_partition` gives every port only limited
membership of the default partition, `ALL=limited, SELF=full`: limited
members can only reach full members, so ports on different networks can't
reach each other there, while every port can still reach the subnet manager.
It is in the partitions file, not `opensm.conf`, so `hardening` doesn't
change it.

Ports on flat networks, which share the default partition, are added as full
members, so they still reach each other. Anything else that uses IPoIB on
the default partition, such as a host's own interface, needs to be a full
member: list its GUID with `=full` in `default_partition`, or keep
`ALL=full` on a fabric where tenants don't share the default partition.

A guest's IPoIB interface uses the pkey at index 0 of its port, which the
manager gives each network's partition (`indx0`). With `ALL=full`, a guest
could still add an IPoIB interface for the default partition, `0x7fff`, and
reach other tenants there; limited membership is what prevents that.

## Turning hardening on for a running fabric

OpenSM reads `opensm.conf` only when it starts, so the manager must be
restarted, which restarts OpenSM. OpenSM then sets the M_Key on every port at
its first sweep. Hosts that run diagnostic tools need the M_Key from then on.
