# Deploying with Kayobe

This guide deploys the alternative fabric manager (AFM) in HA mode on an
OpenStack cloud that Kayobe manages. AFM runs as a custom container on
every controller, through Kayobe's `controller_containers`. The layout
follows [stackhpc-kayobe-config](https://github.com/stackhpc/stackhpc-kayobe-config):

- `pre` and `post` task files beside the container;
- an HAProxy service in `kolla/config/haproxy/services.d`;
- a deployment check, run as a Kayobe hook.

Read [specs/ha.md](../specs/ha.md) for what HA mode guarantees.

When you are done, you have:

- **an AFM container on each controller.** All of them are HAProxy
  backends behind the internal VIP. One of them, the **leader**, runs
  OpenSM under its watchdog. The others are **candidates**: they serve the
  API and stand by to take over.
- **AFM's database in kolla's MariaDB (Galera).** The deployment creates
  it idempotently, with the root credentials from kolla's `passwords.yml`.
- **networking-mlnx's `mlnx_sdn_assist` pointing at the VIP.** Each
  InfiniBand network's ports are then put in its partition, for
  virtualised instances, bare metal nodes, or both (see
  [Virtualised and bare metal](#virtualised-and-bare-metal)).

To create InfiniBand networks and boot instances on them once AFM is
deployed, see [infiniband-networks.md](infiniband-networks.md).

## Contents

- [Requirements](#requirements)
- [Virtualised and bare metal](#virtualised-and-bare-metal)
- [Deploy AFM](#deploy-afm): the procedure every deployment follows
- Optional parts, each with its own procedure:
  - [To enable virtualised instances](#to-enable-virtualised-instances)
  - [To enable bare metal nodes](#to-enable-bare-metal-nodes)
  - [To allow flat InfiniBand networks without Ironic](#to-allow-flat-infiniband-networks-without-ironic)
  - [To create the network with openstack-config](#to-create-the-network-with-openstack-config)
  - [To enable the deployment check hook](#to-enable-the-deployment-check-hook)
  - [To pull images from a private registry](#to-pull-images-from-a-private-registry)
  - [To change the M_Key lease](#to-change-the-m_key-lease)
  - [To enable experimental hardening groups](#to-enable-experimental-hardening-groups)
- [Upgrades and maintenance](#upgrades-and-maintenance)
- [Known limitations](#known-limitations)
- [Future improvements](#future-improvements)

**Commands on a controller.** Every container command in this guide uses
Kayobe's `container_engine`, `docker` or `podman`. The commands you run
by hand on a controller use `${ENGINE:-docker}`: set `ENGINE=podman` on
Podman hosts. AFM's container is named `alternative_fabric_manager`.

## Requirements

- **Every controller has a port on the InfiniBand fabric** that AFM
  manages, since any instance may become leader and run OpenSM there. If
  the controllers have none, use another group's containers, such as
  `infra_vm_containers`, on hosts that do, with the same files.
- **The `ib_umad` module on every controller.** OpenSM uses it to reach
  the fabric. Step 1 of [Deploy AFM](#deploy-afm) loads it.
- **Virtual functions on the compute hosts**, for virtualised instances
  only: see [To enable virtualised instances](#to-enable-virtualised-instances).
- **Docker.** AFM runs OpenSM's container through the Docker API. Podman
  is untested; support for it is a
  [future improvement](#future-improvements).
- **Kayobe's own OpenSM is off**: `opensm_enabled: false`, the default.
  AFM runs OpenSM itself, and two would fight.
- **No other subnet manager** on the fabric, whether managed or embedded
  in a switch.
- **Galera** from kolla-ansible, reached through the internal VIP. Only
  the majority side of a Galera partition can elect a leader (see
  [specs/ha.md](../specs/ha.md), The lease).
- **The AFM image** in a registry the controllers can pull from, built
  from this repository's Dockerfile.
- **OpenSM's image**, `ghcr.io/stackhpc/docker-opensm`, pinned by digest.
  It must have `python3`, which runs the watchdog.
- **M_Key and SM_Key protection.** HA mode refuses to start without the
  `m_key` and `sm_key` hardening groups. Read
  [opensm-hardening.md](opensm-hardening.md) before you enable them on a
  fabric shared with hosts outside OpenStack.
- **The experimental hardening groups are optional.** They run in HA mode
  too, `m_key_per_port` (per-port M_Keys) among them, but are untested
  there on hardware. See
  [To enable experimental hardening groups](#to-enable-experimental-hardening-groups)
  before naming any.

## Virtualised and bare metal

One deployment can run virtualised instances, bare metal nodes, or both.
Configure the parts that apply to the instances your cloud will run. How
their ports get into partitions is in
[infiniband-networks.md](infiniband-networks.md#virtualised-and-bare-metal).

| Part | Virtualised instances | Bare metal nodes | Where |
| --- | --- | --- | --- |
| `mlnx_sdn_assist`, pointing at AFM | Yes | Yes | [Deploy AFM](#deploy-afm), step 9 |
| `ib_umad` on the controllers | Yes | Yes | [Deploy AFM](#deploy-afm), step 1 |
| VLAN networks on `ibphysnet`, for partitioning | Yes | Yes | [Deploy AFM](#deploy-afm), step 9 |
| Virtual functions and `ib_ipoib` on the compute hosts | Yes | No | [To enable virtualised instances](#to-enable-virtualised-instances) |
| kolla's SR-IOV and Mellanox agents | Yes | No | [To enable virtualised instances](#to-enable-virtualised-instances) |
| An IPA image with the Mellanox drivers | No | Yes | [To enable bare metal nodes](#to-enable-bare-metal-nodes) |
| Ironic ports for the InfiniBand interfaces, with a client ID and `ibphysnet` as their physical network | No | Yes | [To enable bare metal nodes](#to-enable-bare-metal-nodes) |

Partitioning needs VLAN networks: all of a flat network's ports share one
partition.

## Deploy AFM

Every deployment follows these steps. Paths are relative to your Kayobe
configuration repository: `etc/kayobe/` is `$KAYOBE_CONFIG_PATH`.

The `afm.conf.j2` examples show each option under its section header,
such as `[opensm]`: set the option in that existing section of the file
from step 6. A second copy of a section header stops AFM starting.

### Step 1: Load the controllers' InfiniBand module

Every controller needs `ib_umad`, since any of them may become leader and
run OpenSM. The in-kernel Mellanox drivers are enough: OFED isn't needed.

1. Write `etc/kayobe/ansible/sriov.yml`:

   ```yaml
   ---
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
   ```

   For virtualised instances, use the longer playbook in
   [To enable virtualised instances](#to-enable-virtualised-instances)
   instead. It contains this play too.

2. Run it. The playbook uses Kayobe's user and virtualenv, so run it after
   `kayobe overcloud host configure` and before
   `kayobe overcloud service deploy`, or as a hook after host configure:

   ```sh
   kayobe playbook run $KAYOBE_CONFIG_PATH/ansible/sriov.yml
   ```

3. **Check** on each controller that the module is loaded:

   ```sh
   lsmod | grep ib_umad
   ```

### Step 2: Store AFM's secrets

AFM needs two secrets: a database password, and an API token that
networking-mlnx sends with each request. Keep them in Kayobe's vaulted
secrets. Then add them to kolla's `passwords.yml` as custom passwords, so
that AFM's tasks and kolla-ansible's templates read them from one place,
beside kolla's own `database_password`.

1. Add them to `etc/kayobe/secrets.yml`, which is vaulted (edit it with
   `ansible-vault edit`). Generate each value, for example with
   `openssl rand -hex 32`:

   ```yaml
   secrets_afm_database_password: "<generated>"
   secrets_afm_api_token: "<generated>"
   ```

2. Add them to kolla's custom passwords, in `etc/kayobe/kolla.yml`:

   ```yaml
   kolla_ansible_extra_custom_passwords:
     afm_database_password: "{{ secrets_afm_database_password }}"
     afm_api_token: "{{ secrets_afm_api_token }}"
   ```

`kayobe overcloud service deploy` (or `kayobe overcloud service
reconfigure`) writes them to `$KOLLA_CONFIG_PATH/passwords.yml`. Step 10
runs it.

### Step 3: Set AFM's variables

Write `etc/kayobe/alternative-fabric-manager.yml`. It is laid out like
Kayobe's own configuration files. Replace `<release>` with an AFM release
and `<digest>` with OpenSM's image digest:

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

### Step 4: Define the container

Add AFM to `etc/kayobe/controllers.yml`:

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
then.

**The stop timeout.** Kayobe's container module sets no stop timeout, so
Docker's default of 10 s applies. AFM's configuration (step 6) declares
it, as `container_stop_timeout = 10`, with a shorter `shutdown_timeout`,
so that a stopped leader steps down and releases the lease before Docker
kills it.

### Step 5: Write the tasks that run before the container

Kayobe runs these tasks on each controller before it starts AFM's
container. They:

1. create AFM's directories;
2. make sure OpenSM's image is on every controller;
3. render AFM's configuration from kolla's passwords;
4. create AFM's database and user, once;
5. bring the database's schema up to date, once.

Write `etc/kayobe/containers/alternative-fabric-manager/pre.yml`:

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

# On every controller, so that a takeover never waits on the registry (see
# specs/ha.md, Timeouts). Only when it's missing, so that a deploy doesn't
# need the registry, nor move a tag under AFM. AFM pulls it too if it's
# missing, with the credentials in its own container: for a private
# registry, mount a Docker config.json into it as well.
- name: Check whether OpenSM's image is present
  ansible.builtin.command:
    argv:
      - "{{ container_engine }}"
      - image
      - inspect
      - "{{ afm_opensm_image }}"
  register: afm_opensm_inspect
  changed_when: false
  failed_when: false
  # Read-only, so that --check can decide whether to pull
  check_mode: false
  become: true

- name: Ensure OpenSM's image is present
  ansible.builtin.command:
    argv:
      - "{{ container_engine }}"
      - pull
      - "{{ afm_opensm_image }}"
  when: afm_opensm_inspect.rc != 0
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

Notes on these tasks:

- **`login_user` falls back to `root`**, kolla's default `database_user`.
- **The database passwords appear in the command's arguments**, so they
  are briefly visible in that host's process list, as with kolla-ansible's
  own database tasks. `no_log` keeps them out of Kayobe's output.

### Step 6: Write AFM's configuration

Kayobe renders this template to `/etc/alternative-fabric-manager/afm.conf`
on each controller. Write
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
# The experimental groups, such as m_key_per_port, only as
# opensm-hardening.md's In HA mode says
hardening = m_key, sm_key, enforcement
# An hour by default: a Galera outage longer than the lease stops every
# OpenSM long enough for a tenant to read the M_Key. 0 makes keys
# permanent, at the cost of recovering a lost database (see
# opensm-hardening.md, The lease); freeze before database maintenance.
key_lease_period = 3600
# HA mode: a job must outlast a takeover (see specs/ha.md, Timeouts):
# 237 s here, or 327 s with m_key_per_port, so 360 covers either
job_timeout = 360
start_timeout = 60

[ha]
ha = true
node = {{ inventory_hostname }}
ha_state_dir = {{ afm_state_dir }}
host_proc = /host/proc
# Kayobe's containers have Docker's default stop timeout
container_stop_timeout = 10
# An instance cut off from Galera fails /healthz once its heartbeat is
# 2 * ha_period + call_timeout old, 7 s, and HAProxy then takes five failed
# checks to drop it, so it gets requests for about 17-19 s, answering 500,
# which networking-mlnx resends.
# If you lower self_stop_timeout (20 s), keep it well above Galera's
# eviction of a lost node, evs.suspect_timeout (5 s) plus a view change,
# during which no node can commit: below it, losing any Galera node stops
# OpenSM.
shutdown_timeout = 8
```

What each part does:

| Section | Purpose |
| --- | --- |
| `[api]` | Each instance listens on its own internal address. HAProxy (step 8) listens on the VIP. `token` is the API token from step 2. |
| `[database]` | AFM's Galera database, through the internal VIP. |
| `[opensm]` | The managed driver, which HA mode requires, starts OpenSM's container (`opensm`) from `image`, inside the PID namespace of `manager_container`. `hardening` names the groups HA mode needs, `m_key` and `sm_key`, plus `enforcement`. Because it is an explicit list, a release that adds a group to `all` adds nothing here. |
| `job_timeout` | A job must outlast a takeover, or networking-mlnx sets its port to `ERROR`. A takeover takes up to 237 s with these settings, or 327 s with `m_key_per_port`, so 360 s covers either. Nova's and Ironic's waits must be longer still (see [To enable virtualised instances](#to-enable-virtualised-instances) and [To enable bare metal nodes](#to-enable-bare-metal-nodes)). |
| `key_lease_period` | How long a port keeps its M_Key protection when no subnet manager holding the key reaches it. See [To change the M_Key lease](#to-change-the-m_key-lease). |
| `[ha]` | Turns HA mode on. `node` names this controller in AFM's database. `container_stop_timeout` matches Docker's default stop timeout of 10 s, and `shutdown_timeout` (8 s) is shorter, so that a stopping leader steps down first. |

**Every instance must have the same `[ha]` timings.** The first instance
stores them in the database, and an instance with different ones refuses
to be a candidate.

**How long a cut-off instance still gets requests.** An instance cut off
from Galera fails `/healthz` once its heartbeat is
2 × `ha_period` + `call_timeout` old, 7 s. HAProxy then takes five failed
checks to drop it. So it gets requests for about 17-19 s, and answers them
with 500, which networking-mlnx resends.

**If you lower `self_stop_timeout`** (20 s), keep it well above Galera's
eviction of a lost node: `evs.suspect_timeout` (5 s) plus a view change,
during which no node can commit. Below that, losing any Galera node stops
OpenSM.

### Step 7: Write the tasks that run after the container

These tasks wait until AFM's API answers on each controller. Then they
apply a configuration change, so that changing `afm.conf.j2` needs
nothing but `kayobe overcloud service deploy`.

AFM reads its configuration only when it starts, and Kayobe replaces the
container only when its definition changes, such as its image or
volumes. So these tasks restart each instance whose configuration
directory has a file newer than the container's start:

1. They read the lease with `--status`, and stop the deploy if the
   cluster is frozen: restarting a frozen leader would stop its OpenSM.
2. They restart the other controllers one at a time, each waiting until
   its API answers again. If one fails, the remaining candidates still
   restart, but the leader doesn't, and the deploy fails.
3. They restart the leader last, as a rolling configuration change needs
   (see [Change AFM's configuration](#change-afms-configuration)).
4. They wait for the takeover: a holder with a higher term.

Write `etc/kayobe/containers/alternative-fabric-manager/post.yml`:

```yaml
---
- name: Wait for AFM's API
  ansible.builtin.uri:
    url: "http://{{ internal_net_name | net_ip }}:{{ afm_api_port }}/healthz"
  register: afm_health
  until: afm_health.status == 200
  retries: 30
  delay: 2
  any_errors_fatal: true

# rc 0: a file in AFM's configuration directory changed after the
# container started, so the instance still runs the old configuration. A
# container that this deploy created or started reads the new one.
# Podman's StartedAt ends in a zone name, which find doesn't accept. Any
# other failure is rc 2, which fails the task.
- name: Check whether AFM's configuration changed since it started
  ansible.builtin.shell:
    cmd: |
      set -o pipefail
      started=$({{ container_engine }} inspect \
        -f '{{ '{{' }}.State.StartedAt{{ '}}' }}' alternative_fabric_manager \
        | sed 's/ [A-Z]*$//') || exit 2
      newer=$(find {{ afm_config_dir }} -type f -newermt "$started") || exit 2
      [ -n "$newer" ]
    executable: /bin/bash
  register: afm_config_newer
  changed_when: false
  failed_when: afm_config_newer.rc > 1
  check_mode: false
  become: true
  when: afm_restart_on_change | default(true) | bool

# The play holds every overcloud host, but only AFM's hosts run these tasks
- name: Note which instances need a restart
  ansible.builtin.set_fact:
    afm_restart_hosts: >-
      {{ ansible_play_hosts | map('extract', hostvars)
         | selectattr('afm_config_newer', 'defined')
         | selectattr('afm_config_newer.rc', 'defined')
         | selectattr('afm_config_newer.rc', 'equalto', 0)
         | map(attribute='inventory_hostname') | list }}

- name: Read AFM's lease
  ansible.builtin.command:
    argv:
      - "{{ container_engine }}"
      - exec
      - alternative_fabric_manager
      - alternative-fabric-manager
      - --config-file
      - "{{ afm_config_dir }}/afm.conf"
      - --status
  register: afm_status
  changed_when: false
  check_mode: false
  run_once: true
  become: true
  when: afm_restart_hosts | length > 0

# "holder: <node> (instance ...)", where <node> is [ha] node, which this
# guide sets to inventory_hostname, or "-" while no instance holds the
# lease; and "term: N"
- name: Note AFM's leader and term
  ansible.builtin.set_fact:
    afm_leader: >-
      {{ (afm_status.stdout | default('')
          | regex_findall('^holder: ([^ ]+)', multiline=True)
          | default(['-'], true)) | first }}
    afm_term: >-
      {{ (afm_status.stdout | default('')
          | regex_findall('^term: ([0-9]+)', multiline=True)
          | default(['0'], true)) | first | int }}
  when: afm_restart_hosts | length > 0

- name: Refuse to restart AFM while it is frozen
  ansible.builtin.fail:
    msg: >-
      AFM's configuration changed, but the cluster is frozen, and a
      restart would stop the frozen leader's OpenSM. Deploy again after
      --unfreeze.
  run_once: true
  when:
    - afm_restart_hosts | length > 0
    - "afm_status.stdout is not search('^freeze: none$', multiline=True)"

# One at a time, each waiting until its instance answers again, while the
# leader keeps leading on the old configuration
- name: Restart AFM on the other controllers, one at a time
  ansible.builtin.shell: >-
    {{ container_engine }} restart alternative_fabric_manager
    && timeout 120 sh -c 'until curl -sf
    http://{{ internal_net_name | net_ip }}:{{ afm_api_port }}/healthz;
    do sleep 2; done'
  when:
    - inventory_hostname in afm_restart_hosts
    - inventory_hostname != afm_leader
  throttle: 1
  any_errors_fatal: true
  become: true

# Last: the leader steps down and releases the lease. Once every live node
# has the new configuration's hash, the epoch moves, and an updated
# instance takes over.
- name: Restart AFM on the leader
  ansible.builtin.shell: >-
    {{ container_engine }} restart alternative_fabric_manager
    && timeout 120 sh -c 'until curl -sf
    http://{{ internal_net_name | net_ip }}:{{ afm_api_port }}/healthz;
    do sleep 2; done'
  when:
    - inventory_hostname in afm_restart_hosts
    - inventory_hostname == afm_leader
  any_errors_fatal: true
  become: true

# A takeover can take up to 237 s with this guide's settings, or 327 s
# with m_key_per_port: wait up to 360 s for a holder with a higher term
- name: Wait for the takeover
  ansible.builtin.command:
    argv:
      - "{{ container_engine }}"
      - exec
      - alternative_fabric_manager
      - alternative-fabric-manager
      - --config-file
      - "{{ afm_config_dir }}/afm.conf"
      - --status
  register: afm_after
  until: >-
    afm_after.rc == 0
    and afm_after.stdout is search('^released: no$', multiline=True)
    and ((afm_after.stdout
          | regex_findall('^term: ([0-9]+)', multiline=True)
          | default(['0'], true)) | first | int) > afm_term | int
  retries: 72
  delay: 5
  changed_when: false
  run_once: true
  become: true
  when:
    - afm_restart_hosts | length > 0
    - afm_leader in afm_restart_hosts
```

Notes on these tasks:

- **`docker restart`** stops the instance cleanly first, within Docker's
  default 10 s, which `container_stop_timeout` matches (step 6).
- **They need `curl` and `bash`** on each controller.
- **A rerun is safe.** If a deploy stops part way, the next one restarts
  whichever instances still run the old configuration.
- **Any change to `afm.conf` restarts AFM**, a comment included, and a
  restart of the leader is a failover. Group changes together.
- **Don't change `afm_tag` and the configuration in one deploy.** A new
  image replaces every controller's container at once, not one at a time.
- **`-e afm_restart_on_change=false`** writes the configuration without
  restarting anything, for a change that needs every instance stopped
  (see [Changing the HA timeouts](#change-afms-configuration)).

### Step 8: Add the HAProxy service

Any instance answers requests, so HAProxy balances across all of them.
`/healthz` fails on an instance that is stopping or can't reach the
database, and HAProxy then drops it.

Write `etc/kayobe/kolla/config/haproxy/services.d/alternative-fabric-manager.cfg`.
Kayobe templates it, and then kolla-ansible templates what is inside the
`raw` block:

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

### Step 9: Point Neutron at AFM

These settings apply to virtualised instances and bare metal nodes alike.

1. **Add the InfiniBand physical network**, for VLAN networks, to
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

2. **Set the mechanism drivers**, in the same file. This list replaces
   kolla's defaults, so keep any others the deployment uses, such as
   `baremetal`. ML2 tries them in order when it binds a port, so the order
   matters:

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

   `mlnx_infiniband` must only add client IDs to InfiniBand ports. That
   needs a networking-mlnx that includes "Only add client-id to InfiniBand
   ports".

3. **Point `mlnx_sdn_assist` at AFM's VIP, and give `ibphysnet` its MTU**,
   in `etc/kayobe/kolla/config/neutron/ml2_conf.ini`. Kayobe templates
   this file with its own variables, so it can use the variables from
   `alternative-fabric-manager.yml`, and the token is Kayobe's secret.
   Replace each 1500 with the deployment's real MTU for that network:

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

   2044 is IPoIB's datagram-mode MTU on a 2048-byte InfiniBand MTU. List
   every physical network in `physical_network_mtus`: one left out gets
   2044.

4. **Raise Neutron's MTU cap**, unless `global_physnet_mtu` is already
   2044 or more, in `etc/kayobe/kolla/config/neutron.conf`:

   ```ini
   [DEFAULT]
   global_physnet_mtu = 2044
   ```

Two `[sdn]` settings must stay as they are:

- **Leave `bind_normal_ports` off.** Turning it on lets `mlnx_sdn_assist`
  bind DHCP and router ports on `ibphysnet`. AFM never puts those in a
  partition, so they couldn't reach the instances anyway (see
  [infiniband-networks.md](infiniband-networks.md#limitations)).
- **Leave `retry_count` at its default, -1.** AFM answers 503 to every
  request while the cluster is frozen. With a finite count, a long freeze
  would put new ports in `ERROR`, and a port deletion that runs out of
  retries holds every port with the same GUID in `ERROR` until an operator
  clears it.

Now add the parts your cloud needs: see
[To enable virtualised instances](#to-enable-virtualised-instances) and
[To enable bare metal nodes](#to-enable-bare-metal-nodes). Do that before
step 10, so that one deploy applies everything.

### Step 10: Deploy

1. Run the deploy:

   ```sh
   kayobe overcloud service deploy
   ```

   Kayobe runs kolla-ansible's deploy first, then AFM's `pre.yml`, starts
   the container, and runs `post.yml`, which waits for each instance's
   `/healthz`.

2. **Check each instance's health**, on each controller, using its
   internal address:

   ```sh
   curl -s http://<controller's internal address>:8090/healthz
   ```

   Expected output: `{"status":"ok"}`. Check the VIP the same way, with
   `http://<kolla_internal_fqdn>:8090/healthz`.

3. **Check that exactly one controller runs OpenSM.** On each controller:

   ```sh
   ${ENGINE:-docker} ps --filter name=^opensm$ --format '{{.Names}}'
   ```

   Exactly one controller prints `opensm`. The
   [deployment check hook](#to-enable-the-deployment-check-hook) automates
   steps 2 and 3.

4. **Check the lease**, on any controller:

   ```sh
   ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --status
   ```

   Expected output: `holder: <leader> (instance <UUID>)`, a `term`,
   `released: no` and `freeze: none`.

5. **Check the leader's log** on the controller that runs OpenSM. The
   second `grep` keeps key lines out of your terminal, since OpenSM's log
   holds keys:

   ```sh
   ${ENGINE:-docker} logs alternative_fabric_manager 2>&1 \
     | grep -E "lease|SUBNET UP|Leading|ERROR" | grep -vi "key 0x"
   ```

   Expected: `Took the lease, term N, ...`, `Leading, term N`, and
   `SUBNET UP`.

### Step 11: Wait for the expected switches

AFM completes jobs only while OpenSM sees every switch it expects. On a
new database, the leader records the fabric's switches by itself, once
its fabric checks have seen the same switches for `[opensm]
confirm_checks` checks in a row (a minute by default). Until then no job
completes ("No expected switches yet").

1. Wait about a minute.
2. **Check** the leader's log for the line
   `Recorded the expected switches, seen by N checks in a row: ...`:

   ```sh
   ${ENGINE:-docker} logs alternative_fabric_manager 2>&1 \
     | grep "expected switches"
   ```

To record them at once instead, run `--confirm-fabric` on the leader, the
controller that runs the `opensm` container:

```sh
${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf --confirm-fabric
```

Later, run it only after removing or replacing a switch (see
[Record a removed or replaced switch](#record-a-removed-or-replaced-switch)).
It only records the switches, whatever the hardening groups.

### Step 12: Test it

1. Run the networking-mlnx tempest plugin against the deployment.
2. Create an InfiniBand network and boot an instance on it, as in
   [infiniband-networks.md](infiniband-networks.md).

## To enable virtualised instances

Virtualised instances need virtual functions (VFs) and `ib_ipoib` on the
compute hosts, kolla's SR-IOV and Mellanox agents, and Nova settings. The
`sriov` role installs NVIDIA's firmware tools, to enable SR-IOV in the
adapter's firmware; OFED isn't needed.

1. **Choose the virtual functions to create, per device**, in
   `etc/kayobe/inventory/group_vars/compute/sriov`:

   ```yaml
   ---
   sriov_devices:
     # InfiniBand
     - name: ibs4f0
       numvfs: 16
   ```

   Create only as many as you need: many virtual functions break `ip a`
   (see [Known limitations](#known-limitations)).

2. **Add the compute hosts to a `sriov` group**, in
   `etc/kayobe/inventory/groups`. The playbook's third play runs on it:

   ```ini
   [sriov:children]
   compute
   ```

3. **Require stackhpc.linux 1.3.0 or later**, which has the `sriov` role,
   in `etc/kayobe/ansible/requirements.yml`:

   ```yaml
   collections:
     - name: stackhpc.linux
       version: 1.3.0
   ```

   Install it:

   ```sh
   kayobe control host bootstrap
   ```

4. **Replace `etc/kayobe/ansible/sriov.yml`** with this playbook. It loads
   `ib_ipoib` on the compute hosts and `ib_umad` on the controllers,
   creates the virtual functions, and reboots the hosts that need it:

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

5. **Run it**, after `kayobe overcloud host configure` and before
   `kayobe overcloud service deploy`, or as a hook after host configure:

   ```sh
   kayobe playbook run $KAYOBE_CONFIG_PATH/ansible/sriov.yml
   ```

   > [!WARNING]
   > The last play reboots every host whose virtual functions needed it,
   > all at once. On a running cloud, limit it to a few hosts at a time
   > (`--limit`), or skip it (`--skip-tags reboot`) and reboot them
   > yourself.

6. **Check** on a compute host that the device has its virtual functions
   and the module is loaded:

   ```sh
   cat /sys/class/net/ibs4f0/device/sriov_numvfs
   lsmod | grep ib_ipoib
   ```

   The first prints the `numvfs` you set, such as `16`.

7. **Enable kolla-ansible's SR-IOV and Mellanox agents**, in
   `etc/kayobe/kolla.yml`:

   ```yaml
   kolla_enable_neutron_sriov: true
   kolla_enable_neutron_mlnx: true
   ```

8. **Map the InfiniBand physical network to the compute hosts' device**,
   in `etc/kayobe/kolla/globals.yml`. kolla also builds Nova's PCI
   passthrough list from the SR-IOV mapping:

   ```yaml
   neutron_sriov_physnet_mappings:
     ibphysnet: ibs4f0
   neutron_mlnx_physnet_mappings:
     ibphysnet: ibs4f0
   ```

9. **Configure Nova**, in `etc/kayobe/kolla/config/nova.conf`. Nova must
   schedule instances only to hosts with a free virtual function. It must
   also wait longer for an instance's ports to plug than a port's job can
   take: a takeover can hold a job for up to the job timeout, 360 s.

   ```ini
   [DEFAULT]
   vif_plugging_timeout = 420

   [filter_scheduler]
   # Nova's default filters, and PciPassthroughFilter
   enabled_filters = ComputeFilter,ComputeCapabilitiesFilter,ImagePropertiesFilter,ServerGroupAntiAffinityFilter,ServerGroupAffinityFilter,PciPassthroughFilter
   ```

   Check `enabled_filters` against your Nova release's default and any
   filters the deployment already adds.

10. **Deploy**, as in [Deploy AFM](#deploy-afm), step 10:

    ```sh
    kayobe overcloud service deploy
    ```

11. **Check** that the SR-IOV and Mellanox agents are up:

    ```sh
    openstack network agent list
    ```

    Then boot an instance, as in
    [infiniband-networks.md](infiniband-networks.md).

<a id="for-bare-metal-nodes"></a>

## To enable bare metal nodes

Bare metal nodes need an IPA image with the Mellanox drivers, and an
Ironic port for each InfiniBand interface, with a client ID and
`ibphysnet` as its physical network.

1. **Configure Ironic's port handling.** Ironic needs the `neutron`
   network interface, and must fail a deployment whose port fails to bind:
   keep `[neutron] fail_on_port_binding_failure = True`, the default.
   Ironic's wait for ports, `[agent] neutron_agent_max_attempts` ×
   `neutron_agent_status_retry_interval`, is 1000 s by default, which is
   longer than the job timeout of 360 s. Keep it longer.

2. **Give the inspection image the Mellanox drivers.** Inspection records
   each InfiniBand port's client ID in the Ironic port's `extra`
   `client-id`, if the inspection image can see the port. When Kayobe
   builds IPA (`ipa_build_images: true`), add the drivers in
   `etc/kayobe/ipa.yml`:

   ```yaml
   ipa_build_dib_elements_extra:
     # Installs packages and loads the drivers that Mellanox adapters need.
     - "mellanox"
   ```

   With a prebuilt IPA image, make sure it has the drivers.

3. **Make inspection create a port for the InfiniBand interface.**
   Kayobe's default only adds the interface that PXE booted. In
   `etc/kayobe/inspector.yml`:

   ```yaml
   # Add a port for every interface, including InfiniBand ones, which don't
   # PXE boot and have no IP address during inspection.
   inspector_add_ports: "all"
   ```

4. **Give each InfiniBand port `ibphysnet` as its physical network.**
   Without one, Ironic lets any network use the port, so an Ethernet
   network's port could land on it. How to set it depends on the Kayobe
   release:

   - **Kayobe 19 (2025.2) and later** use Ironic's built-in inspection.
     Its `physical-network` hook maps by IP address, which an InfiniBand
     interface doesn't have during inspection. So after inspection, set it
     on each InfiniBand port, the ports whose `extra` has a `client-id`:

     ```sh
     openstack baremetal port set --physical-network ibphysnet <port UUID>
     ```

   - **Kayobe 18 (2025.1) and earlier**, with standalone ironic-inspector
     and
     [stackhpc-inspector-plugins](https://github.com/stackhpc/stackhpc-inspector-plugins),
     can have the `ib_physnet` hook set it. In `etc/kayobe/inspector.yml`:

     ```yaml
     inspector_processing_hooks_extra:
       # Assigns physical_network to InfiniBand ports.
       - ib_physnet
       # For Ethernet ports with genericswitch: the switch's name in
       # local_link_connection, and a physical network from it.
       - system_name_llc
       - system_name_physnet
     ```

     and in `etc/kayobe/kolla/config/ironic-inspector.conf`:

     ```ini
     [port_physnet]
     ib_physnet = ibphysnet
     ```

5. **Deploy**, as in [Deploy AFM](#deploy-afm), step 10, then inspect the
   nodes.

6. **Check** each node's InfiniBand port: its `extra` has a `client-id`,
   and its physical network is `ibphysnet`:

   ```sh
   openstack baremetal port show <port UUID> -f value -c extra -c physical_network
   ```

The resulting port, and how to enroll one by hand, are in
[infiniband-networks.md](infiniband-networks.md#bare-metal-ports).

<a id="for-both"></a>

## To allow flat InfiniBand networks without Ironic

Without Ironic, kolla only allows flat networks on the physical networks
it knows of. All of a flat network's ports share one partition.

1. Add `ibphysnet` to the flat networks, in
   `etc/kayobe/kolla/config/neutron/ml2_conf.ini`, beside step 9's
   settings:

   ```ini
   [ml2_type_flat]
   flat_networks = physnet1,ibphysnet
   ```

2. Deploy:

   ```sh
   kayobe overcloud service deploy
   ```

To turn it off, remove `ibphysnet` from `flat_networks` and deploy again.

<a id="creating-the-network-with-openstack-config"></a>

## To create the network with openstack-config

Networks are usually created as in
[infiniband-networks.md](infiniband-networks.md#creating-networks). To
create a flat InfiniBand network with
[openstack-config](https://github.com/stackhpc/openstack-config) instead:

1. **Without Ironic, allow flat networks on `ibphysnet` first:** see
   [To allow flat InfiniBand networks without Ironic](#to-allow-flat-infiniband-networks-without-ironic).

2. **Add the network to `openstack_networks`**, in openstack-config's
   `etc/openstack-config/openstack-config.yml`:

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

3. **Apply openstack-config** as usual.

4. **Share the network with `openstack network rbac create`**, not with
   `openstack_networks_rbac`, which fails when it finds more than one
   matching RBAC policy:

   ```sh
   openstack network rbac create --type network --action access_as_shared \
     --target-project <project ID> infiniband
   ```

5. **Check** the network:

   ```sh
   openstack network show infiniband -c provider:network_type \
     -c provider:physical_network -c mtu
   ```

   Expected: `flat`, `ibphysnet` and `2044`.

## To enable the deployment check hook

The check runs after every `kayobe overcloud service deploy`, as
stackhpc-kayobe-config does with its deployment playbooks. It checks that
every instance is healthy, and that exactly one controller runs OpenSM.

1. Write `etc/kayobe/ansible/deployment/check-alternative-fabric-manager.yml`:

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

2. Link it as a hook, from the directory that holds `etc/`:

   ```sh
   mkdir -p etc/kayobe/hooks/overcloud-service-deploy/post.d
   ln -s ../../../ansible/deployment/check-alternative-fabric-manager.yml \
     etc/kayobe/hooks/overcloud-service-deploy/post.d/90-check-alternative-fabric-manager.yml
   ```

3. **Check** it by running it once by hand:

   ```sh
   kayobe playbook run $KAYOBE_CONFIG_PATH/ansible/deployment/check-alternative-fabric-manager.yml
   ```

   The play ends with `All assertions passed` and no failed hosts.

The hook runs with the deploy's `--limit`, if you give one. Limited to one
controller, it fails on any controller that isn't the leader, since only
the leader runs OpenSM. The rolling procedures below skip it, and run it
by hand at the end.

To turn it off, remove the link:

```sh
rm etc/kayobe/hooks/overcloud-service-deploy/post.d/90-check-alternative-fabric-manager.yml
```

## To pull images from a private registry

Three pulls need the registry's credentials:

| Image | Who pulls it | Credentials it uses |
| --- | --- | --- |
| AFM's image | Kayobe, before it starts the container | `kolla_docker_registry_username` and `kolla_docker_registry_password`, for `kolla_docker_registry` |
| OpenSM's image, at deploy | `pre.yml`'s `docker pull`, as root | root's Docker credentials on each controller |
| OpenSM's image, at run time | AFM, when the image is missing on its controller | The credentials inside AFM's container |

AFM's own pull uses the Docker SDK, which runs as root in the container
and reads `/root/.docker/config.json`. So:

1. **Log in as root on each controller.** This writes
   `/root/.docker/config.json`:

   ```sh
   sudo docker login <registry>
   ```

   If the host's Docker uses a credential helper (`credsStore`), the file
   holds no credentials, and AFM's container has no helper to ask. Write
   the credentials into the file instead.

2. **Mount the file into AFM's container.** Add a volume to
   `controller_containers` in `etc/kayobe/controllers.yml`:

   ```yaml
       volumes:
         # ...the volumes from step 4 of Deploy AFM...
         # Registry credentials, for AFM's pulls of OpenSM's image
         - "/root/.docker/config.json:/root/.docker/config.json:ro"
   ```

3. **Roll the change out** one controller at a time, as in
   [Upgrade AFM](#upgrade-afm): a new volume replaces the container.

4. **Check** that the file is in the container:

   ```sh
   ${ENGINE:-docker} exec alternative_fabric_manager test -r /root/.docker/config.json && echo ok
   ```

Three things to know:

- **Log in before deploying.** If `/root/.docker/config.json` doesn't
  exist when Kayobe starts the container, Docker creates a directory at
  that path instead.
- **After logging in again, restart AFM** on that controller. `docker
  login` replaces the file rather than rewriting it, so the container
  keeps seeing the old one, and the Docker SDK reads credentials only
  when AFM starts anyway.
- **AFM's own image** is pulled by Kayobe with the
  `kolla_docker_registry` credentials only when it is on
  `kolla_docker_registry`. From any other registry, Kayobe's pull uses
  the Kayobe user's Docker credentials, not root's.

If AFM can't pull, its log says `Can't pull OpenSM's image ..., retrying
in N s: <reason>`. A controller without OpenSM's image can't become
leader until the pull succeeds.

## To change the M_Key lease

`[opensm] key_lease_period` sets the lease of every key AFM puts on ports.
If no subnet manager holding the key reaches a port for that long, the
port drops its protection, and a tenant can read the M_Key.

- **An hour (3600 s) is the default.** A Galera outage longer than the
  lease stops every OpenSM long enough for a tenant to read the M_Key:
  every OpenSM stops while Galera has no Primary component. So freeze the
  cluster before database maintenance (see
  [Galera maintenance](#galera-maintenance)).
- **0 makes keys permanent**, at the cost of recovering a lost database:
  a port whose key is lost stays locked until it's power-cycled or reset
  by firmware tools (see [opensm-hardening.md](opensm-hardening.md), The
  lease, and Permanent keys).

> [!WARNING]
> The lease changes what OpenSM enforces. On a fabric others share, agree
> it with them first.

To change it:

1. **Back up the database**, which holds the fabric's keys:

   ```sh
   kayobe overcloud database backup
   ```

2. **Set it** in `afm.conf.j2`:

   ```ini
   [opensm]
   # Seconds, up to 65535; 3600 by default. 0: permanent keys.
   key_lease_period = 3600
   ```

3. **Roll it out** as in
   [Change AFM's configuration](#change-afms-configuration). The lease is
   part of the configuration hash, so the leader keeps the old lease until
   the epoch moves.

4. **Check**: `--config-epoch` shows every node's hash allowed. A lease no
   longer than a takeover's longest makes every instance warn
   `[opensm] key_lease_period is N s, no longer than a takeover's
   longest, ...`: any tenant can read the M_Key during an unclean
   takeover.

**Upgrading from a release with permanent keys by default.** A
deployment that doesn't set `key_lease_period` gets the hour's lease at
the next start, once the configuration epoch moves. On a fabric others
share, agree it, or set `key_lease_period = 0` in the same change to keep
permanent keys. This guide's `afm.conf.j2` sets it explicitly.

## To enable experimental hardening groups

The experimental groups are `m_key_per_port`, `allowed_sm`, `class_keys`
and `sa_rate`. They are untested on hardware, in HA mode as in single
node, and none is in `all`. Read
[opensm-hardening.md](opensm-hardening.md#experimental-hardening) and its
[In HA mode](opensm-hardening.md#in-ha-mode) section before naming any.

> [!WARNING]
> **Agree the hardening with whoever shares the fabric** before naming
> or removing any group: each changes what OpenSM enforces.
>
> **Naming `m_key_per_port` turns per-port M_Keys on**, and removing it
> turns them off. The leader arms the turn-on by itself once its
> preconditions hold, and restarts OpenSM, re-keying every port. Never
> name it as a routine step.

1. **Upgrade every node to a release with configuration epochs first**,
   with the configuration unchanged. A node naming an experimental group
   isn't a candidate while a node of a release without configuration
   epochs is live. **Check**: `--config-epoch` (below) lists a hash for
   every node, not `(none recorded)`.

2. **Prepare.** On a fabric you control, back up the database
   (`kayobe overcloud database backup`) and keep a non-zero
   `key_lease_period`. Run the group's own "Before enabling" checklist on
   a single node first, then the "Before enabling in HA mode" checklist,
   both in [opensm-hardening.md](opensm-hardening.md).

3. **Name the group** in `afm.conf.j2`, after the groups HA mode needs.
   For example:

   ```ini
   [opensm]
   hardening = m_key, sm_key, enforcement, class_keys
   ```

   Change it here, not by hand on the controllers, or the next deploy
   removes it.

4. **Roll it out** as in
   [Change AFM's configuration](#change-afms-configuration). The new
   configuration takes effect when the epoch moves and a node with it
   takes over, which restarts OpenSM.

5. **Check**, on the leader, that the fabric check passes ("The fabric is
   complete"), and that a port bound to a network goes `ACTIVE`. For
   `m_key_per_port`, also run:

   ```sh
   ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --port-key-states
   ```

   It shows the target and what keeps the leader from arming the turn-on.
   To apply the target, at most once an hour, the leader steps down and
   releases the lease ("Stepping down: the per-port M_Key target is on,
   ..."), and the takeover's start turns them on.

**With `m_key_per_port`**, a takeover's start takes 1.5 × `start_timeout`
longer, so a takeover takes up to 327 s rather than 237 s. This guide's
`job_timeout = 360` covers that, and stays below Nova's
`vif_plugging_timeout` of 420 s.

**To turn a group off**, follow its own section in
[opensm-hardening.md](opensm-hardening.md) first: `m_key_per_port` and
`class_keys` leave keys on ports, and each has a "Turning ... off"
procedure. Then remove the group and roll the change out the same way.

## Upgrades and maintenance

Each procedure runs commands on a controller as in
[Deploy AFM](#deploy-afm). To find the leader at any time, run
`--status` on any controller and read the `holder:` line:

```sh
${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf --status
```

### Upgrade AFM

Change `afm_tag`, then deploy. `pre.yml` upgrades the schema first, and
each schema change is compatible with the version before. A plain
`kayobe overcloud service deploy` replaces every controller's container.
The procedure below follows [specs/ha.md](../specs/ha.md), Upgrades: it
upgrades the candidates one at a time, and the leader last.

> [!WARNING]
> **Upgrade at a quiet time.** A schema change that rebuilds a table,
> such as widening the jobs table's counters, blocks every commit in the
> Galera cluster while it runs. If it took longer than
> `self_stop_timeout`, the leader would stop OpenSM until it ends.

1. **Read the release notes.** A release that adds a hardening group to
   `all` changes nothing here, since this guide names its groups
   explicitly. A release that changes what the configuration hash covers
   rolls out like a configuration change: the epoch moves once every node
   runs it.

2. **Back up the database**, which holds the fabric's keys. Keep the
   backup as secret as the keys:

   ```sh
   kayobe overcloud database backup
   ```

3. **Set the new tag** in `etc/kayobe/alternative-fabric-manager.yml`:

   ```yaml
   afm_tag: "<new release>"
   ```

4. **Find the leader** with `--status`, as above.

5. **Upgrade each candidate, one at a time.** For each controller that
   isn't the leader:

   ```sh
   kayobe overcloud service deploy --limit <controller> \
     --skip-hooks check-alternative-fabric-manager
   ```

   `--limit` restricts AFM's container deploy to that controller.
   `--skip-hooks` skips the deployment check hook, which would fail on a
   controller that doesn't run OpenSM. The first run upgrades the schema.
   `post.yml` waits for the new instance's `/healthz`.

6. **Upgrade the leader last**, the same way. The leader steps down when
   its container is replaced, releasing the lease, and another instance
   takes over at once. Each release of the lease restarts OpenSM on
   another node, so its SA state is rebuilt.

7. **Check.** `--status` names a holder, with a higher term, and the
   new leader's log shows `Took the lease` and `Leading, term N` (see
   [Deploy AFM](#deploy-afm), step 10). Then run the deployment check
   across every controller:

   ```sh
   kayobe playbook run $KAYOBE_CONFIG_PATH/ansible/deployment/check-alternative-fabric-manager.yml
   ```

### Upgrade from a version with `[ha] manual_takeover`

If you set `manual_takeover` to true, set it to false and deploy that
first, before the upgrade. During a rolling upgrade, an old instance that
leads would otherwise wait for a confirmation that the new version's CLI
can't give outside a freeze. The new version ignores the option, with a
warning.

### Change AFM's configuration

Edit `afm.conf.j2` and deploy. AFM reads its configuration only when it
starts, so the deploy's `post.yml` (step 7) restarts each instance whose
configuration changed: one controller at a time, with the leader last.

**Configuration epochs keep this safe.** Each node's configuration has a
hash, covering everything that decides the partitions and how OpenSM
protects the fabric. The **epoch**, stored in Galera, lists the hashes
allowed to lead. A restarted node whose hash isn't allowed isn't a
candidate, while the leader keeps leading on the old configuration. Once
every live node has the same hash, the epoch moves by itself, and an
updated node takes over. See [specs/ha.md](../specs/ha.md), Configuration
epochs.

Some changes need no epoch: node-specific options, such as paths, and
the rolling options (`sweep_interval`, the routing options and the
logging options), which move no protection. Roll those out the same way.

On a deployment that names `m_key_per_port`, the OpenSM start that
follows a takeover also applies any pending per-port M_Key target, which
re-keys every port (see [opensm-hardening.md](opensm-hardening.md),
Per-port M_Keys).

1. **Back up the database:**

   ```sh
   kayobe overcloud database backup
   ```

2. **Edit `afm.conf.j2`, and deploy:**

   ```sh
   kayobe overcloud service deploy
   ```

   `pre.yml` writes the new `afm.conf` to every controller. `post.yml`
   then restarts each controller's instance in turn, waiting for its API
   to answer before the next, and the leader's last, then waits for the
   takeover. For a change the hash
   covers, each restarted candidate is `NOT allowed` until the epoch
   moves, and logs an alarm once per epoch. That is expected. The leader
   steps down when it restarts, and releases the lease. Once every live
   node has the new hash, the epoch moves, and an updated node takes
   over.

   If the cluster is frozen, the deploy stops before restarting
   anything: deploy again after `--unfreeze`.

3. **Check** that the epoch number moved, and that every node's hash is
   `allowed`:

   ```sh
   ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --config-epoch
   ```

   The output lists the epoch, `This configuration: <hash> ...`, and one
   `Node <name>: <hash> allowed` or `NOT allowed` line per node.
   `--status` names the new holder.

To restart an instance by hand instead, for example on a controller that
`--limit` left out, run `${ENGINE:-docker} restart
alternative_fabric_manager` there, candidates first and the leader last,
or deploy again: the next deploy restarts whichever instances still run
the old configuration.

A node that comes back with the old configuration isn't allowed, and,
being live, stops the epoch moving back.

**When `--allow-config` is needed.** The epoch moves by itself, so a
rolling change needs no command. Run `--allow-config` on a controller
whose configuration you want allowed:

- to move to a configuration before every node has it;
- to allow two configurations at once, by passing the other one's hash,
  as `--config-epoch` prints it.

```sh
${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf --allow-config [<hash>]
```

It prints `Recorded configuration epoch N, allowing <hashes>`.

**Changing the HA timeouts.** The `[ha]` timings are cluster settings,
covered by the hash. An epoch moves them by itself only in the safe
direction: `lease_timeout` and `watchdog_timeout` not shorter, and
`self_stop_timeout`, `ha_period` and `call_timeout` not longer. A change
in the unsafe direction needs every node stopped cleanly, so that no node
may still run OpenSM under the old settings, and raises an alarm
meanwhile. The fabric has no subnet manager while every node is stopped.

1. Edit `afm.conf.j2`, and write it to every controller without
   restarting AFM:

   ```sh
   kayobe overcloud service deploy -e afm_restart_on_change=false
   ```

2. On every controller, stop AFM cleanly:

   ```sh
   ${ENGINE:-docker} stop alternative_fabric_manager
   ```

3. On every controller, start it again:

   ```sh
   ${ENGINE:-docker} start alternative_fabric_manager
   ```

4. Check with `--config-epoch` and `--status`, as above.

If the leader dies uncleanly while the nodes with the new settings wait,
nothing can take over. The alarm and `--allow-config` say what to do: run
one node with the old settings until it leads, which clears its entry,
then roll the change again.

### Galera maintenance

Freeze the cluster before Galera maintenance, so that OpenSM stays on the
leader whatever happens to Galera. New requests get 503 while frozen,
which networking-mlnx retries.

1. **Freeze**, on any controller:

   ```sh
   ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --freeze
   ```

   Expected output: `Frozen on <leader>`.

2. **Do the maintenance.**

3. **Unfreeze**:

   ```sh
   ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --unfreeze
   ```

   Expected output: `Unfrozen`.

**If `--unfreeze` reports that the leader hasn't acknowledged it**, run
`--status`. It shows "unfreeze pending", and names the frozen leader's
host. No instance takes over until that leader's manager acknowledges. If
its manager is paused or hung:

1. stop its OpenSM;
2. check that no `opensm` process runs on that host;
3. run `--force-unfreeze`:

   ```sh
   ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --force-unfreeze
   ```

Another instance then takes over. Nothing checks steps 1 and 2 for you.

### Retire a controller

1. **Unfreeze first**, if the cluster is frozen.
2. **Stop AFM's container on it cleanly**, before taking it out of
   service:

   ```sh
   ${ENGINE:-docker} stop alternative_fabric_manager
   ```

   It marks itself stopped, so the others never count it as a candidate.
   If it leads, it releases the lease, and another instance takes over at
   once. If it couldn't verify that its OpenSM stopped, or couldn't reach
   the database, the takeover waits for the lease to expire instead.
3. **Remove the host from the `controllers` group.** The next deploy then
   drops it from HAProxy's backends.
4. **Optionally, remove its node row.** The row stays in AFM's database,
   and its ports stay known subnet manager ports: with `allowed_sm`, they
   stay in the `allowed_sm_guids` list. `--forget-node` removes the row,
   so that its ports leave that list at the next takeover. It is refused
   while the node may run OpenSM, or its manager is live. Don't delete the
   row by hand.

   ```sh
   ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --forget-node <controller>
   ```

   Its port GUIDs stay among the known subnet manager ports, since its
   files still hold the keys. `--drop-sm-port <guid>` drops one once its
   adapter is no longer in a host with the keys.

### After restoring Neutron's database

Run `--forget-revisions` after restoring Neutron's database from a
backup. AFM refuses a request that runs late, after a later one for the
same object, by Neutron's revision numbers, and keeps deleted objects'
revisions for 7 days (see [specs/ha.md](../specs/ha.md), Late requests).
A restore takes revisions back, and brings deleted IDs back, so their
requests would be refused until then.

```sh
${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf --forget-revisions
```

Expected output: `Forgot N deleted objects and every recorded revision`.

### Record a removed or replaced switch

The leader adds new switches to the expected ones by itself, but doesn't
drop a removed or replaced switch while its links may be up. Meanwhile
the switch counts as missing: grants it can't affect still complete, and
other jobs wait (see the README's
[Expected switches](../README.md#expected-switches)). `--status` shows the
missing switches, and the jobs, tainted pkeys and tainted hosts they hold.

1. **Check which switches are missing,** with `--status` on any
   controller. The next step drops every missing switch, so make sure
   they are all ones you removed.
2. **Unplug or wipe each of them.** A hung switch can keep forwarding
   with its old partitions.
3. **On the leader, record the switches OpenSM sees now,** and reset what
   ports may still hold stale, which untaints their pkeys and hosts:

   ```sh
   ${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
     --config-file /etc/alternative-fabric-manager/afm.conf --confirm-fabric --reset-held
   ```

   It prints the switches it kept and those it dropped. It records
   only the switches, whatever the hardening groups. If it says the
   latest partitions haven't been swept yet, the switches are recorded:
   run `--reset-held` on its own, on any controller, once the leader's
   sweep has finished. The waiting jobs then complete after the leader's
   next sweep, unless they reached `job_timeout` meanwhile and failed.
   With per-port M_Keys, the removed switch's adapter ports also need
   `--release-port <guid> ...`, which takes every GUID in one run, or
   their jobs wait (see [specs/zero-touch.md](../specs/zero-touch.md),
   What stays manual).

### Use diagnostic tools

With M_Key protection on, SMP tools, such as `ibswitches`, `smpquery` and
`ibportstate`, need the M_Key. Read it into a variable, and never print
it:

```sh
KEY=$(${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf --show-keys \
  | awk '$1 == "m_key" {print $2}')
ibswitches -y $KEY
```

Match `m_key` exactly: `--show-keys` also prints `m_key_base`.

With per-port M_Keys, each port has its own key, which opens only that
port. Read it the same way:

```sh
KEY=$(${ENGINE:-docker} exec alternative_fabric_manager alternative-fabric-manager \
  --config-file /etc/alternative-fabric-manager/afm.conf --port-m-key <guid>)
```

See [opensm-hardening.md](opensm-hardening.md), Per-port M_Keys.

## Known limitations

See [specs/ha.md](../specs/ha.md). In short:

- **A paused node can resume as a stale master** until its watchdog kills
  it.
- **Ask (SMInfo DISABLE) isn't sent yet.**

In both cases, the new leader's fabric checks find the other master, and
no job completes meanwhile.

**Many virtual functions break `ip a`.** With many virtual functions on a
device, such as 127, `ip a` fails with "Error: Buffer too small for
object." (an
[iproute2 bug](https://bugs.launchpad.net/ubuntu/+source/iproute2/+bug/1849856)).
Create fewer virtual functions.

For the limitations of InfiniBand networks themselves, such as no DHCP
inside a partition, see
[infiniband-networks.md](infiniband-networks.md#limitations).

## Future improvements

- **Podman.** The guide uses Kayobe's `container_engine` throughout, and
  `afm_container_socket` would point AFM at Podman's Docker-compatible
  socket. But neither that socket nor OpenSM's container joining AFM's PID
  namespace has been tested under Podman.
