# Tool-dpdk

## Purpose
Crucible tool for collecting DPDK port, queue, and mempool statistics via dpdk-telemetry v2 during benchmark runs. Produces delta-rate metrics (pps, Gbps), direction-normalized xstats, and per-queue breakouts.

## Languages
- Bash: start/stop wrapper scripts (`dpdk-start`, `dpdk-stop`)
- Python: collection and post-processing (`dpdk-collect`, `dpdk-post-process`, `dpdk_telemetry_client.py`)

## Key Files
| File | Purpose |
|------|---------|
| `dpdk-start` | Launches `dpdk-collect` in background with configured parameters |
| `dpdk-stop` | Sends SIGTERM and compresses output with xz |
| `dpdk-collect` | Discovers DPDK telemetry socket, polls endpoints, writes JSONL output |
| `dpdk-post-process` | Computes delta rates, normalizes xstats, and emits CDM metrics |
| `dpdk_telemetry_client.py` | Reusable DPDK Telemetry v2 socket client |
| `rickshaw.json` | Rickshaw integration: collector scripts, blacklist/whitelist |
| `workshop.json` | Engine image build requirements |
| `tool-metadata.json` | Machine-readable description and CDM-indexed status (consumed by `crucible tools list`) |
| `multiplex.json` | Parameter validation rules and `defaults` preset for multiplex (mirrors benchmark `multiplex.json`) |

## Configuration
- `--interval <seconds>` — Polling interval (default: `1`)
- `--profile <name>` — Telemetry profile matching JSON file in `profiles/` (default: `default`)
- `--connect-timeout <seconds>` — Socket discovery retry cycle timeout (default: `30`)
- `--file-prefix <prefix>` — DPDK runtime file prefix (e.g. `rte`, `ovs`)
- `--socket-path <path>` — Explicit path to DPDK telemetry v2 UNIX domain socket

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

- `process(output_dir, input_dir=".")` separates input reads from output writes: input files (`dpdk-telemetry-output.json.xz`, `engine-env.txt`) are read from `input_dir`; output (`post-process-data.json`) is written to `output_dir` (default `"postprocess"`)
- Computes delta rates from cumulative counters: `rx-pps`, `tx-pps`, `rx-Gbps`, `tx-Gbps`, `rx-missed-sec`
- Normalizes xstats by stripping `rx_`/`tx_` prefix and adding `direction` label (CDM field)
- Normalizes per-queue xstats with `direction` + `queue` labels
- Per-queue ethdev stats (`q_ipackets`/`q_opackets`) use generic `type: "queue-packets"` with `queue` as a named dimension (not embedded in type name)
- Adds PCI address via `device` label from `/ethdev/info`
- Core metrics (`rx-packets`, `tx-packets`, etc.) remain unchanged
- Part of PERFNFV-316: all post-processing artifacts go into `postprocess/` subdirectory (coordinated with toolbox PR #117 and rickshaw PR #826)

## Multi-Instance Support

- Supports multi-instance deployment via rickshaw's `id`, `deployment`, and `opt-tag` run file fields
- Instance isolation is directory-based: rickshaw creates `tool-data/{tool-id}/` per instance; scripts use `pwd`
- CDM metric source name is read from `engine-env.txt` (`tool_name=` line) by `get_tool_source_name()` in `dpdk-post-process`; defaults to `dpdk` when no env file is present (backward compatible)
- No start/stop/collect script changes are needed for multi-instance — they already use fixed filenames relative to `pwd`
- Regression tests in `tests/test_post_process.py` cover source naming for single-instance, multi-instance, and backward compatibility

## Deployment Notes

- The profiler must be on the same host as the DPDK application (socket is local)
- For VM-based testpmd with Podman: add `"host-mounts": [{"src": "/run"}]` to the server remote config so the telemetry socket is visible to the profiler
- OVS-DPDK with `--no-telemetry` will produce no data; the tool detects and warns about this
- On hosts with no DPDK application (e.g., TRex), the tool retries until stopped, then exits cleanly with no error
- The `--connect-timeout` parameter (default 30s) controls the per-cycle retry interval, not a total timeout

## Conventions
- Primary branch is `main`
- Standard Bash modelines and 4-space indentation
- Python code follows 4-space indentation with standard modelines
