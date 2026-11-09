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
- networking-mlnx's `mlnx_sdn_assist` pointing at the VIP.

## Requirements

- **Every controller has a port on the InfiniBand fabric** that AFM manages:
  any instance may become leader and run OpenSM there. If the controllers
  have none, use another group's containers, such as `infra_vm_containers`,
  on hosts that do, and the same files.
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

`etc/kayobe/alternative-fabric-manager.yml`:

```yaml
# The AFM image, and OpenSM's, which AFM starts
afm_image: "ghcr.io/stackhpc/alternative-fabric-manager"
afm_tag: "<release>"
afm_opensm_image: "ghcr.io/stackhpc/docker-opensm@sha256:<digest>"
# The API port on each controller; HAProxy listens on the same port on the
# internal VIP
afm_api_port: 8090
# networking-mlnx's [sdn] domain
afm_domain: "cloudx"
# The container's name: OpenSM's container joins its PID namespace
afm_container_name: "alternative_fabric_manager"
afm_config_dir: "/etc/alternative-fabric-manager"
afm_state_dir: "/run/alternative-fabric-manager"
# The container API AFM uses to run OpenSM's container: Docker's socket, or,
# with container_engine: podman, Podman's Docker-compatible one,
# /run/podman/podman.sock (untested)
afm_container_socket: >-
  {{ '/run/podman/podman.sock' if container_engine == 'podman'
     else '/var/run/docker.sock' }}
# Where AFM's database is created: a host kolla-ansible manages, which has
# the kolla_toolbox container
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

## networking-mlnx and Nova

`etc/kayobe/kolla/config/neutron/ml2_conf.ini`:

```ini
[ml2]
mechanism_drivers = ovn,mlnx_infiniband,mlnx_sdn_assist

[sdn]
url = http://{{ kolla_internal_fqdn }}:8090/ufmRestV3
domain = cloudx
token = {{ afm_api_token }}
sync_enabled = True
bind_normal_ports = False
physical_networks = ibphysnet
```

kolla-ansible renders `{{ afm_api_token }}` from `passwords.yml`.

A takeover can hold a port's job for up to the job timeout, 300 s, so Nova
must wait longer for its ports to plug. `etc/kayobe/kolla/config/nova.conf`:

```ini
[DEFAULT]
vif_plugging_timeout = 420
```

Ironic's wait for ports needs the same.

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

## Future improvements

- **Podman.** The guide uses Kayobe's `container_engine` throughout, and
  `afm_container_socket` would point AFM at Podman's Docker-compatible
  socket, but neither that socket nor OpenSM's container joining AFM's PID
  namespace has been tested under Podman.
