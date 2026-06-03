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
| Graceful timeout handling (180s default, diagnostic messages) | ✅ Complete |
| Delta/rate computation for cumulative counters | ❌ Pending |
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
| `--connect-timeout` | `180` | Max seconds to wait for telemetry socket (increased from 30s for crucible timing) |

## How It Works

Rickshaw sets CWD to the tool output directory before invoking start/stop scripts. Parameters arrive as `--key value` arguments parsed with `getopt`.

1. **dpdk-start** — Parses `--interval`, `--profile`, `--file-prefix`, `--socket-path`, `--connect-timeout` via `getopt`; launches `dpdk-collect` in the background; saves PID to `dpdk-collect-pid.txt`
2. **dpdk-collect** — Searches multiple directories for the DPDK telemetry socket (with 180s retry + exponential backoff), discovers ports and mempools, polls endpoints at the configured interval, writes timestamped JSONL output. On timeout, exits gracefully with diagnostic messages instead of a traceback.
3. **dpdk-stop** — Reads PID, sends SIGTERM (10s grace, SIGKILL fallback), compresses output with `xz --threads=0`
4. **dpdk-post-process** — Defaults to CWD when called without arguments (matching rickshaw convention). Resolves `TOOLBOX_HOME` for the metrics library, reads compressed JSONL, maps DPDK telemetry to CDM metrics via `toolbox.metrics.log_sample()` / `finish_samples()`, writes `post-process-data.json`

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

When testpmd runs **inside a VM** (e.g., OVS-DPDK + VM-testpmd topology), the telemetry socket lives inside the VM's filesystem and is **not accessible** from the hypervisor host. In this scenario:
- The dpdk tool on the hypervisor will gracefully time out with no data — this is expected
- To collect testpmd telemetry from within the VM, the tool must be deployed inside the VM itself

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

| Telemetry Source | CDM Class | CDM Type | Dimensions |
|------------------|-----------|----------|------------|
| `ipackets` | throughput | rx-packets | port |
| `opackets` | throughput | tx-packets | port |
| `ibytes` | throughput | rx-bytes | port |
| `obytes` | throughput | tx-bytes | port |
| `imissed` | count | rx-missed | port |
| `ierrors` | count | rx-errors | port |
| `oerrors` | count | tx-errors | port |
| `rx_nombuf` | count | rx-nombuf | port |
| `q_ipackets[N]` | throughput | rx-queue-packets | port, queue |
| `q_opackets[N]` | throughput | tx-queue-packets | port, queue |
| xstats | count | xstat-\<name\> | port |
| link_status | pass/fail | link-status | port |
| link speed | count | link-speed-Mbps | port |
| mempool count | count | mempool-used | mempool_name |
| mempool size | count | mempool-total | mempool_name |

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
        { "arg": "interval", "val": "1" },
        { "arg": "profile", "val": "default" }
    ]}
]
```

**Important:** The profiler must be co-located on the same host as the DUT server (testpmd) so tool-dpdk can access the telemetry socket at `/var/run/dpdk/`.

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
