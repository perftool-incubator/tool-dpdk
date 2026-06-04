# Tool-dpdk

## Purpose
Crucible tool for collecting DPDK port and queue statistics via dpdk-telemetry during benchmark runs.

## Language
- Bash for start/stop wrapper scripts
- Python for collection and post-processing

## Conventions
- Primary branch is `main`
- Standard Bash modelines and 4-space indentation
- Python code follows 4-space indentation with standard modelines

## Architecture

- `dpdk-start` — Bash wrapper that launches `dpdk-collect` in background
- `dpdk-collect` — Python collector that discovers the DPDK telemetry socket, polls endpoints, writes JSONL output
- `dpdk-stop` — Bash wrapper that sends SIGTERM and compresses output
- `dpdk-post-process` — Python post-processor that converts JSONL to CDM metrics
- `dpdk_telemetry_client.py` — Reusable DPDK Telemetry v2 socket client with recursive discovery and retry logic

## Socket Discovery

The telemetry client uses **recursive globbing** (`**/dpdk_telemetry.v2`) across multiple search directories. When no explicit socket-path or file-prefix is given, the collector also attempts **OVS file-prefix auto-detection** via `ovs-vsctl`.

Search directories: `/var/run/dpdk`, `/var/run/openvswitch`, `/var/run/openvswitch/.dpdk`, `/run/dpdk`, `/run/openvswitch`, `/run/openvswitch/.dpdk`, `/tmp/dpdk`

## Deployment Notes

- The profiler must be on the same host as the DPDK application (socket is local)
- For VM-based testpmd: the telemetry socket is inside the VM, inaccessible from the hypervisor
- OVS-DPDK with `--no-telemetry` will produce no data; the tool detects and warns about this
- On hosts with no DPDK application, the tool gracefully times out after `--connect-timeout` seconds
