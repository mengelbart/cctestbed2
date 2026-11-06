# cctestbed2

A testbed for congestion control experiments built from Linux network
namespaces and veth pairs. Each testcase is a YAML file that describes a
schedule of bottleneck settings (delay, rate, buffer, AQM) and the applications
to run on the end hosts. Every veth end is captured with tcpdump.

## Requirements

- Linux and root privileges (namespaces, qdiscs, `modprobe`, `sysctl`)
- Python >= 3.12
- `iproute2` (`ip`, `tc`)
- `ethtool`, used to disable TSO/GSO/GRO on all veths
- `tcpdump`
- Kernel modules `sch_netem`, `sch_tbf`, `sch_htb`, and optionally
  `sch_dualpi2` (plus a `tc` that knows dualpi2) for L4S experiments
- The binaries used by the testcases, e.g. `iperf3` for `example_config.yaml`

Python dependencies:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Topology

```
  ns1                 ns2                     ns3                 ns4
 (host)            (router)                (router)             (host)
 l12-a ---------- l12-b  l23-a ========== l23-b  l34-a ---------- l34-b
10.0.12.1     10.0.12.2  10.0.23.1    10.0.23.2  10.0.34.1    10.0.34.2
```

All links are /24. ns2 and ns3 forward packets, and ns1 and ns4 have routes to
all subnets. The bottleneck is the ns2-ns3 link: `l23-a` in ns2 shapes the
ns1 -> ns4 direction and `l23-b` in ns3 shapes ns4 -> ns1. Both directions
always get the same settings.

The qdisc chain on each bottleneck interface is

```
netem (delay) -> tbf or htb (rate) -> leaf (htb only: bfifo or AQM)
```

## Usage

All commands need root. With a virtualenv, run its interpreter through sudo:

```sh
sudo .venv/bin/python main.py run -t example_config.yaml
```

Subcommands:

| Command | Description |
| --- | --- |
| `run -t FILE [FILE ...]` | Run the given testcase configs in order |
| `run -g GLOB` | Run all testcase configs matching `GLOB` (quote it) |
| `setup` | Create namespaces, veths and routes for manual use |
| `clean` | Remove the namespaces and with them all veths |
| `tc` | Add delay, shaper and leaf qdiscs to the bottleneck after `setup`. Options `--delay`, `--bandwidth`, `--limit`, `--burst`, `--shaper`, `--aqm`, `--aqm-options` match the network step keys below. |
| `clear` | Remove all qdiscs from the bottleneck |

`-o DIR` (default `out/`) sets the base output directory and goes before the
subcommand, e.g. `main.py -o results/ run -g 'configs/*.yaml'`.

`run` validates all configs before running anything and exits with status 2 if
any is invalid. Each testcase sets up a fresh topology and removes it
afterwards. If a testcase fails, the remaining ones still run and `run` exits
with status 1. `vnetns.sh` opens a shell (or kitty terminal) inside a namespace
for manual experiments after `setup`.

## Output

Results go to `<output>/<unix timestamp of the run>/<testcase name>/`, where
the testcase name is the config file name without extension. Config file names
must therefore be unique within a run. Each directory contains

- `config.json`: the parsed testcase and its start time
- `tc.log`: one JSON line per applied network step with wall clock time and
  offset in seconds from the testcase start
- `<app>.stdout.log`, `<app>.stderr.log` for each application
- one pcap per veth end (snaplen 200 bytes): `ns1.pcap`, `ns4.pcap`, and
  `ns2-l12-b.pcap`, `ns2-l23-a.pcap`, `ns3-l23-b.pcap`, `ns3-l34-a.pcap`
- whatever the applications write, as they run with this directory as working
  directory
- `FAILED` with error details if anything went wrong

When run through sudo, the output is chowned to the invoking user.

## Testcase config

```yaml
duration: 12              # seconds, total testcase length
network:                  # steps applied one after another
  - traffic_control: true
    duration: 12          # seconds until the next step
    bandwidth: 1mbit
    delay: 100000         # microseconds
    limit: 12500          # bytes
    burst: 6250           # bytes
applications:
  - name: server
    namespace: ns1
    start_time: 0         # seconds after testcase start
    duration: 12          # seconds, then SIGTERM
    environment: []
    binary: iperf3
    arguments: ['-s']
```

### Top level

| Key | Type | Description |
| --- | --- | --- |
| `duration` | number | Testcase length in seconds. All applications are stopped and the network schedule ends at this time. |
| `network` | list | Network steps, see below |
| `applications` | list | Applications, see below |
| `sysctls` | mapping | Optional per-namespace sysctls, see below |

### Sysctls

Sysctls such as `net.ipv4.tcp_ecn` or `net.ipv4.tcp_congestion_control` are
per namespace. `sysctls` maps a namespace to the settings applied with
`sysctl -w` after the topology is set up and before tcpdump and the
applications start. Values are strings or integers. A setting the kernel
rejects fails the testcase.

```yaml
sysctls:
  ns1:
    net.ipv4.tcp_ecn: 1
    net.ipv4.tcp_congestion_control: prague
  ns4:
    net.ipv4.tcp_ecn: 1
```

### Network steps

Steps are applied in order. Each step lasts `duration` seconds and the next
step starts after that. A step with `traffic_control: false` removes all
qdiscs, so the link runs unshaped.

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `traffic_control` | bool | required | Apply the settings below or remove all qdiscs |
| `duration` | number | required | Seconds until the next step |
| `bandwidth` | string | required if tc | tc rate string, e.g. `10mbit` |
| `delay` | number | required if tc | One-way netem delay in microseconds, applied in each direction |
| `limit` | int | required if tc | Bottleneck buffer in bytes, at least 1514. Used as tbf `limit` or bfifo `limit`. For dualpi2 it becomes `limit // 1514` packets. |
| `burst` | int | required if tc | Shaper burst in bytes, at least 1514 |
| `shaper` | string | `tbf` | `tbf` or `htb` |
| `aqm` | string | none | Leaf qdisc under the htb class, e.g. `dualpi2`. Requires `shaper: htb`. Without it htb uses a bfifo leaf. |
| `aqm_options` | string | none | Extra tc arguments for `aqm`, passed verbatim. Do not set the buffer size here, it is derived from `limit`. |
| `netem_limit` | int | 1000000 | netem queue length in packets. The default keeps netem from becoming a hidden bottleneck. |

### Applications

| Key | Type | Description |
| --- | --- | --- |
| `name` | string | Unique name, used for the log file names |
| `namespace` | string | One of `ns1` to `ns4` |
| `start_time` | number | Start offset in seconds from the testcase start |
| `duration` | number | Runtime in seconds, then the process gets SIGTERM (SIGKILL after 1 s). `start_time + duration` must not exceed the testcase duration. |
| `binary` | string | Executable name or path, looked up in `PATH` |
| `arguments` | list of strings | Command line arguments. Quote numbers in YAML. |
| `environment` | list | Optional extra environment variables as `{name: ..., value: ...}` |
