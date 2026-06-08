# Tool-dpdk

## Purpose
Crucible tool for collecting DPDK port and queue statistics via dpdk-telemetry during benchmark runs. Produces delta-rate metrics (pps, Gbps), direction-normalized xstats, and per-queue breakouts.

## Language
- Bash for start/stop wrapper scripts
- Python for collection and post-processing

## Conventions
- Primary branch is `main`
- Standard Bash modelines and 4-space indentation
- Python code follows 4-space indentation with standard modelines

## Architecture

- `dpdk-start` — Bash wrapper that launches `dpdk-collect` in background
- `dpdk-collect` — Python collector that discovers the DPDK telemetry socket, polls endpoints, stores per-port metadata, writes JSONL output
- `dpdk-stop` — Bash wrapper that sends SIGTERM and compresses output
- `dpdk-post-process` — Python post-processor that computes delta rates, normalizes xstats with direction/queue/device labels, and emits CDM metrics
- `dpdk_telemetry_client.py` — Reusable DPDK Telemetry v2 socket client with recursive discovery, negotiated recv buffer, and retry logic

## Socket Discovery

The telemetry client uses **recursive globbing** (`**/dpdk_telemetry.v2`) across multiple search directories. When no explicit socket-path or file-prefix is given, the collector also attempts **OVS file-prefix auto-detection** via `ovs-vsctl`.

Search directories: `/var/run/dpdk`, `/var/run/openvswitch`, `/var/run/openvswitch/.dpdk`, `/run/dpdk`, `/run/openvswitch`, `/run/openvswitch/.dpdk`, `/tmp/dpdk`

The collector retries socket discovery **indefinitely** (in 30s cycles) until SIGTERM, accommodating the timing gap between rickshaw's `start-tools` and `server-start` phases.

## Post-Processing

- Computes delta rates from cumulative counters: `rx-pps`, `tx-pps`, `rx-Gbps`, `tx-Gbps`, `rx-missed-sec`
- Normalizes xstats by stripping `rx_`/`tx_` prefix and adding `direction` label (CDM field)
- Normalizes per-queue xstats with `direction` + `queue` labels (queue requires CDM schema update)
- Adds PCI address via `device` label from `/ethdev/info`
- Core metrics (`rx-packets`, `tx-packets`, etc.) remain unchanged

## Deployment Notes

- The profiler must be on the same host as the DPDK application (socket is local)
- For VM-based testpmd with Podman: add `"host-mounts": [{"src": "/run"}]` to the server remote config so the telemetry socket is visible to the profiler
- OVS-DPDK with `--no-telemetry` will produce no data; the tool detects and warns about this
- On hosts with no DPDK application (e.g., TRex), the tool retries until stopped, then exits cleanly with no error
- The `--connect-timeout` parameter (default 30s) controls the per-cycle retry interval, not a total timeout
