# Deploying with Kayobe

This guide deploys the alternative fabric manager (AFM) in HA mode on a
Kayobe-managed OpenStack, as a custom container on every controller, using
Kayobe's `controller_containers`, with the patterns of
[stackhpc-kayobe-config](https://github.com/stackhpc/stackhpc-kayobe-config):
`pre`/`post` task files beside the container, an HAProxy service in
`kolla/config/haproxy/services.d`, and a deployment check as a Kayobe hook.
Read [specs/ha.md](../specs/ha.md) for what HA mode guarantees.

The result:

- an AFM container on each controller, all HAProxy backends behind the
  internal VIP; one of them, the leader, runs OpenSM under its watchdog;
- AFM's database in kolla's MariaDB (Galera), created idempotently with
  the root credentials from kolla's `passwords.yml`;
- networking-mlnx's `mlnx_sdn_assist` pointing at the VIP, so that each
  InfiniBand network's ports are put in its partition, for virtualised
  instances, bare metal nodes, or both (see
  [Virtualised and bare metal](#virtualised-and-bare-metal)).

## Requirements

- **Every controller has a port on the InfiniBand fabric** that AFM manages:
  any instance may become leader and run OpenSM there. If the controllers
  have none, use another group's containers, such as `infra_vm_containers`,
  on hosts that do, and the same files.
- **The `ib_umad` module on every controller**, which OpenSM uses to reach
  the fabric, and, for virtualised instances, **virtual functions on the
  compute hosts**: see [InfiniBand hosts](#infiniband-hosts).
- **Docker.** AFM runs OpenSM's container through the Docker API. Podman
  is untested, and support for it is a future improvement (see below).
- **Kayobe's own OpenSM is off**: `opensm_enabled: false`, the default. AFM
  runs OpenSM itself, and two would fight.
- **No other subnet manager** on the fabric, managed or embedded in a
  switch.
- **Galera** from kolla-ansible, reached through the internal VIP. Only the
  majority side of a Galera partition can elect a leader (see
  specs/ha.md, The lease).
- **The AFM image** in a registry the controllers can pull from, built from
  this repository's Dockerfile, and OpenSM's image,
  `ghcr.io/stackhpc/docker-opensm`, pinned by digest. OpenSM's image must
  have `python3`, which runs the watchdog.
- **M_Key and SM_Key protection.** HA mode refuses to start without the
  `m_key` and `sm_key` hardening groups. Read
  [opensm-hardening.md](opensm-hardening.md) before enabling them on a
  fabric shared with hosts outside OpenStack.

## Virtualised and bare metal

AFM puts a port in its network's partition by the GUID in the port's DHCP
client ID, however the port is attached. What differs is who binds the
port and where the client ID comes from. Neither needs the user to know a
GUID:

| | Virtualised instances | Bare metal nodes (Ironic) |
| --- | --- | --- |
| The port | `vnic_type` `direct`, on a virtual function: create it first, then `openstack server create --port` | `vnic_type` `baremetal`, on the node's adapter: `openstack server create --network` is enough |
| Bound by | `mlnx_infiniband` (VIF type `ib_hostdev`) | `mlnx_sdn_assist` |
| Its client ID | Derived by `mlnx_infiniband` from the port's MAC address, as the virtual function's GUID is | Copied by Ironic from its port's `extra` `client-id`, which inspection records |
| Hosts need | Virtual functions and `ib_ipoib` on the compute hosts; kolla's SR-IOV and Mellanox agents | An IPA image with the Mellanox drivers; Ironic ports for the InfiniBand interfaces, with `ibphysnet` as their physical network |

Both need `mlnx_sdn_assist`, the controllers' `ib_umad`, and VLAN networks
on `ibphysnet` for partitioning: a flat network's ports all share one
partition. One deployment can have both; configure each part below that
applies.

`mlnx_sdn_assist` only binds a bare metal port that has a client ID, and no
option turns that off: the client ID is where AFM reads the GUID to put in
the partition. A port without one is left to the other drivers, and fails
to bind on `ibphysnet`. Let inspection record it rather than typing it in.

## InfiniBand hosts

Every controller needs `ib_umad`. The compute hosts need virtual functions
only for virtualised instances; skip their plays for bare metal alone. The
in-kernel Mellanox drivers are enough: OFED isn't needed, though the
`sriov` role installs NVIDIA's firmware tools to enable SR-IOV in the
adapter's firmware.

The virtual functions to create, per device, in
`etc/kayobe/inventory/group_vars/compute/sriov`:

```yaml
---
sriov_devices:
  # InfiniBand
  - name: ibs4f0
    numvfs: 16
```

Create only as many as you need: many virtual functions break `ip a` (see
Known limitations).

The playbook's third play runs on a `sriov` group. Add the compute hosts
to it in `etc/kayobe/inventory/groups`:

```ini
[sriov:children]
compute
```

`etc/kayobe/ansible/sriov.yml`:

```yaml
---
- name: Ensure the compute hosts' InfiniBand modules are loaded
  hosts: compute
  gather_facts: false
  tasks:
    - name: Ensure ib_ipoib is loaded, which the sriov role needs
      community.general.modprobe:
        name: ib_ipoib
        state: present
        persistent: present
      become: true

- name: Ensure the controllers' InfiniBand modules are loaded
  hosts: controllers
  gather_facts: false
  tasks:
    # Any controller may become leader and run OpenSM.
    - name: Ensure ib_umad is loaded, which OpenSM needs
      community.general.modprobe:
        name: ib_umad
        state: present
        persistent: present
      become: true

- name: Ensure the virtual functions exist
  hosts: sriov
  tags:
    - sriov
  vars:
    sriov_numvfs_required_by:
      - docker.service
  tasks:
    - ansible.builtin.import_role:
        name: stackhpc.linux.sriov
  handlers:
    - name: reboot
      ansible.builtin.set_fact:
        kayobe_needs_reboot: true

- name: Reboot when required
  hosts: overcloud
  tags:
    - reboot
  tasks:
    - name: Reboot
      ansible.builtin.reboot:
        reboot_timeout: 3600
      become: true
      when: kayobe_needs_reboot | default(false) | bool
```

The `sriov` role needs stackhpc.linux 1.3.0 or later, in
`etc/kayobe/ansible/requirements.yml`:

```yaml
collections:
  - name: stackhpc.linux
    version: 1.3.0
```

Install it with `kayobe control host bootstrap`. The playbook uses Kayobe's
user and virtualenv, so run it after `kayobe overcloud host configure`,
before `kayobe overcloud service deploy`, or as a hook after host
configure:

```sh
kayobe playbook run $KAYOBE_CONFIG_PATH/ansible/sriov.yml
```

The last play reboots every host whose virtual functions needed it, all
at once. On a running cloud, limit it to a few hosts at a time
(`--limit`), or skip it (`--skip-tags reboot`) and reboot them yourself.

## Secrets

AFM needs a database password and an API token, which networking-mlnx
sends. Keep them in Kayobe's vaulted secrets, and add them to kolla's
`passwords.yml` as custom passwords, so that AFM's tasks and kolla-ansible's
templates read them from one place, beside kolla's own `database_password`.

`etc/kayobe/secrets.yml` (vaulted):

```yaml
secrets_afm_database_password: "<generated>"
secrets_afm_api_token: "<generated>"
```

`etc/kayobe/kolla.yml`:

```yaml
kolla_ansible_extra_custom_passwords:
  afm_database_password: "{{ secrets_afm_database_password }}"
  afm_api_token: "{{ secrets_afm_api_token }}"
```

`kayobe overcloud service deploy` (or `kayobe overcloud service
reconfigure`) writes them to `$KOLLA_CONFIG_PATH/passwords.yml`.

## Variables

`etc/kayobe/alternative-fabric-manager.yml`, laid out like Kayobe's own
configuration files:

```yaml
---
###############################################################################
# Alternative fabric manager (AFM) images.

# AFM's image, built from this repository's Dockerfile.
afm_image: "ghcr.io/stackhpc/alternative-fabric-manager"

# AFM's image tag.
afm_tag: "<release>"

# OpenSM's image, which AFM starts on the leader. Pin it by digest. It must
# have python3, which runs the watchdog.
afm_opensm_image: "ghcr.io/stackhpc/docker-opensm@sha256:<digest>"

###############################################################################
# AFM API.

# The API port on each controller. HAProxy listens on the same port on the
# internal VIP.
afm_api_port: 8090

# networking-mlnx's [sdn] domain.
afm_domain: "cloudx"

###############################################################################
# AFM container.

# The container's name. OpenSM's container joins its PID namespace, so this
# must match [opensm] manager_container in AFM's configuration.
afm_container_name: "alternative_fabric_manager"

# AFM's configuration directory on the host.
afm_config_dir: "/etc/alternative-fabric-manager"

# The directory the manager and OpenSM's watchdog share.
afm_state_dir: "/run/alternative-fabric-manager"

# The container API AFM uses to run OpenSM's container: Docker's socket, or,
# with container_engine: podman, Podman's Docker-compatible one (untested).
afm_container_socket: >-
  {{ '/run/podman/podman.sock' if container_engine == 'podman'
     else '/var/run/docker.sock' }}

###############################################################################
# AFM database.

# The host on which AFM's database is created: one that kolla-ansible
# manages, so that it has the kolla_toolbox container.
afm_database_host: "{{ groups['controllers'][0] }}"
```

Every container command below uses Kayobe's `container_engine`, `docker`
or `podman`.

## The container

`etc/kayobe/controllers.yml`:

```yaml
controller_containers:
  alternative_fabric_manager:
    image: "{{ afm_image }}"
    tag: "{{ afm_tag }}"
    pre: "{{ kayobe_config_path }}/containers/alternative-fabric-manager/pre.yml"
    post: "{{ kayobe_config_path }}/containers/alternative-fabric-manager/post.yml"
    command: "--config-file {{ afm_config_dir }}/afm.conf"
    # An init process reaps the OpenSM processes that the shared PID
    # namespace reparents to the manager.
    init: true
    network_mode: host
    restart_policy: unless-stopped
    volumes:
      - "{{ afm_config_dir }}:{{ afm_config_dir }}:ro"
      - "/etc/opensm:/etc/opensm"
      - "{{ afm_container_socket }}:/var/run/docker.sock"
      # The tmpfs the manager and OpenSM's watchdog share
      - "{{ afm_state_dir }}:{{ afm_state_dir }}"
      # The host's processes, to verify that no OpenSM runs
      - "/proc:/host/proc:ro"
      - "alternative_fabric_manager:/var/lib/alternative-fabric-manager"
```

Kayobe deploys `controller_containers` in `kayobe overcloud service
deploy`, after kolla-ansible's deploy, so MariaDB and HAProxy are up by
then. Kayobe's container module sets no stop timeout, so Docker's default
of 10 s applies: the configuration below declares it, with a shorter
`shutdown_timeout`, so that a stopped leader steps down and releases the
lease before Docker kills it.

### Before the container: `pre.yml`

`etc/kayobe/containers/alternative-fabric-manager/pre.yml`:

```yaml
---
- name: Ensure AFM's directories exist
  ansible.builtin.file:
    path: "{{ item.path }}"
    state: directory
    mode: "{{ item.mode }}"
  loop:
    - { path: "{{ afm_config_dir }}", mode: "0700" }
    - { path: "/etc/opensm", mode: "0755" }
    - { path: "{{ afm_state_dir }}", mode: "0700" }
  become: true

- name: Read kolla's passwords
  ansible.builtin.set_fact:
    afm_kolla_passwords: >-
      {{ lookup('ansible.builtin.file', kolla_config_path ~ '/passwords.yml')
         | from_yaml }}
  no_log: true

- name: Ensure AFM's configuration is present
  ansible.builtin.template:
    src: "{{ kayobe_config_path }}/containers/alternative-fabric-manager/afm.conf.j2"
    dest: "{{ afm_config_dir }}/afm.conf"
    mode: "0600"
  become: true

# Idempotently, as kolla-ansible creates its own databases: with the
# kolla_toolbox container's MySQL modules, as kolla's database_user.
# kolla_toolbox is a long-lived container on every host kolla-ansible
# manages, so this runs once, on one of them, whichever hosts AFM runs on.
- name: Ensure AFM's database and user exist
  ansible.builtin.command:
    argv:
      - "{{ container_engine }}"
      - exec
      - kolla_toolbox
      - ansible
      - localhost
      - -o
      - -m
      - "{{ item.module }}"
      - -a
      - "{{ item.args | to_json }}"
  loop:
    - module: mysql_db
      args:
        login_host: "{{ kolla_internal_vip_address }}"
        login_port: 3306
        login_user: "{{ afm_kolla_passwords.database_user | default('root') }}"
        login_password: "{{ afm_kolla_passwords.database_password }}"
        name: afm
    - module: mysql_user
      args:
        login_host: "{{ kolla_internal_vip_address }}"
        login_port: 3306
        login_user: "{{ afm_kolla_passwords.database_user | default('root') }}"
        login_password: "{{ afm_kolla_passwords.database_password }}"
        name: afm
        password: "{{ afm_kolla_passwords.afm_database_password }}"
        host: "%"
        priv: "afm.*:ALL"
        append_privs: true
  register: afm_database
  changed_when: "'CHANGED' in afm_database.stdout"
  delegate_to: "{{ afm_database_host }}"
  run_once: true
  become: true
  no_log: true

- name: Ensure AFM's schema is up to date
  ansible.builtin.command:
    argv:
      - "{{ container_engine }}"
      - run
      - --rm
      - --network=host
      - -v
      - "{{ afm_config_dir }}:{{ afm_config_dir }}:ro"
      - "{{ afm_image }}:{{ afm_tag }}"
      - --config-file
      - "{{ afm_config_dir }}/afm.conf"
      - --db-upgrade
  register: afm_db_upgrade
  changed_when: "'Running upgrade' in afm_db_upgrade.stderr"
  run_once: true
  become: true
```

`login_user` falls back to `root`, kolla's default `database_user`. The
passwords are in the command's arguments, so they are briefly visible in
that host's process list, as with kolla-ansible's own database tasks;
`no_log` keeps them out of Kayobe's output.

### AFM's configuration

`etc/kayobe/containers/alternative-fabric-manager/afm.conf.j2`:

```ini
[api]
# Each instance listens on its own internal address; HAProxy listens on the
# VIP.
host = {{ internal_net_name | net_ip }}
port = {{ afm_api_port }}
url_prefix = /ufmRestV3
domain = {{ afm_domain }}
token = {{ afm_kolla_passwords.afm_api_token }}

[database]
connection = mysql+pymysql://afm:{{ afm_kolla_passwords.afm_database_password }}@{{ kolla_internal_vip_address }}:3306/afm

[opensm]
driver = managed
container = opensm
image = {{ afm_opensm_image }}
manager_container = {{ afm_container_name }}
config_dir = /etc/opensm
host_config_dir = /etc/opensm
hardening = m_key, sm_key, enforcement
# HA mode: a job must outlast a takeover (see specs/ha.md, Timeouts)
job_timeout = 300
start_timeout = 60

[ha]
ha = true
node = {{ inventory_hostname }}
ha_state_dir = {{ afm_state_dir }}
host_proc = /host/proc
# Kayobe's containers have Docker's default stop timeout
container_stop_timeout = 10
# If you lower self_stop_timeout (20 s), keep it well above Galera's
# eviction of a lost node, evs.suspect_timeout (5 s) plus a view change,
# during which no node can commit: below it, losing any Galera node stops
# OpenSM.
shutdown_timeout = 8
```

Every instance must have the same `[ha]` timings, which the first one
stores in the database: an instance with others refuses to be a candidate.

### After the container: `post.yml`

`etc/kayobe/containers/alternative-fabric-manager/post.yml`:

```yaml
---
- name: Wait for AFM's API
  ansible.builtin.uri:
    url: "http://{{ internal_net_name | net_ip }}:{{ afm_api_port }}/healthz"
  register: afm_health
  until: afm_health.status == 200
  retries: 30
  delay: 2
```

## HAProxy

`etc/kayobe/kolla/config/haproxy/services.d/alternative-fabric-manager.cfg`,
templated by Kayobe, then by kolla-ansible inside the `raw` block:

```
{% raw %}
frontend alternative_fabric_manager_frontend
    mode http
    option httplog
    bind {{ kolla_internal_vip_address }}:8090
    default_backend alternative_fabric_manager_backend

backend alternative_fabric_manager_backend
    mode http
    # Any instance answers requests; /healthz fails on one that is
    # stopping or can't reach the database.
    option httpchk GET /healthz
{% for host in groups['controllers'] %}
{% set host_name = hostvars[host].ansible_facts.hostname %}
{% set host_ip = 'api' | kolla_address(host) %}
    server {{ host_name }} {{ host_ip }}:8090 check inter 2000 rise 2 fall 5
{% endfor %}
{% endraw %}
```

## Neutron, Nova and Ironic

### For both

The InfiniBand physical network, for VLAN networks, in
`etc/kayobe/neutron.yml`, beside the deployment's existing physical
networks:

```yaml
kolla_neutron_ml2_network_vlan_ranges:
  - physical_network: physnet1
  # Without a range, only an administrator can create a VLAN network here,
  # choosing its VLAN ID, and so its partition key. Add, for example,
  # range: "100:199" to let projects have one allocated.
  - physical_network: ibphysnet
```

The mechanism drivers, in the same file. This list replaces kolla's
defaults, so keep any others the deployment uses, such as `baremetal`.
ML2 tries them in order when it binds a port:

```yaml
kolla_neutron_ml2_mechanism_drivers:
  - ovn
  # Binds virtualised instances' direct ports on the InfiniBand network to
  # virtual functions. ovn doesn't bind direct ports without switchdev, so
  # the two don't compete.
  - mlnx_infiniband
  # Binds bare metal ports that have a client ID, and sends each network on
  # ibphysnet, and each bound port with a client ID, to AFM. Before
  # genericswitch, which binds bare metal ports too.
  - mlnx_sdn_assist
  # Only with Ironic on Ethernet switches.
  - genericswitch
  # Only for virtualised instances: kolla enables the SR-IOV agent for
  # Nova's PCI passthrough list. After mlnx_infiniband, which must bind
  # InfiniBand direct ports.
  - sriovnicswitch
```

`mlnx_infiniband` must only add client IDs to InfiniBand ports: that needs
networking-mlnx with "Only add client-id to InfiniBand ports".

Point `mlnx_sdn_assist` at AFM's VIP, and give `ibphysnet` its MTU, in
`etc/kayobe/kolla/config/neutron/ml2_conf.ini`. Kayobe templates this
file with its own variables, so it can use `alternative-fabric-manager.yml`'s,
and the token is Kayobe's secret:

```ini
[ml2]
# Neutron caps every network's MTU at global_physnet_mtu, 1500 by default.
# Raise that (see neutron.conf below), and keep the Ethernet networks at
# their own MTU.
path_mtu = 1500
physical_network_mtus = physnet1:1500,ibphysnet:2044

[sdn]
url = http://{{ kolla_internal_fqdn }}:{{ afm_api_port }}/ufmRestV3
domain = {{ afm_domain }}
token = {{ secrets_afm_api_token }}
sync_enabled = True
physical_networks = ibphysnet
bind_normal_ports = False
```

and `etc/kayobe/kolla/config/neutron.conf`, unless `global_physnet_mtu` is
already 2044 or more:

```ini
[DEFAULT]
global_physnet_mtu = 2044
```

2044 is IPoIB's datagram-mode MTU on a 2048-byte InfiniBand MTU. List
every physical network in `physical_network_mtus`, with the deployment's
real MTUs in place of 1500: one left out gets 2044.

Leave `bind_normal_ports` off: it lets `mlnx_sdn_assist` bind DHCP and
router ports on `ibphysnet`, which AFM never puts in a partition, so they
couldn't reach the instances anyway (see Known limitations). Leave `[sdn]
retry_count` at its default, -1: AFM answers 503 to every request while
frozen, and with a finite count a long freeze would put new ports in
`ERROR`, and a port deletion that runs out of retries holds every port
with the same GUID in `ERROR` until an operator clears it.

Without Ironic, kolla only allows flat networks on the physical networks
it knows of. For a flat InfiniBand network, add to `ml2_conf.ini`:

```ini
[ml2_type_flat]
flat_networks = physnet1,ibphysnet
```

### For virtualised instances

Enable kolla-ansible's SR-IOV and Mellanox agents in `etc/kayobe/kolla.yml`:

```yaml
kolla_enable_neutron_sriov: true
kolla_enable_neutron_mlnx: true
```

Map the InfiniBand physical network to the compute hosts' device in
`etc/kayobe/kolla/globals.yml`. kolla also builds Nova's PCI passthrough
list from the SR-IOV mapping:

```yaml
neutron_sriov_physnet_mappings:
  ibphysnet: ibs4f0
neutron_mlnx_physnet_mappings:
  ibphysnet: ibs4f0
```

Nova must schedule instances only to hosts with a free virtual function,
and, since a takeover can hold a port's job for up to the job timeout,
300 s, wait longer for its ports to plug. `etc/kayobe/kolla/config/nova.conf`:

```ini
[DEFAULT]
vif_plugging_timeout = 420

[filter_scheduler]
# Nova's default filters, and PciPassthroughFilter
enabled_filters = ComputeFilter,ComputeCapabilitiesFilter,ImagePropertiesFilter,ServerGroupAntiAffinityFilter,ServerGroupAffinityFilter,PciPassthroughFilter
```

Check `enabled_filters` against your Nova release's default and any
filters the deployment already adds.

### For bare metal nodes

Ironic needs the `neutron` network interface, and must fail a deployment
whose port fails to bind: keep `[neutron] fail_on_port_binding_failure =
True`, the default. Its wait for ports, `[agent]
neutron_agent_max_attempts` × `neutron_agent_status_retry_interval`, is
1000 s by default, longer than a takeover's 300 s.

Inspection records each InfiniBand port's client ID in the Ironic port's
`extra` `client-id`, if the inspection image can see the port. When Kayobe
builds IPA (`ipa_build_images: true`), add the Mellanox drivers in
`etc/kayobe/ipa.yml`; with a prebuilt IPA image, make sure it has them:

```yaml
ipa_build_dib_elements_extra:
  # Installs packages and loads the drivers that Mellanox adapters need.
  - "mellanox"
```

Inspection must also create a port for the InfiniBand interface. Kayobe's
default only adds the interface that PXE booted, so in
`etc/kayobe/inspector.yml`:

```yaml
# Add a port for every interface, including InfiniBand ones, which don't
# PXE boot and have no IP address during inspection.
inspector_add_ports: "all"
```

The port also needs `ibphysnet` as its physical network. Without one,
Ironic lets any network use it, so an Ethernet network's port could land
on it. How to set it depends on the Kayobe release:

- **Kayobe 19 (2025.2) and later** use Ironic's built-in inspection, whose
  `physical-network` hook maps by IP address, which an InfiniBand interface
  doesn't have during inspection. Set it on each InfiniBand port after
  inspection, the ports whose `extra` has a `client-id`:

  ```sh
  openstack baremetal port set --physical-network ibphysnet <port UUID>
  ```

- **Kayobe 18 (2025.1) and earlier**, with standalone ironic-inspector and
  [stackhpc-inspector-plugins](https://github.com/stackhpc/stackhpc-inspector-plugins),
  can have the `ib_physnet` hook set it, in `etc/kayobe/inspector.yml`:

  ```yaml
  inspector_processing_hooks_extra:
    # Assigns physical_network to InfiniBand ports.
    - ib_physnet
    # For Ethernet ports with genericswitch: the switch's name in
    # local_link_connection, and a physical network from it.
    - system_name_llc
    - system_name_physnet
  ```

  and `etc/kayobe/kolla/config/ironic-inspector.conf`:

  ```ini
  [port_physnet]
  ib_physnet = ibphysnet
  ```

After inspection, the node's InfiniBand port looks like this, its client ID
ending in its GUID:

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

## The InfiniBand network

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

Instances get their IPoIB addresses from the config drive, or statically:
nothing serves DHCP inside a partition. `--project` gives the network to
one project; to share it with several, leave it out and add `openstack
network rbac create --type network --action access_as_shared
--target-project <project ID> <network ID>` for each. The ports inherit
the network's disabled port security.

A flat network on `ibphysnet` has no partition of its own: AFM puts the
ports of every flat network in one shared partition, `[opensm] flat_pkey`,
so they can all reach each other. It also needs networking-mlnx's "Send
flat network creation to the SDN controller"
(https://review.opendev.org/c/x/networking-mlnx/+/1014376), and, without
Ironic, `flat_networks` (above). With Kayobe's `openstack_networks`, in
`etc/kayobe/openstack-config.yml` or wherever the deployment keeps them:

```yaml
openstack_networks:
  - "{{ openstack_network_infiniband }}"

openstack_network_infiniband_name: "infiniband"

openstack_network_infiniband:
  name: "{{ openstack_network_infiniband_name }}"
  provider_network_type: "flat"
  provider_physical_network: "ibphysnet"
  shared: false
  mtu: 2044
  port_security_enabled: false
  subnets:
    - "{{ openstack_subnet_infiniband }}"

openstack_subnet_infiniband:
  name: "{{ openstack_network_infiniband_name }}"
  cidr: "172.26.0.0/24"
  enable_dhcp: false
  # Without gateway_ip, the os_networks role creates the subnet without a
  # gateway.
  allocation_pool_start: "172.26.0.10"
  allocation_pool_end: "172.26.0.250"
```

Share it with `openstack network rbac create` as above:
`openstack_networks_rbac` fails when it finds more than one matching RBAC
policy.

### Booting instances

A bare metal instance just names the network. Nova creates its port, and
Ironic binds it as `baremetal` with the client ID from the node's Ironic
port:

```sh
openstack server create --flavor baremetal --image <image> \
  --network <Ethernet network> --network infiniband-100 <name>
```

A virtualised instance needs a `direct` port, created first: with
`--network`, Nova creates a `normal` port, which nothing binds on
`ibphysnet`, and the instance fails to boot. `mlnx_infiniband` derives the
port's client ID from its MAC address.

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

## A deployment check, as a hook

After `kayobe overcloud service deploy`, check that exactly one controller
runs OpenSM and every instance is healthy.
`etc/kayobe/ansible/deployment/check-alternative-fabric-manager.yml`:

```yaml
---
- name: Check the alternative fabric manager
  hosts: controllers
  gather_facts: false
  tasks:
    - name: Check each instance's health
      ansible.builtin.uri:
        url: "http://{{ internal_net_name | net_ip }}:{{ afm_api_port }}/healthz"

    - name: Find OpenSM's container
      ansible.builtin.command: >-
        {{ container_engine }} ps --filter name=^opensm$
        --format "{{ '{{' }}.Names{{ '}}' }}"
      register: afm_opensm
      changed_when: false
      become: true

    - name: Check that exactly one controller runs OpenSM
      ansible.builtin.assert:
        that: >-
          ansible_play_hosts | map('extract', hostvars, ['afm_opensm', 'stdout'])
          | select('equalto', 'opensm') | list | length == 1
      run_once: true
```

Run it after every deploy, as stackhpc-kayobe-config does with its
deployment playbooks:

```sh
mkdir -p etc/kayobe/hooks/overcloud-service-deploy/post.d
ln -s ../../../ansible/deployment/check-alternative-fabric-manager.yml \
  etc/kayobe/hooks/overcloud-service-deploy/post.d/90-check-alternative-fabric-manager.yml
```

## First deployment

1. Deploy: `kayobe overcloud service deploy`.
2. Record the fabric's switches, on the leader, the controller running the
   `opensm` container. Until then no job completes ("No expected
   switches"). Here and below, set `ENGINE=podman` on Podman hosts:

   ```sh
   ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --confirm-switches
   ```

3. Run the networking-mlnx tempest plugin against the deployment.

## Upgrades and maintenance

- **Upgrade AFM** by changing `afm_tag` and running
  `kayobe overcloud service deploy`. Its `pre.yml` upgrades the schema
  first; each schema change is compatible with the version before. The
  leader steps down when its container is replaced, releasing the lease,
  and another instance takes over at once.
- **Upgrading from a version with `[ha] manual_takeover`**: if you set it
  to true, set it to false and deploy that first. During a rolling upgrade
  an old instance that leads would otherwise wait for a confirmation that
  the new version's CLI can't give outside a freeze. The
  new version ignores the option, with a warning.
- **Galera maintenance**: freeze the cluster first, so that OpenSM stays on
  the leader whatever happens to Galera, and unfreeze after:

  ```sh
  ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
    --config-file /etc/alternative-fabric-manager/afm.conf --freeze
  # ...
  ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
    --config-file /etc/alternative-fabric-manager/afm.conf --unfreeze
  ```

  New requests get 503 while frozen, which networking-mlnx retries.

  If `--unfreeze` reports that the leader hasn't acknowledged it, run
  `--status`: it shows "unfreeze pending" and names the frozen leader's
  host. No instance takes over until that leader's manager acknowledges.
  If its manager is paused or hung, stop its OpenSM, check that no
  `opensm` process runs on that host, and run `--force-unfreeze`; another
  instance then takes over.
- **Diagnostic tools** on the M_Key-protected fabric need the key:
  `KEY=$(${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager
  --config-file /etc/alternative-fabric-manager/afm.conf --show-keys | awk
  '/^m_key/{print $2}')`, then `ibswitches -y $KEY`. Never print the keys.

## Known limitations

See specs/ha.md. In short: a paused node can resume as a stale master
until its watchdog kills it, and Ask (SMInfo DISABLE) isn't sent yet; in
both cases the new leader's fabric checks find the other master, and no job
completes meanwhile.

Nothing serves DHCP on a partitioned InfiniBand network: AFM only puts
ports with a client ID in a partition, so a DHCP agent's port is never in
one, and kolla's DHCP agent has no IPoIB interface driver. Use the config
drive or static addresses.

With many virtual functions on a device, such as 127, `ip a` fails with
"Error: Buffer too small for object." (an
[iproute2 bug](https://bugs.launchpad.net/ubuntu/+source/iproute2/+bug/1849856)).
Create fewer virtual functions.

## Future improvements

- **Podman.** The guide uses Kayobe's `container_engine` throughout, and
  `afm_container_socket` would point AFM at Podman's Docker-compatible
  socket, but neither that socket nor OpenSM's container joining AFM's PID
  namespace has been tested under Podman.
