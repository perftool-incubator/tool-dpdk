# tool-dpdk

DPDK Telemetry collection tool for the [perftool-incubator](https://github.com/perftool-incubator) / [crucible](https://github.com/perftool-incubator/crucible) benchmarking ecosystem.

## Status

**Crucible integration: complete** — registered in `config/repos.json`, activated at `subprojects/tools/dpdk`, end-to-end validation passed.

| Milestone | Status |
|-----------|--------|
| Core collection (ethdev stats, xstats, link_status, link-speed, mempool) | ✅ Complete |
| CDM post-processing (all metrics to OpenSearch) | ✅ Complete |
| Crucible/rickshaw integration (schema-compliant rickshaw.json, workshop.json) | ✅ Complete |
| Multi-directory socket discovery (dpdk, openvswitch, /run, /tmp, XDG) | ✅ Complete |
| Indefinite socket retry (30s cycles until SIGTERM) | ✅ Complete |
| Delta/rate computation (rx-pps, tx-pps, rx-Gbps, tx-Gbps, rx-missed-sec) | ✅ Complete |
| Direction breakout (rx/tx normalization via CDM direction field) | ✅ Complete |
| Per-queue xstat normalization (direction + queue labels) | ✅ Complete |
| PCI address as device label | ✅ Complete |
| Negotiated max_output_len from DPDK handshake | ✅ Complete |
| Additional profiles (testpmd, l3fwd-power, grout) | ❌ Pending |

## Overview

tool-dpdk connects to the **DPDK Telemetry v2 API** — a JSON-over-Unix-socket interface exposed by every DPDK application since DPDK 20.05 — to collect real-time port statistics, extended NIC counters, queue-level metrics, and memory pool utilization during benchmark runs.

Collected data is post-processed into Crucible's **Common Data Model (CDM)** and indexed in OpenSearch for correlation with benchmark results from bench-trafficgen.

### Supported DPDK Applications

| Application | Status | Profile |
|-------------|--------|---------|
| dpdk-testpmd | ✅ Primary target | `default` |
| OVS-DPDK | ✅ Socket discovery supported | `default` |
| l3fwd-power | ⏳ Planned | `l3fwd-power` |
| Grout (rte_graph router) | ⏳ Future | `grout` |
| Any DPDK app with telemetry | ✅ Supported via auto-discovery | `default` |

## Usage with Crucible

```bash
crucible run bench-trafficgen \
    --tool dpdk:interval=1,profile=default \
    --endpoint remotehosts,host:my-test-host \
    ...
```

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--interval` | `1` | Collection interval in seconds |
| `--file-prefix` | (auto) | DPDK `--file-prefix`; auto-scans multiple directories if omitted |
| `--socket-path` | (derived) | Explicit path to `dpdk_telemetry.v2` socket |
| `--profile` | `default` | Application profile name |
| `--connect-timeout` | `30` | Seconds per retry cycle when searching for the telemetry socket. The collector retries indefinitely until stopped via SIGTERM. |

## How It Works

Rickshaw sets CWD to the tool output directory before invoking start/stop scripts. Parameters arrive as `--key value` arguments parsed with `getopt`.

1. **dpdk-start** — Parses `--interval`, `--profile`, `--file-prefix`, `--socket-path`, `--connect-timeout` via `getopt`; launches `dpdk-collect` in the background; saves PID to `dpdk-collect-pid.txt`
2. **dpdk-collect** — Retries socket discovery indefinitely in 30s cycles until SIGTERM (accommodating the gap between rickshaw's `start-tools` and `server-start` phases). Discovers ports, mempools, and per-port metadata (PCI address, driver, MAC, queues). Polls endpoints at the configured interval, writes timestamped JSONL output. Uses negotiated `max_output_len` from the DPDK handshake. Logs the discovered socket path on connection.
3. **dpdk-stop** — Reads PID, sends SIGTERM (10s grace, SIGKILL fallback), compresses output with `xz --threads=0`
4. **dpdk-post-process** — Defaults to CWD when called without arguments (matching rickshaw convention). Resolves `TOOLBOX_HOME` for the metrics library, reads compressed JSONL, computes delta rates (pps, Gbps, drops/sec) between consecutive samples, normalizes xstats with `direction` and `queue` labels, adds PCI address via `device` label, emits CDM metrics via `toolbox.metrics.log_sample()` / `finish_samples()`, writes `post-process-data.json`

## Socket Discovery

The telemetry client recursively searches multiple directories for DPDK sockets, supporting both standard and OVS-DPDK deployments:

| Search Path | Covers |
|-------------|--------|
| `/var/run/dpdk/**/dpdk_telemetry.v2` | Standard DPDK apps (testpmd, l3fwd) |
| `/var/run/openvswitch/**/dpdk_telemetry.v2` | OVS-DPDK |
| `/var/run/openvswitch/.dpdk/**/dpdk_telemetry.v2` | OVS-DPDK with .dpdk prefix directory |
| `/run/dpdk/`, `/run/openvswitch/`, `/run/openvswitch/.dpdk/` | Alternative runtime dirs |
| `/tmp/dpdk/` | Non-standard deployments |
| `$XDG_RUNTIME_DIR/dpdk/` | Non-root user sockets |

Socket discovery uses **recursive globbing** so the telemetry socket is found regardless of how many subdirectory levels deep it is placed.

### OVS File-Prefix Auto-Detection

When neither `--socket-path` nor `--file-prefix` is provided, the collector automatically queries OVS for its DPDK EAL arguments:

```
ovs-vsctl get Open_vSwitch . other_config:dpdk-extra
```

If `--file-prefix` is found in `dpdk-extra`, it is used to locate the telemetry socket under the matching subdirectory. If `--no-telemetry` is detected, a warning is emitted.

### When to Use Explicit Paths

- **`--socket-path`** — Use when the telemetry socket is at a fully custom location (e.g., inside a specific container mount or non-standard prefix)
- **`--file-prefix`** — Use when you know the DPDK EAL `--file-prefix` value but auto-detection from OVS is not available (e.g., standalone testpmd not managed by OVS)

## Deployment Considerations

### Host Placement

The dpdk profiler engine must be co-located on the **same host** where the DPDK application (OVS-DPDK or testpmd) is running. The telemetry socket is a local Unix domain socket that cannot be accessed remotely.

### VM-Based Workloads

When testpmd runs **inside a VM** (e.g., OVS-DPDK + VM-testpmd topology), the telemetry socket is created inside the server engine container's filesystem. To make it visible to the profiler engine, add `host-mounts` to the server remote configuration in the run-file:

```json
{
    "engines": [ { "role": "server", "ids": "2" } ],
    "config": {
        "settings": {
            "osruntime": "chroot",
            "host-mounts": [ { "src": "/run" } ]
        },
        "host": "192.168.0.103"
    }
}
```

This bind-mounts the host's `/run` into the server container, ensuring the testpmd socket at `/run/dpdk/<cs_label>/dpdk_telemetry.v2` is on the shared host filesystem where the profiler can find it.

### OVS-DPDK with Telemetry Disabled

If OVS is configured with `dpdk-extra="--no-telemetry"`, no telemetry socket will be created. The tool detects this configuration and emits a warning. To enable telemetry:

```bash
ovs-vsctl set Open_vSwitch . other_config:dpdk-extra=""
systemctl restart openvswitch
```

## Telemetry Endpoints Collected

### Default Profile

| Endpoint | Data | Per-port |
|----------|------|----------|
| `/ethdev/stats` | Packet/byte counters, per-queue arrays | Yes |
| `/ethdev/xstats` | Extended NIC statistics (100+ counters) | Yes |
| `/ethdev/link_status` | Link state, speed, duplex | Yes |
| `/ethdev/info` | Device info, link speed (Mbps) | Yes |
| `/mempool/list` + `/mempool/info` | Memory pool utilization | Global |
| `/eal/params` | EAL configuration (captured once) | Global |

## Application Profiles

Profiles define which telemetry endpoints to query. They live in the `profiles/` directory as JSON files.

```json
{
    "name": "default",
    "description": "Standard DPDK library-level telemetry endpoints for testpmd/OVS-DPDK",
    "endpoints_per_port": ["/ethdev/stats", "/ethdev/xstats", "/ethdev/link_status", "/ethdev/info"],
    "endpoints_global": ["/mempool/list"],
    "mempool_info": true
}
```

## CDM Metrics Produced

### Core Metrics (cumulative counters)

| Telemetry Source | CDM Class | CDM Type | Dimensions |
|------------------|-----------|----------|------------|
| `ipackets` | throughput | rx-packets | port, device |
| `opackets` | throughput | tx-packets | port, device |
| `ibytes` | throughput | rx-bytes | port, device |
| `obytes` | throughput | tx-bytes | port, device |
| `imissed` | count | rx-missed | port, device |
| `ierrors` | count | rx-errors | port, device |
| `oerrors` | count | tx-errors | port, device |
| `rx_nombuf` | count | rx-nombuf | port, device |

### Rate Metrics (delta-computed per sample interval)

| CDM Type | CDM Class | Unit | Dimensions |
|----------|-----------|------|------------|
| rx-pps | throughput | packets/sec | port, device |
| tx-pps | throughput | packets/sec | port, device |
| rx-Gbps | throughput | Gbps | port, device |
| tx-Gbps | throughput | Gbps | port, device |
| rx-missed-sec | throughput | drops/sec | port, device |

### Per-Queue Metrics

| CDM Type | Dimensions | Description |
|----------|------------|-------------|
| queue-q{N}-packets | port, device, direction | Per-queue packet counter from ethdev/stats |
| xstat-q\_packets | port, device, direction, queue | Normalized per-queue xstat packets |
| xstat-q\_bytes | port, device, direction, queue | Normalized per-queue xstat bytes |
| xstat-q\_good\_packets | port, device, direction, queue | Per-queue valid packets (virtio) |

### xstats (direction-normalized)

| CDM Type | Dimensions | Description |
|----------|------------|-------------|
| xstat-good\_packets | port, device, direction | Valid packets |
| xstat-phy\_packets | port, device, direction | PHY-layer packets (Intel) |
| xstat-unicast\_packets | port, device, direction | Unicast packets |
| xstat-size\_64\_packets | port, device, direction | 64B packet count |
| xstat-missed\_errors | port, device, direction | NIC ring overflow |
| xstat-mac\_local\_errors | port, device | No direction (MAC-level) |

### Other Metrics

| CDM Type | CDM Class | Dimensions |
|----------|-----------|------------|
| link-status | pass/fail | port, device |
| link-speed-Mbps | count | port, device |
| mempool-used | count | mempool\_name |
| mempool-total | count | mempool\_name |

## Dependencies

- **Python 3** (standard library only: `socket`, `json`, `os`, `time`, `signal`, `argparse`, `glob`, `lzma`, `pathlib`)
- **xz** (compression)
- **toolbox** (perftool-incubator CDM metrics library — available in crucible controller environment via `TOOLBOX_HOME`)
- **No DPDK libraries required** — telemetry is consumed via raw Unix domain sockets

## Crucible Integration

### Register the tool

Add to `config/repos.json` in the crucible repository:

```json
{
    "name": "dpdk",
    "type": "tool",
    "repository": "<repo-url-or-local-path>",
    "primary-branch": "main",
    "checkout": { "mode": "follow", "target": "main" }
}
```

Then activate:

```bash
crucible update dpdk
```

### Example run file (trafficgen + tool-dpdk)

```json
"tool-params": [
    { "tool": "sysstat" },
    { "tool": "procstat" },
    { "tool": "dpdk", "params": [
        { "arg": "interval", "val": "1" }
    ]}
]
```

**Notes:**
- The profiler is auto-deployed to all hosts. No explicit `--socket-path` or `--connect-timeout` needed.
- For VM testpmd hosts, add `"host-mounts": [{"src": "/run"}]` to the server remote settings.
- The collector retries socket discovery indefinitely until the DPDK application starts.

## Running Tests

```bash
python3 -m pytest tests/test_telemetry_client.py -v
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## References

- [DPDK Telemetry Guide](https://doc.dpdk.org/guides/howto/telemetry.html)
- [perftool-incubator](https://github.com/perftool-incubator)
- [crucible](https://github.com/perftool-incubator/crucible)
- [bench-trafficgen](https://github.com/perftool-incubator/bench-trafficgen)
- [Architecture Document](docs/tool-dpdk-technical-architecture.md)
