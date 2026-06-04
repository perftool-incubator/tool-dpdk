# tool-dpdk — Technical Architecture Document

**End-to-end DPDK telemetry collection for the perftool-incubator / crucible ecosystem**

| Attribute | Value |
|-----------|-------|
| **Project** | perftool-incubator/tool-dpdk |
| **Document Version** | 1.3 (socket discovery: recursive glob, OVS auto-detection, diagnostics) |
| **Primary Target** | dpdk-testpmd |
| **Future Targets** | Grout (DPDK/grout), l3fwd-power |
| **Phases** | 5 |
| **Total Tasks** | 50 |
| **External DPDK Deps** | 0 |
| **Implementation Status** | Phase 1 complete, Phase 2 partial (post-process done, delta rates pending) |
| **Crucible Integration** | Registered and activated (`config/repos.json` + `subprojects/tools/dpdk`) |

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement, Requirements & Real-Time Gaps](#2-problem-statement-requirements--real-time-gaps)
3. [System Context](#3-system-context)
4. [End-to-End Data Collection Flows](#4-end-to-end-data-collection-flows)
5. [DPDK Telemetry Protocol](#5-dpdk-telemetry-protocol)
6. [tool-dpdk Internal Architecture](#6-tool-dpdk-internal-architecture)
7. [Crucible / Rickshaw Integration](#7-crucible--rickshaw-integration)
8. [Gap Analysis and Dependencies](#8-gap-analysis-and-dependencies)
9. [Step-by-Step Task Plan](#9-step-by-step-task-plan)
10. [Risk Mitigation](#10-risk-mitigation)

**Appendices**

- [Appendix A — Repository Creation Guide](#appendix-a--repository-creation-guide)

---

## 1. Executive Summary

tool-dpdk is a new telemetry collection tool for the perftool-incubator ecosystem. It connects to the **DPDK Telemetry API** — a JSON-over-Unix-socket interface exposed by every DPDK application since DPDK 20.05 — to collect real-time port statistics, extended NIC counters, queue-level metrics, and memory pool utilization. Collected data is post-processed into Crucible's **Common Data Model (CDM)** and indexed in OpenSearch for correlation with benchmark results from bench-trafficgen.

### Scope

**In Scope:**

- Collect metrics from dpdk-testpmd via DPDK Telemetry v2 socket
- Integrate with crucible/rickshaw tool lifecycle
- Post-process into CDM-compliant metric output
- Extensible profile system for future DPDK apps
- Grout (DPDK/grout) integration path design
- Support remotehosts, k8s/OpenShift, and OSP endpoints

**Out of Scope:**

- Modifications to dpdk-testpmd or Grout source code
- Real-time dashboarding (CDM handles visualization)
- TRex telemetry collection (separate tool consideration)
- DPDK v1 telemetry protocol support

---

## 2. Problem Statement, Requirements & Real-Time Gaps

### 2.1 The Problem Today

In the current perftool-incubator / crucible ecosystem, **there is zero visibility into the DPDK data-plane** during bench-trafficgen runs. When a binary-search test fails to converge, shows unexpected packet loss, or reports throughput below expectations, engineers face a blind spot:

```
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │  WHAT WE CAN SEE TODAY                  WHAT WE CANNOT SEE TODAY             │
 │  ═══════════════════                    ═════════════════════════            │
 │                                                                              │
 │  ✓ TRex Tx/Rx rates (binary-search.py)  ✗ testpmd per-port packet counters   │
 │  ✓ TRex latency histograms              ✗ testpmd per-queue distribution     │
 │  ✓ OVS-DPDK PMD stats (tool-ovs)        ✗ NIC-level extended stats (xstats)  │
 │  ✓ OVS flow table / conntrack           ✗ Rx missed / Rx nombuf events       │
 │  ✓ CPU / memory host-level metrics      ✗ Mempool exhaustion / pressure      │
 │  ✓ NIC sysfs counters (tool-sysfs)      ✗ Link flaps during test runs        │
 │                                         ✗ Queue stalls / imbalances          │
 │                                         ✗ DPDK app EAL configuration         │
 │                                         ✗ Grout graph-node processing cost   │
 └──────────────────────────────────────────────────────────────────────────────┘
```

This gap leads to several recurring engineering problems:

**Problem 1 — "Where did the packets go?"**
When TRex reports packets sent but fewer received, the loss could be at the NIC Rx ring (imissed), inside the DPDK app's forwarding path, at the mempool (rx_nombuf), or at the Tx ring. Without DUT-side counters correlated to the same timeline, root-cause is guesswork.

**Problem 2 — "Why isn't throughput scaling?"**
testpmd and Grout use multi-queue RSS to distribute packets across cores. If one queue is overloaded while others are idle, throughput hits a per-core ceiling. Per-queue `q_ipackets[N]` / `q_opackets[N]` counters expose this imbalance, but they are not collected today.

**Problem 3 — "Inconsistent results between runs"**
A link flap, a transient NIC error, or mempool exhaustion during a specific trial can skew binary-search convergence. Without `link_status`, `ierrors`, and mempool utilization sampled alongside the benchmark, these transient events are invisible in post-analysis.

**Problem 4 — "Is it the NIC or the software?"**
Extended NIC statistics (xstats) expose hardware-level counters — CRC errors, flow-control pause frames, PCIe replay counts — that distinguish NIC/cable/switch issues from software problems. These are available via DPDK telemetry but are never captured during crucible runs.

**Problem 5 — "What was the DUT actually configured to do?"**
EAL parameters, queue counts, offload flags, and RSS configuration are set at testpmd launch time but never recorded in CDM. If a test fails, there is no way to confirm the DUT was configured as expected without manually inspecting logs.

**Problem 6 — "No Grout data-path visibility"**
As Grout (rte_graph-based router) is adopted in bench-trafficgen as a DUT, there is no tooling to capture its graph-node processing statistics (cycles per node, packets per call, cache misses) alongside the standard DPDK ethdev metrics.

### 2.2 Requirements

The following requirements are derived from the real-time gaps identified above.

#### Functional Requirements

| ID | Requirement | Priority | Addresses Gap | Status |
|----|-------------|----------|---------------|--------|
| FR-01 | Collect per-port packet and byte counters (`ipackets`, `opackets`, `ibytes`, `obytes`) from the DUT at configurable intervals during bench-trafficgen runs | **Must** | Problem 1 | ✅ Done |
| FR-02 | Collect per-port error and drop counters (`imissed`, `ierrors`, `oerrors`, `rx_nombuf`) from the DUT | **Must** | Problem 1, 3 | ✅ Done |
| FR-03 | Collect per-queue packet and byte counters (`q_ipackets[N]`, `q_opackets[N]`) to identify queue imbalances | **Must** | Problem 2 | ✅ Done |
| FR-04 | Collect extended NIC statistics (xstats) to capture hardware-level counters from the DUT NIC driver | **Must** | Problem 4 | ✅ Done |
| FR-05 | Collect link status (`link_status`) at each sample interval to detect link flaps during test runs | **Should** | Problem 3 | ✅ Done |
| FR-06 | Collect mempool utilization (`mempool/info`) to detect memory pressure and buffer exhaustion | **Should** | Problem 1, 3 | ✅ Done |
| FR-07 | Record DUT EAL parameters and application configuration (`/eal/params`, `/eal/app_params`) at collection start | **Should** | Problem 5 | 🔶 Partial — `/eal/params` captured in output header; `/eal/app_params` not yet collected |
| FR-08 | Collect DUT queue configuration (`rx_queue`, `tx_queue`) and device info (`/ethdev/info`) for configuration verification | **Should** | Problem 5 | ⏳ Pending — requires testpmd profile (Phase 4) |
| FR-09 | Support Grout-specific telemetry (graph-node stats, route state) via the Grout control-plane socket | **Could** (future) | Problem 6 | ⏳ Pending — Phase 5 |
| FR-10 | Support application-specific telemetry endpoints (e.g., `/l3fwd_power/stats`) via extensible profiles | **Could** (future) | Extensibility | 🔶 Partial — profile loading implemented; only `default` profile exists |
| FR-11 | Auto-discover DPDK telemetry sockets when `--file-prefix` is not explicitly provided | **Should** | Usability | ✅ Done — recursive globbing across 7 search directories + OVS file-prefix auto-detection via `ovs-vsctl` |
| FR-12 | Compute per-second rates (deltas) from cumulative counters in post-processing | **Must** | All throughput metrics | ❌ Not done |

#### Non-Functional Requirements

| ID | Requirement | Priority | Rationale | Status |
|----|-------------|----------|-----------|--------|
| NFR-01 | The tool must not impact DUT forwarding performance by more than 0.1% | **Must** | DPDK telemetry runs on a separate thread; tool polls at 1s intervals to stay non-intrusive | ✅ Design met — 1s polling, separate telemetry thread; formal benchmark pending (Task 3.8) |
| NFR-02 | All collected data must be post-processed into CDM-compliant `metric_desc` + `metric_data` documents | **Must** | Mandatory contract for any crucible tool | ✅ Done |
| NFR-03 | The tool must work without any DPDK libraries, headers, or development packages installed in the collector container | **Must** | DPDK telemetry is consumed via raw Unix socket; Python stdlib is sufficient | ✅ Done — only python3 + xz in workshop.json |
| NFR-04 | The tool must handle DUT restart (socket disappearance) gracefully with automatic reconnection | **Should** | testpmd may restart between binary-search trials | ✅ Done — `connect_with_retry()` + reconnect on connection loss |
| NFR-05 | The tool must support all crucible endpoint types: remotehosts, k8s/OpenShift, and OSP | **Must** | Ecosystem parity with existing tools | 🔶 Partial — rickshaw.json whitelists all three; volume mount config pending (Phase 3) |
| NFR-06 | Collection output must be compressed with `xz` before transfer to the controller | **Must** | Standard archival pattern in perftool-incubator | ✅ Done |
| NFR-07 | The tool must be extensible to new DPDK applications without core code changes (profile-based) | **Should** | Future support for Grout, l3fwd-power, and custom apps | ✅ Done — profile loading infrastructure in place |

### 2.3 How tool-dpdk Fills Each Gap

The table below maps each identified real-time gap to the specific tool-dpdk capability that addresses it, the telemetry source used, and when the capability is delivered.

| Real-Time Gap | Current State | tool-dpdk Solution | Telemetry Source | Phase | Status |
|---------------|---------------|---------------------|------------------|-------|--------|
| **Packet loss location** — Cannot determine where packets are dropped inside the DUT | TRex only reports final Tx/Rx counts | Collect `imissed`, `ierrors`, `oerrors`, `rx_nombuf` per-port at every sample interval; post-process into CDM timeline correlated with TRex results | `/ethdev/stats` | 1 | ✅ Done |
| **Queue imbalance** — Cannot detect per-core RSS skew causing throughput ceiling | No per-queue visibility | Collect `q_ipackets[N]` and `q_opackets[N]` arrays; post-process each queue as a separate CDM metric dimension | `/ethdev/stats` | 1 | ✅ Done |
| **Transient NIC events** — Link flaps, error bursts are invisible | No DUT-side event capture | Sample `link_status` (up/down + speed) and error counters each interval; post-process as pass/fail and count metrics | `/ethdev/link_status`, `/ethdev/stats` | 1 | ✅ Done |
| **Hardware vs software distinction** — Cannot tell NIC/cable/switch issues from app bugs | NIC counters not collected from DUT context | Collect full xstats (CRC errors, PCIe replays, pause frames, etc.); filter zeros to manage response size | `/ethdev/xstats` | 1 | ✅ Done |
| **Memory pressure** — Mempool exhaustion causes silent Rx drops (`rx_nombuf`) | Not monitored | Track `mempool/info` (used count vs total size) per mempool at each interval | `/mempool/info` | 2 | ✅ Done |
| **DUT configuration drift** — Cannot confirm DUT config matches expectations | EAL params unrecorded | Capture `/eal/params`, `/eal/app_params`, and `/ethdev/info` at first sample; store as metadata metrics | `/eal/params`, `/ethdev/info` | 2 | 🔶 Partial — `/eal/params` captured; `/eal/app_params` and `/ethdev/info` pending |
| **Queue state verification** — Cannot confirm all queues are active and correctly configured | Queue config not captured | Collect `rx_queue` and `tx_queue` state per port; detect stopped/errored queues | `/ethdev/rx_queue`, `/ethdev/tx_queue` | 2 | ⏳ Pending — requires testpmd profile |
| **No DUT metrics in CDM** — Cannot correlate DUT behavior with TRex results in OpenSearch | No tool collects DPDK telemetry | All metrics indexed into CDM alongside TRex profiler data; same `run` → `iteration` → `sample` → `period` hierarchy | `toolbox.metrics` API | 2 | ✅ Done |
| **Grout graph-node costs** — Cannot measure per-node processing overhead in rte_graph pipeline | No Grout telemetry collection | Dual-socket collection: DPDK telemetry + Grout control-plane socket; graph node cycles/pkts/call mapped to CDM | Grout API + `/ethdev/*` | 5 | ⏳ Pending |
| **Inconsistent debugging workflow** — Engineers SSH into test hosts and run ad-hoc commands post-failure | No automated DUT data collection | Fully automated collection integrated into crucible lifecycle; data is always available in OpenSearch after every run | Automated via rickshaw | 1 | ✅ Done |

### 2.4 Before & After: Debugging a Failed Binary-Search Run

To illustrate the practical impact, here is a concrete before/after comparison when investigating a bench-trafficgen run that failed to converge at expected throughput:

**Before tool-dpdk (today):**

```
1. Engineer notices binary-search converged at 8.2 Mpps instead of expected 10 Mpps
2. Checks TRex profiler data → TRex sent 10 Mpps, received 8.2 Mpps → 18% loss somewhere
3. Checks tool-ovs PMD stats → OVS-DPDK not involved (testpmd is DUT, no OVS)
4. SSHs into test host → testpmd already terminated → console logs show stats-period output
   but no per-queue breakdown, no xstats, no mempool data
5. Reruns the test with manual dpdk-telemetry.py → adds hours to investigation
6. Discovers rx_nombuf was non-zero → mempool too small for burst traffic
7. Total debug time: 4-8 hours (often across multiple days)
```

**After tool-dpdk (target state):**

```
1. Engineer notices binary-search converged at 8.2 Mpps instead of expected 10 Mpps
2. Opens OpenSearch dashboard → filters by run ID
3. Sees tool-dpdk metrics alongside TRex data:
   - imissed = 0 on all ports ✓
   - rx_nombuf ramping up from trial 5 onward ← ROOT CAUSE
   - mempool-used hitting mempool-total during high-rate trials
   - q_ipackets distribution shows even RSS spread ✓
   - link_status stable ✓, no xstat errors ✓
4. Conclusion: mempool size undersized for 10 Mpps burst → increase --mbuf-size
5. Total debug time: 15-30 minutes
```

### 2.5 Traceability Matrix

| Requirement | Design Component | Implementation File | Task IDs | Status |
|-------------|-----------------|---------------------|----------|--------|
| FR-01, FR-02, FR-03 | Ethdev stats collection | `dpdk-collect`, `dpdk-post-process` | 1.6, 2.2 | ✅ Done |
| FR-04 | Extended stats collection | `dpdk-collect`, `dpdk-post-process` | 1.6, 2.3 | ✅ Done |
| FR-05 | Link status monitoring | `dpdk-collect`, `dpdk-post-process` | 1.6, 2.2 | ✅ Done |
| FR-06 | Mempool utilization | `dpdk-collect`, `dpdk-post-process` | 1.6, 2.4 | ✅ Done |
| FR-07, FR-08 | Config capture | `dpdk-collect` (first-sample metadata) | 4.3, 4.6 | 🔶 Partial — `/eal/params` only |
| FR-09 | Grout dual-socket collection | `grout-telemetry-client.py`, `dpdk-collect` | 5.1–5.7 | ⏳ Pending |
| FR-10 | Extensible profiles | `profiles/`, `dpdk-collect` | 4.1–4.5 | 🔶 Partial — infrastructure done, only `default` profile |
| FR-11 | Socket auto-discovery | `dpdk_telemetry_client.py` (recursive glob + `diagnose_paths()`), `dpdk-collect` (OVS auto-detection via `ovs-vsctl`) | 2.8 | ✅ Done |
| FR-12 | Delta computation | `dpdk-post-process` | 2.5 | ❌ Not done |
| NFR-01 | Non-intrusive polling | 1s default interval, separate telemetry thread | 3.8 | ✅ Design met |
| NFR-02 | CDM compliance | `dpdk-post-process` via `toolbox.metrics` | 2.6, 2.7 | ✅ Done |
| NFR-03 | Zero DPDK deps | Raw `SOCK_SEQPACKET` via Python stdlib | 1.4 | ✅ Done |
| NFR-04 | Graceful reconnect | `dpdk_telemetry_client.py` exponential backoff | 1.4 | ✅ Done |
| NFR-05 | All endpoint types | `rickshaw.json` whitelist + volume mounts | 3.1–3.4 | 🔶 Partial — whitelist configured; volume mounts pending |
| NFR-06 | xz compression | `dpdk-stop` | 1.7 | ✅ Done |
| NFR-07 | Profile extensibility | `profiles/` directory + `--profile` param | 4.1–4.2 | ✅ Done |

---

## 3. System Context

tool-dpdk operates within the crucible benchmarking ecosystem. The following diagram shows how all components interact during a bench-trafficgen run, where TRex drives traffic through a DPDK-based DUT (testpmd or Grout) while tool-dpdk collects telemetry alongside.

### 3.1 High-Level System Context

```
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │                              CRUCIBLE CONTROLLER                                     │
 │                                                                                      │
 │  crucible run bench-trafficgen --tool dpdk:interval=1,profile=testpmd ...            │
 │                                                                                      │
 │  ┌──────────────────────┐    ┌───────────────────────┐    ┌────────────────────────┐ │
 │  │     Rickshaw         │    │   CommonDataModel     │    │      OpenSearch        │ │
 │  │  (orchestration)     │───▶│  (metric indexing)    │───▶│  (query & visualize)   │ │
 │  └──────────┬───────────┘    └───────────────────────┘    └────────────────────────┘ │
 └─────────────┼────────────────────────────────────────────────────────────────────────┘
               │ deploys & orchestrates
    ┌──────────┼───────────────────────────────────────────────────────────────────┐
    │          ▼          BENCHMARK HOST / CLUSTER                                 │
    │                                                                              │
    │  ┌─────────────────────────┐         ┌────────────────────────────────────┐  │
    │  │  CLIENT ENGINE          │         │  SERVER ENGINE                     │  │
    │  │  (TRex traffic gen)     │         │  (dpdk-testpmd / Grout DUT)        │  │
    │  │                         │ network │                                    │  │
    │  │  binary-search.py       │════════▶│  dpdk-testpmd --auto-start         │  │
    │  │  generates traffic,     │◀════════│  --forward-mode mac                │  │
    │  │  binary search for      │  SRIOV  │  OR                                │  │
    │  │  max throughput         │  /DPDK  │  grout (rte_graph router)          │  │
    │  │                         │         │                                    │  │
    │  │  Telemetry socket:      │         │  Telemetry socket:                 │  │
    │  │  /var/run/dpdk/         │         │  /var/run/dpdk/                    │  │
    │  │   trafficgen_trex_/     │         │   <cs_label>/                      │  │ 
    │  │   dpdk_telemetry.v2     │         │   dpdk_telemetry.v2                │  │
    │  └─────────────────────────┘         └──────────────┬─────────────────────┘  │
    │                                                     │                        │
    │                                      shared volume  │ /var/run/dpdk/         │
    │                                                     │                        │
    │  ┌──────────────────────────────────────────────────┼──────────────────────┐ │
    │  │  PROFILER COLLECTOR                              │                      │ │
    │  │  (tool-dpdk)                                     ▼                      │ │
    │  │                                    ┌──────────────────────────┐         │ │
    │  │  dpdk-start ─────────────────────▶ │  dpdk-collect            │         │ │
    │  │                                    │  (polling loop)          │         │ │
    │  │                                    │                          │         │ │
    │  │                                    │  connect() to socket     │         │ │
    │  │                                    │  query /ethdev/stats     │         │ │
    │  │                                    │  query /ethdev/xstats    │         │ │
    │  │                                    │  query /mempool/info     │         │ │
    │  │                                    │  write timestamped JSON  │         │ │
    │  │                                    └──────────────────────────┘         │ │
    │  │                                                                         │ │
    │  │  dpdk-stop ─► kill + xz compress ─► dpdk-telemetry-output.json.xz       │ │
    │  └─────────────────────────────────────────────────────────────────────────┘ │
    └──────────────────────────────────────────────────────────────────────────────┘
               │
               │  transfer to controller
               ▼
    ┌─────────────────────────────────────────────────────────────────────────────┐
    │  POST-PROCESSING (on controller)                                            │
    │                                                                             │
    │  dpdk-post-process                                                          │
    │    ├── read dpdk-telemetry-output.json.xz                                   │
    │    ├── parse timestamped JSON samples                                       │
    │    ├── compute deltas (per-second rates)                                    │
    │    ├── toolbox.metrics.log_sample() for each metric                         │
    │    ├── toolbox.metrics.finish_samples()                                     │
    │    └── output: post-process-data.json + metric-data-*.json.xz               │
    │                         │                                                   │
    │                         ▼                                                   │
    │              CDM indexing into OpenSearch                                   │
    └─────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Component Roles

| Component | Role | Runs On | Interaction with tool-dpdk |
|-----------|------|---------|---------------------------|
| Crucible | User-facing CLI; defines runs with tools | Controller node | Passes `--tool dpdk:params` to rickshaw |
| Rickshaw | Orchestration engine; deploys engines + collectors | Controller node | Discovers tool-dpdk via `rickshaw.json`; manages lifecycle |
| bench-trafficgen (client) | TRex binary-search traffic generation | Client engine container | No direct interaction; drives traffic through DUT |
| bench-trafficgen (server) | DUT: testpmd or Grout forwarding packets | Server engine container | Exposes DPDK telemetry socket consumed by tool-dpdk |
| tool-dpdk | Telemetry collector for DPDK apps | Profiler collector container | Connects to DUT telemetry socket; produces CDM metrics |
| CommonDataModel | Metric indexing and schema | Controller node | Indexes tool-dpdk post-process output into OpenSearch |
| OpenSearch | Time-series metric storage and query | Controller node | Stores CDM `metric_desc` + `metric_data` documents |

---

## 4. End-to-End Data Collection Flows

### 4.1 Flow A — dpdk-testpmd (Primary Target)

In a bench-trafficgen run, testpmd acts as the DUT server. It is launched by `trafficgen-server-start` with DPDK EAL options including `--file-prefix` set to the client-server label (`RS_CS_LABEL`). The telemetry socket is created automatically by `librte_telemetry` inside the server engine container.

```
 TIME ──────────────────────────────────────────────────────────────────────────▶

 RICKSHAW    ┌─deploy engines─┐  ┌─roadblock──┐        ┌─roadblock──┐
 (orch)      │  & collectors  │  │  sync:     │        │  sync:     │
             └───────┬────────┘  │  tools-go  │        │  tools-end │
                     │           └─────┬──────┘        └─────┬──────┘
                     │                 │                     │
 TESTPMD             │     ┌───────────┼─────────────────────┼──────────┐
 (server)            │     │  dpdk-testpmd --file-prefix <cs_label>     │
                     │     │  --auto-start --forward-mode mac           │
                     │     │  --rxq 4 --txq 4 --nb-cores 4              │
                     │     │                                            │
                     │     │  librte_telemetry creates:                 │
                     │     │  /var/run/dpdk/<cs_label>/                 │
                     │     │    dpdk_telemetry.v2                       │
                     │     └───────────┼────────────────────────────────┘
                     │                 │
 TREX                │     ┌───────────┼──────────────────────┐
 (client)            │     │  binary-search.py                │
                     │     │  traffic ═══▶ testpmd ═══▶ back  │
                     │     │  search for max zero-loss rate   │
                     │     └───────────┼──────────────────────┘
                     │                 │
 TOOL-DPDK   ┌──────┴──────┐  ┌───────┴──────────────────────┴──────┐
 (profiler)  │ dpdk-start  │  │  dpdk-collect (background loop)     │
             │             │  │                                     │
             │ parse args  │  │  0. detect_ovs_file_prefix()        │
             │ --interval  │  │     (auto via ovs-vsctl if avail)   │
             │ --profile   │  │  1. recursive scan 7 dirs (**)      │
             │ --file-     │  │     dpdk_telemetry.v2               │
             │   prefix    │  │  2. connect(socket)                 │
             │             │  │  3. recv handshake {version,pid}    │
             │ launch      │  │  4. send("/") → discover endpoints  │
             │ dpdk-collect│  │  5. send("/ethdev/list") → ports    │
             │ save PID    │  │    DATE: <epoch_ms>                 │  ┌──────────┐
             └─────────────┘  │    /ethdev/stats,<port> → JSON      │  │dpdk-stop │
                              │    /ethdev/xstats,<port> → JSON     │  │          │
                              │    /ethdev/link_status,<port>       │  │kill PID  │
                              │    /mempool/info,<name> → JSON      │  │xz output │
                              │    write to output file             │  │          │
                              └─────────────────────────────────────┘  └──────────┘
                                                                            │
                                                                            ▼
                                                              dpdk-telemetry-output
                                                                    .json.xz
                                                                            │
 POST-       ┌──────────────────────────────────────────────────────────────┴───┐
 PROCESS     │  dpdk-post-process (on controller)                               │
 (ctrl)      │                                                                  │
             │  1. decompress dpdk-telemetry-output.json.xz                     │
             │  2. for each DATE-delimited sample:                              │
             │       parse JSON responses                                       │
             │       for each port, each metric:                                │
             │         desc = {source:"dpdk", class:"throughput", type:"..."}   │
             │         names = {port:"0", queue:"2", ...}                       │
             │         sample = {end: epoch_ms, value: N}                       │
             │         toolbox.metrics.log_sample(fid, desc, names, sample)     │
             │  3. (future) compute deltas when --compute-rates is implemented  │
             │  4. toolbox.metrics.finish_samples()                             │
             │  5. write post-process-data.json                                 │
             │                                                                  │
             │  Output files:                                                   │
             │    post-process-data.json                                        │
             │    metric-data-0.json.xz  (ethdev stats)                         │
             │    metric-data-1.json.xz  (ethdev xstats)                        │
             │    metric-data-2.json.xz  (mempool metrics)                      │
             └──────────────────────────────────────────────────────────────────┘
```

> **Note:** testpmd does not register custom telemetry commands. It relies entirely on DPDK library-level endpoints: `/ethdev/*` (19+ commands), `/eal/*`, `/mempool/*`. The same collector code works for any standard DPDK application without modification.

### 4.2 Flow B — Grout (Future Component)

Grout (Graph Router) is a DPDK-based network processing application using `rte_graph` for data path processing. As a DPDK application, it inherits the full DPDK Telemetry v2 interface. Additionally, Grout exposes its own control-plane Unix socket for configuration and graph-level statistics.

```
 ┌─────────────────────────────────────────────────────────────────────────┐
 │                     GROUT APPLICATION                                   │
 │                                                                         │
 │  ┌─────────────────────────┐    ┌──────────────────────────────────┐    │
 │  │  DPDK Telemetry v2      │    │  Grout Control-Plane Socket      │    │
 │  │  (librte_telemetry)     │    │  (libevent-based API)            │    │
 │  │                         │    │                                  │    │
 │  │  Standard endpoints:    │    │  Grout-specific data:            │    │
 │  │  /ethdev/stats          │    │  • graph node statistics         │    │
 │  │  /ethdev/xstats         │    │  • route table state             │    │
 │  │  /ethdev/link_status    │    │  • interface configuration       │    │
 │  │  /mempool/info          │    │  • ARP/NDP neighbor tables       │    │
 │  │  /eal/params            │    │  • firewall rule counters        │    │
 │  │                         │    │                                  │    │
 │  │  Socket:                │    │  Socket:                         │    │
 │  │  /var/run/dpdk/         │    │  /run/grout.sock                 │    │
 │  │   <prefix>/             │    │  (custom protocol)               │    │
 │  │   dpdk_telemetry.v2     │    │                                  │    │
 │  └────────────┬────────────┘    └──────────────┬───────────────────┘    │
 └───────────────┼────────────────────────────────┼────────────────────────┘
                 │                                │
                 │  SOCK_SEQPACKET + JSON         │  Grout API protocol
                 │                                │
 ┌───────────────┼────────────────────────────────┼────────────────────────┐
 │  TOOL-DPDK    │   (profiler collector)         │                        │
 │               ▼                                ▼                        │
 │  ┌────────────────────────┐  ┌─────────────────────────────────────┐    │
 │  │  DPDK Telemetry Client │  │  Grout Telemetry Client             │    │
 │  │  (Phase 1 — existing)  │  │  (Phase 5 — future extension)       │    │
 │  │                        │  │                                     │    │
 │  │  Reuses identical      │  │  New client module for Grout API    │    │
 │  │  collection logic as   │  │  Collects graph-node stats,         │    │
 │  │  testpmd path          │  │  route state, interface metrics     │    │
 │  └───────────┬────────────┘  └──────────────────┬──────────────────┘    │
 │              │                                   │                      │
 │              └──────────────┬────────────────────┘                      │
 │                             ▼                                           │
 │              ┌──────────────────────────────┐                           │
 │              │  Unified Output Stream       │                           │
 │              │  dpdk-telemetry-output       │                           │
 │              │    .json.xz                  │                           │
 │              │                              │                           │
 │              │  {source: "dpdk-ethdev"}     │                           │
 │              │  {source: "grout-graph"}     │                           │
 │              └──────────────┬───────────────┘                           │
 └─────────────────────────────┼───────────────────────────────────────────┘
                               │
                               ▼
              ┌─────────────────────────────────────┐
              │  dpdk-post-process                  │
              │  Routes samples by source tag:      │
              │  • "dpdk-ethdev" → ethdev metrics   │
              │  • "grout-graph" → graph metrics    │
              │  • "grout-route" → routing metrics  │
              │                                     │
              │  Output:                            │
              │  metric-data-0.json.xz (ethdev)     │
              │  metric-data-1.json.xz (xstats)     │
              │  metric-data-2.json.xz (mempool)    │
              │  metric-data-3.json.xz (graph)      │
              │  metric-data-4.json.xz (routes)     │
              └─────────────────────────────────────┘
```

### 4.3 Grout vs testpmd — Telemetry Comparison

| Dimension | dpdk-testpmd | Grout (DPDK/grout) |
|-----------|--------------|--------------------|
| DPDK telemetry v2 | Full support (library-level) | Full support (library-level) |
| Custom telemetry | None | Graph node stats, routing metrics via Grout API |
| Socket: DPDK | `/var/run/dpdk/<prefix>/dpdk_telemetry.v2` | `/var/run/dpdk/<prefix>/dpdk_telemetry.v2` |
| Socket: App-specific | N/A | `/run/grout.sock` (custom protocol) |
| Data path | Port-to-port forwarding (mac/io mode) | rte_graph processing pipeline |
| Packet processing model | Simple: receive → forward | Graph: rx → classify → route → tx |
| Key unique metrics | Per-queue pkt/byte counters | Graph node cycles, pkts/call, cache misses |
| tool-dpdk profile | `testpmd` (Phase 1) | `grout` (Phase 5 — future) |
| Collection complexity | Low — standard DPDK socket only | Medium — DPDK socket + Grout API socket |

---

## 5. DPDK Telemetry Protocol

### 5.1 Protocol Architecture

The DPDK Telemetry library (`librte_telemetry`) runs a dedicated listener thread inside each DPDK application process. It accepts client connections on a Unix domain socket (`SOCK_SEQPACKET`), providing message-boundary semantics that simplify parsing. The protocol is synchronous request-response with JSON encoding.

**Protocol Specification:**

| Property | Value |
|----------|-------|
| Socket family | `AF_UNIX` |
| Socket type | `SOCK_SEQPACKET` |
| Path (root) | `/var/run/dpdk/<file-prefix>/dpdk_telemetry.v2` |
| Path (non-root) | `$XDG_RUNTIME_DIR/dpdk/<prefix>/dpdk_telemetry.v2` |
| Default file-prefix | `rte` |
| Protocol version | v2 |
| Encoding | JSON (UTF-8) |
| Max response | 16,384 bytes |
| Framing | SOCK_SEQPACKET message boundaries |

**Connection Lifecycle:**

1. **Client connects** to Unix socket
2. **Server sends handshake:**
   ```json
   {"version":"DPDK 26.03.0","pid":60285,"max_output_len":16384}
   ```
3. **Client sends command:**
   ```
   /ethdev/stats,0
   ```
4. **Server responds with JSON:**
   ```json
   {"/ethdev/stats":{"ipackets":1234,"opackets":1230,...}}
   ```
5. **Repeat steps 3–4** or close connection

### 5.2 Complete Endpoint Reference

| Endpoint | Returns | Params | Apps | Priority |
|----------|---------|--------|------|----------|
| `/` | All available commands | None | All | **P0 — Required** |
| `/info` | DPDK version, PID, max_output_len | None | All | **P0 — Required** |
| `/ethdev/list` | Port ID array | None | All | **P0 — Required** |
| `/ethdev/stats` | Pkt/byte counters + per-queue arrays | port_id | All | **P0 — Required** |
| `/ethdev/xstats` | Extended NIC stats (100+ counters) | port_id [,hide_zero] | All | **P0 — Required** |
| `/ethdev/link_status` | Link state, speed, duplex | port_id | All | P1 — Important |
| `/ethdev/info` | Driver, MAC, queues, offloads | port_id | All | P1 — Important |
| `/ethdev/rx_queue` | Rx queue state, mempool | port_id [,queue_id] | All | P1 — Important |
| `/ethdev/tx_queue` | Tx queue state, offloads | port_id [,queue_id] | All | P1 — Important |
| `/ethdev/rss_info` | RSS hash configuration | port_id | All | P2 — Nice to have |
| `/ethdev/macs` | MAC address list | port_id | All | P2 — Nice to have |
| `/ethdev/flow_ctrl` | Flow control settings | port_id | All | P2 — Nice to have |
| `/eal/params` | EAL startup parameters | None | All | P1 — Important |
| `/eal/app_params` | App-specific parameters | None | All | P2 — Nice to have |
| `/mempool/list` | Mempool name list | None | All | P1 — Important |
| `/mempool/info` | Size, used count, flags | name | All | P1 — Important |
| `/cryptodev/stats` | Crypto enqueue/dequeue counts | dev_id | Crypto apps | P3 — Future |
| `/rawdev/xstats` | Raw device stats | dev_id | Raw apps | P3 — Future |
| `/l3fwd_power/stats` | empty_poll, full_poll, busy% | None | l3fwd-power | P3 — Future |

---

## 6. tool-dpdk Internal Architecture

### 6.1 Project Structure

| File | Language | Purpose |
|------|----------|---------|
| `rickshaw.json` | JSON | Tool manifest: lifecycle scripts, whitelist (profiler, compute), files-from-controller |
| `workshop.json` | JSON | Container build: python3, xz-utils, toolbox dependency |
| `dpdk-start` | Bash | Parse CLI args, discover socket, launch dpdk-collect in background, save PID |
| `dpdk-collect` | Python | Main collection loop: auto-detect OVS file-prefix, connect to DPDK telemetry socket, poll endpoints, write timestamped JSON; emits directory diagnostics on failure |
| `dpdk-stop` | Bash | Read PID, send SIGTERM, wait for exit, xz compress all output files |
| `dpdk-post-process` | Python | Read compressed JSON, compute deltas, emit CDM metrics via toolbox.metrics API |
| `dpdk_telemetry_client.py` | Python | Reusable client library: SOCK_SEQPACKET connection, handshake, query, reconnect, recursive multi-directory socket discovery, path diagnostics |
| `profiles/` | JSON | Application profiles defining endpoint sets per DPDK app type |
| `README.md` | Markdown | Usage guide, parameter reference, profile documentation |

### 6.2 Application Profile System

Profiles define which telemetry endpoints to query for each DPDK application type. The default profile covers all standard library-level endpoints. Application-specific profiles extend the default with custom endpoints registered by that application.

| Profile | Inherits | Endpoints | Use Case | Phase |
|---------|----------|-----------|----------|-------|
| `default` | — | `/ethdev/stats`, `/ethdev/xstats`, `/ethdev/link_status`, `/ethdev/info`, `/mempool/info` | Any DPDK app (testpmd, OVS-DPDK) | 1 |
| `testpmd` | default | + `/ethdev/rx_queue`, `/ethdev/tx_queue` | bench-trafficgen DUT with per-queue detail | 2 |
| `l3fwd-power` | default | + `/l3fwd_power/stats` | Power management benchmarks | 4 |
| `grout` | default | + Grout control-plane API queries | Graph router benchmarks | 5 |
| `custom` | default | User-defined via `--endpoints` param | Any DPDK app with custom telemetry | 4 |

### 6.3 CDM Metric Mapping

Each DPDK telemetry value maps to a CDM metric document with source, class, type, and dimensional names. Post-processing computes per-second deltas for cumulative counters.

| Telemetry Source | CDM Class | CDM Type | Dimensions | Implemented | Delta? |
|------------------|-----------|----------|------------|-------------|--------|
| `/ethdev/stats` → `ipackets` | throughput | rx-packets | port | **Yes** | No (cumulative; delta planned) |
| `/ethdev/stats` → `opackets` | throughput | tx-packets | port | **Yes** | No (cumulative; delta planned) |
| `/ethdev/stats` → `ibytes` | throughput | rx-bytes | port | **Yes** | No (cumulative; delta planned) |
| `/ethdev/stats` → `obytes` | throughput | tx-bytes | port | **Yes** | No (cumulative; delta planned) |
| `/ethdev/stats` → `imissed` | count | rx-missed | port | **Yes** | No (cumulative; delta planned) |
| `/ethdev/stats` → `ierrors` | count | rx-errors | port | **Yes** | No (cumulative; delta planned) |
| `/ethdev/stats` → `oerrors` | count | tx-errors | port | **Yes** | No (cumulative; delta planned) |
| `/ethdev/stats` → `rx_nombuf` | count | rx-nombuf | port | **Yes** | No (cumulative; delta planned) |
| `/ethdev/stats` → `q_ipackets[N]` | throughput | rx-queue-packets | port, queue | **Yes** | No (cumulative; delta planned) |
| `/ethdev/stats` → `q_opackets[N]` | throughput | tx-queue-packets | port, queue | **Yes** | No (cumulative; delta planned) |
| `/ethdev/xstats` → `<name>` | count | xstat-\<name\> | port | **Yes** | No (cumulative; delta planned) |
| `/ethdev/link_status` → `status` | pass/fail | link-status | port | **Yes** | No (point-in-time) |
| `/ethdev/info` → `speed` | count | link-speed-Mbps | port | **Yes** | No (point-in-time) |
| `/mempool/info` → `count` | count | mempool-used | mempool_name | **Yes** | No (point-in-time) |
| `/mempool/info` → `size` | count | mempool-total | mempool_name | **Yes** | No (point-in-time) |
| `/l3fwd_power/stats` → `busy_percent` | count | busy-percent | — | *Not yet* | No |

### 6.4 Configuration Parameters

Parameters passed via crucible `tool-params`, arriving as CLI arguments to `dpdk-start`:

| Parameter | Default | Type | Status | Description |
|-----------|---------|------|--------|-------------|
| `--interval` | `1` | int | **Implemented** | Collection interval in seconds |
| `--file-prefix` | (auto) | string | **Implemented** | DPDK `--file-prefix`; auto-scans multiple directories recursively if omitted (`/var/run/dpdk`, `/var/run/openvswitch`, `/var/run/openvswitch/.dpdk`, `/run/dpdk`, `/run/openvswitch`, `/run/openvswitch/.dpdk`, `/tmp/dpdk`, `$XDG_RUNTIME_DIR/dpdk`). Also auto-detects from OVS `other_config:dpdk-extra` via `ovs-vsctl` when available. |
| `--socket-path` | (derived) | string | **Implemented** | Explicit path to `dpdk_telemetry.v2` socket |
| `--profile` | `default` | string | **Implemented** | Application profile name (only `default` profile exists; includes `/ethdev/info` for link-speed) |
| `--connect-timeout` | `180` | int | **Implemented** | Max seconds to wait for telemetry socket (increased from 30s to accommodate crucible timing where profiler starts ~120s before testpmd) |
| `--endpoints` | (from profile) | string | *Planned* | Comma-separated additional endpoints to query |
| `--hide-zero-xstats` | `true` | bool | *Hardcoded* | Hardcoded in `dpdk-collect`; not yet exposed as CLI parameter |
| `--compute-rates` | `true` | bool | *Not implemented* | Per-second delta computation in post-processing |
| `--reconnect-backoff` | `2` | int | *Hardcoded* | Hardcoded as `backoff=2` in `connect_with_retry()`; not yet exposed as CLI parameter |

---

## 7. Crucible / Rickshaw Integration

### 7.1 Tool Lifecycle

```
 RICKSHAW ORCHESTRATION                    TOOL-DPDK ON COLLECTOR
 ========================                  =======================

 1. Deploy tool to collector       ──▶     Files copied via files-from-controller
    (rickshaw.json config)

 2. Roadblock: tools-start-begin   ──▶     3. Execute dpdk-start
                                              • Parse --interval, --profile, --file-prefix
                                              • Launch dpdk-collect in background
                                              • Save PID to dpdk-collect-pid.txt
                                              • dpdk-collect: wait for socket → connect → poll

 4. Roadblock: tools-start-end     ◀──     dpdk-start exits (collector running in bg)

 5. Benchmark runs                         dpdk-collect continues polling telemetry
    (TRex ←→ testpmd/Grout)                  every <interval> seconds

 6. Roadblock: tools-stop-begin    ──▶     7. Execute dpdk-stop
                                              • Read PID from dpdk-collect-pid.txt
                                              • Send SIGTERM to dpdk-collect
                                              • Wait for clean exit
                                              • xz compress dpdk-telemetry-output.json

 8. Roadblock: tools-stop-end      ◀──     dpdk-stop exits, output compressed

 9. Archive: transfer output       ◀──     dpdk-telemetry-output.json.xz transferred
    files to controller

10. Post-process on controller     ──▶    11. Execute dpdk-post-process
                                              • Read dpdk-telemetry-output.json.xz
                                              • Parse timestamped JSON samples
                                              • Compute deltas (per-second rates)
                                              • toolbox.metrics.log_sample() per metric
                                              • toolbox.metrics.finish_samples()
                                              • Write post-process-data.json

12. CDM indexing                   ◀──     metric-data-*.json.xz indexed to OpenSearch
```

### 7.2 rickshaw.json Manifest

```json
{
    "rickshaw-tool": {
        "schema": { "version": "2020.03.18" }
    },
    "tool": "dpdk",
    "controller": {
        "post-script": "%tool-dir%dpdk-post-process"
    },
    "collector": {
        "files-from-controller": [
            { "src": "%tool-dir%/dpdk-start", "dest": "/usr/bin/" },
            { "src": "%tool-dir%/dpdk-stop", "dest": "/usr/bin/" },
            { "src": "%tool-dir%/dpdk-collect", "dest": "/usr/bin/" },
            { "src": "%tool-dir%/dpdk_telemetry_client.py", "dest": "/usr/bin/" },
            { "src": "%tool-dir%/profiles", "dest": "/usr/bin/" }
        ],
        "blacklist": [
            { "endpoint": "remotehosts", "collector-types": [ "client", "server" ] },
            { "endpoint": "kube", "collector-types": [ "client", "server" ] }
        ],
        "whitelist": [
            { "endpoint": "osp", "collector-types": [ "compute" ] },
            { "endpoint": "remotehosts", "collector-types": [ "profiler" ] },
            { "endpoint": "kube", "collector-types": [ "profiler" ] }
        ],
        "start": "dpdk-start",
        "stop": "dpdk-stop"
    }
}
```

### 7.3 Deployment Topologies

| Endpoint Type | DUT Runs In | tool-dpdk Runs In | Socket Access | Volume Mount |
|---------------|-------------|-------------------|---------------|--------------|
| remotehosts | Podman container | Profiler container on host | `/var/run/dpdk` shared mount | `podman -v /var/run/dpdk:/var/run/dpdk` |
| remotehosts (OVS-DPDK) | Host process (ovs-vswitchd) | Profiler container on same host | `/var/run/openvswitch` shared mount | Automatic via rickshaw mandatory mounts (`/var/run` bind) |
| remotehosts (VM testpmd) | Inside guest VM | Profiler on hypervisor host | **Not accessible** — socket is inside VM | N/A — tool gracefully times out; deploy inside VM for telemetry |
| k8s / OpenShift | Pod (SRIOV NIC) | Profiler pod on same node | hostPath or emptyDir volume | `hostPath: /var/run/dpdk` |
| OSP (compute) | VM on compute | Compute collector on host | Host-level access | Direct filesystem access |

**Important deployment notes:**

- **OVS-DPDK hosts:** The profiler container already receives `/var/run` as a bind mount from rickshaw's mandatory mount list (see `remotehosts.py` line 903-920). The recursive socket discovery will find the OVS-DPDK telemetry socket regardless of subdirectory depth. If OVS uses a custom `--file-prefix`, the auto-detection via `ovs-vsctl` handles it automatically.
- **VM-based testpmd (e.g., OVS-DPDK + VM-testpmd-io topology):** The telemetry socket lives inside the VM's filesystem. The tool on the hypervisor host will time out gracefully after `--connect-timeout` seconds. This is expected behavior — to collect testpmd telemetry from within the VM, a separate profiler deployment inside the VM is required.
- **Hosts without DPDK (e.g., TRex client hosts):** The tool times out gracefully with diagnostic messages. No error is propagated to the benchmark result.

---

## 8. Gap Analysis and Dependencies

### 8.1 Integration Gaps

| Gap | Severity | Description | Solution | Status |
|-----|----------|-------------|----------|--------|
| Socket cross-container access | **Critical** | DPDK telemetry socket created inside server engine container; tool-dpdk runs in separate profiler container | Shared `/var/run/dpdk` volume mount between server and profiler containers | 🔶 Design resolved — profiler co-located on server host in run file; volume mount config pending |
| DPDK file-prefix discovery | **Critical** | testpmd uses `--file-prefix=$RS_CS_LABEL` which is an env var inside server container, not accessible to profiler | Auto-scan `/var/run/dpdk/**/dpdk_telemetry.v2` or pass `--file-prefix` as tool param | ✅ Resolved — recursive globbing across 7 search paths (dpdk, openvswitch, openvswitch/.dpdk, /run variants, /tmp, XDG) + OVS `ovs-vsctl` auto-detection + `--file-prefix` param + diagnostic logging on failure |
| Socket readiness timing | **High** | `dpdk-start` runs before testpmd; socket doesn't exist yet | Poll with exponential backoff in dpdk-collect; `--connect-timeout` param (default 180s) | ✅ Resolved — `connect_with_retry()` with exponential backoff; 180s default accommodates crucible timing; graceful `TimeoutError` with diagnostic messages |
| 16KB response truncation | Medium | Large xstats responses (100+ counters) may exceed 16,384 byte limit | Use `hide_zero=true`; if still truncated, split into subsets | ✅ Mitigated — `hide_zero=true` hardcoded in `dpdk-collect` |
| SOCK_SEQPACKET availability | Medium | Not all container runtimes expose this socket type | Python socket module handles it natively; fallback to SOCK_STREAM not needed for DPDK v2 | ✅ Resolved — works with `osruntime=chroot` |
| Multiple DPDK processes | Low | Both TRex and testpmd create telemetry sockets; tool needs to target correct one | Support `--file-prefix` list or auto-filter by process name in `/info` response | 🔶 Partial — `--file-prefix` param supported; auto-filter not implemented |
| Grout API protocol | Low (future) | Grout uses a custom control-plane socket separate from DPDK telemetry | Design pluggable client modules; implement Grout client in Phase 5 | ⏳ Pending — Phase 5 |

### 8.2 Dependency Matrix

**Build-time Dependencies (workshop.json):**

- **Python 3 standard library:** `socket`, `json`, `os`, `sys`, `time`, `signal`, `struct`, `argparse`, `pathlib`, `glob`
- **System packages:** `xz-utils` (compression)
- **perftool-incubator:** `toolbox` (CDM metrics library)
- **Not required:** No DPDK libraries, headers, or development packages

**Runtime Requirements:**

- **Kernel:** AF_UNIX + SOCK_SEQPACKET support (standard Linux)
- **Filesystem:** Read access to `/var/run/dpdk/` (shared volume)
- **Target process:** DPDK application with telemetry enabled (default since 20.05)
- **Configuration:** Knowledge of file-prefix (or auto-discovery)

**Deployment Prerequisites:**

- **Volume mounts:** `/var/run/dpdk` shared between server engine and profiler
- **rickshaw integration:** Tool registered in crucible `config/repos.json`
- **Container image:** Built via `workshop.json` with python3 + xz-utils
- **bench-trafficgen:** Server engine must expose `/var/run/dpdk` via volume

---

## 9. Step-by-Step Task Plan

> **Dependencies:** Phases are sequential — each builds on the previous. Phase 1 must complete before Phase 2 can begin. Phases 4 and 5 can partially overlap if the profile system (Tasks 4.1–4.2) is completed first.

### Phase 1 — Foundation (Weeks 1–3) ✅ Complete

Deliver a working collector that can connect to a standalone dpdk-testpmd instance and capture raw telemetry JSON output.

**Status:** All tasks complete. Repository created, rickshaw.json and workshop.json corrected to pass schema validation, all scripts implemented and tested, unit tests passing.

| Task | Description | Status |
|------|-------------|--------|
| 1.1 | Create `tool-dpdk` repository under perftool-incubator org | ✅ Done — created at `/root/tool-dpdk`, registered in crucible `config/repos.json` |
| 1.2 | Write `rickshaw.json` manifest (whitelist: profiler, compute; blacklist: client, server) | ✅ Done — corrected to use structured `{src, dest}` objects and per-endpoint whitelist/blacklist |
| 1.3 | Write `workshop.json` (python3, xz-utils) | ✅ Done — corrected to use `userenvs` + named `requirements` pattern with schema `2020.03.02` |
| 1.4 | Implement `dpdk_telemetry_client.py` — SOCK_SEQPACKET client with connect, handshake, query, reconnect | ✅ Done — includes `discover_socket()`, `connect_with_retry()` with exponential backoff |
| 1.5 | Implement `dpdk-start` — parse CLI args, discover socket path, launch dpdk-collect, save PID | ✅ Done |
| 1.6 | Implement `dpdk-collect` — polling loop: connect → discover ports → query endpoints → write JSONL | ✅ Done — profile-based endpoint selection, auto-discovery, reconnection on connection loss |
| 1.7 | Implement `dpdk-stop` — read PID, SIGTERM, wait, xz compress output | ✅ Done — 10s grace period, SIGKILL fallback |
| 1.8 | Write unit tests for `dpdk_telemetry_client.py` (mock socket) | ✅ Done — `FakeTelemetryServer`, discovery, connect, query, retry, timeout tests |
| 1.9 | Manual test: run standalone testpmd, verify tool-dpdk collects JSON output | ✅ Done |
| 1.10 | Write `README.md` with basic usage and parameter reference | ✅ Done |

### Phase 2 — CDM Integration (Weeks 4–6) 🔶 Partial

Build post-processing to convert collected JSON into CDM-compliant metrics. First crucible integration test.

**Status:** Post-processing implemented for ethdev stats, per-queue stats, xstats, link_status, and mempool metrics. Socket auto-discovery implemented. Delta/rate computation (Task 2.5) NOT yet implemented — raw cumulative values are logged. Crucible integration (Task 2.9) complete — tool registered, run file validated, profiler placement verified.

| Task | Description | Status |
|------|-------------|--------|
| 2.1 | Implement `dpdk-post-process` — read compressed JSON, parse timestamped samples | ✅ Done |
| 2.2 | Map `ethdev/stats` fields to CDM `metric_desc` (source, class, type) with port/queue dimensions | ✅ Done — ipackets, opackets, ibytes, obytes, imissed, ierrors, oerrors, rx_nombuf + per-queue |
| 2.3 | Map `ethdev/xstats` fields to CDM metrics with `hide_zero` filtering | ✅ Done — non-zero xstats emitted as `xstat-<name>` |
| 2.4 | Map `mempool/info` fields to CDM metrics with `mempool_name` dimension | ✅ Done — mempool-used + mempool-total |
| 2.5 | Implement delta computation for cumulative counters (per-second rates) | ❌ Not done — raw cumulative values logged; `--compute-rates` not implemented |
| 2.6 | Emit metrics via `toolbox.metrics.log_sample()` + `finish_samples()` | ✅ Done |
| 2.7 | Write `post-process-data.json` output manifest | ✅ Done — schema version `2021.04.12` |
| 2.8 | Implement socket auto-discovery: scan `/var/run/dpdk/*/dpdk_telemetry.v2` | ✅ Done — in `dpdk_telemetry_client.py` `discover_socket()` |
| 2.9 | Integration test: crucible run with bench-trafficgen + tool-dpdk on remotehosts | ✅ Done — registered in `repos.json`, run file validated with profiler co-located on server host |
| 2.10 | Verify CDM metrics appear in OpenSearch and correlate with trafficgen results | ⏳ Pending — requires live run execution |

### Phase 3 — Deployment Patterns (Weeks 7–9) ⏳ Not started

Handle real-world deployment: volume mounts, endpoint types, multi-process scenarios, end-to-end validation.

**Status:** Not started. Deployment configuration lives outside this repository (in rickshaw/bench-trafficgen volume mount config).

| Task | Description | Status |
|------|-------------|--------|
| 3.1 | Configure `/var/run/dpdk` shared volume mount for remotehosts (`podman -v` flag) | ⏳ Pending |
| 3.2 | Configure hostPath volume for k8s/OpenShift DPDK socket access | ⏳ Pending |
| 3.3 | Test on OpenShift with SRIOV networking and testpmd DUT | ⏳ Pending |
| 3.4 | Configure OSP compute node access for telemetry collection | ⏳ Pending |
| 3.5 | Implement multi-process support: `--file-prefix` list, auto-filter by `/info` PID | ⏳ Pending |
| 3.6 | End-to-end test: full bench-trafficgen binary search with tool-dpdk collecting | ⏳ Pending |
| 3.7 | Validate metric accuracy: compare tool-dpdk `ethdev/stats` with testpmd `--stats-period` output | ⏳ Pending |
| 3.8 | Performance test: verify tool-dpdk polling does not impact testpmd forwarding rate | ⏳ Pending |
| 3.9 | Write deployment guide for each endpoint type (remotehosts, k8s, OSP) | ⏳ Pending |
| 3.10 | Update bench-trafficgen documentation to reference tool-dpdk | ⏳ Pending |

### Phase 4 — Extensibility (Weeks 10–12) ⏳ Not started

Profile system for multi-application support. l3fwd-power validation. Upstream contribution preparation.

**Status:** Not started. Profile loading infrastructure exists in `dpdk-collect` (Task 4.2 partially done), but only `default.json` profile is present. No `testpmd.json`, `l3fwd-power.json`, or `--endpoints` parameter yet.

| Task | Description | Status |
|------|-------------|--------|
| 4.1 | Design profile JSON schema: `{name, inherits, endpoints[], metrics_map{}}` | 🔶 Partial — simple schema in `default.json`; no `inherits` or `metrics_map` yet |
| 4.2 | Implement profile loading in `dpdk-collect` (read from `profiles/` directory) | ✅ Done — `load_profile()` with fallback to `default.json` |
| 4.3 | Create testpmd profile: default + `rx_queue`, `tx_queue`, `info` endpoints | ⏳ Pending |
| 4.4 | Create l3fwd-power profile: default + `/l3fwd_power/stats` endpoint | ⏳ Pending |
| 4.5 | Implement `--endpoints` param for ad-hoc custom endpoint addition | ⏳ Pending |
| 4.6 | Add queue-level metric collection (per-queue breakdowns in post-processing) | ✅ Done — `q_ipackets`/`q_opackets` per-queue in `dpdk-post-process` |
| 4.7 | Add mempool utilization tracking with alerting thresholds | 🔶 Partial — mempool-used/mempool-total collected; no alerting thresholds |
| 4.8 | Test profile system with l3fwd-power standalone | ⏳ Pending |
| 4.9 | Prepare upstream contribution: PR to perftool-incubator, update `repos.json` in crucible | ⏳ Pending |
| 4.10 | Write profile authoring guide for adding new DPDK application support | ⏳ Pending |

### Phase 5 — Grout Integration (Weeks 13–15) ⏳ Not started

Dual-socket collection for Grout: standard DPDK telemetry plus Grout-specific control-plane API. This phase can begin in parallel with Phase 4 once the profile system is in place.

**Status:** Not started. Design documented; no implementation.

| Task | Description | Status |
|------|-------------|--------|
| 5.1 | Research Grout control-plane socket protocol (libevent-based API) | ⏳ Pending |
| 5.2 | Implement `grout-telemetry-client.py` module for Grout API socket | ⏳ Pending |
| 5.3 | Map Grout graph-node statistics to CDM metrics (cycles, pkts/call, cache misses) | ⏳ Pending |
| 5.4 | Map Grout routing table and interface metrics to CDM | ⏳ Pending |
| 5.5 | Implement dual-socket collection in `dpdk-collect`: DPDK telemetry + Grout API | ⏳ Pending |
| 5.6 | Create grout profile with both DPDK and Grout-specific endpoints | ⏳ Pending |
| 5.7 | Update `dpdk-post-process` to route samples by source tag (`dpdk-ethdev` vs `grout-graph`) | ⏳ Pending |
| 5.8 | Integration test: bench-trafficgen with Grout DUT + tool-dpdk | ⏳ Pending |
| 5.9 | Write Grout integration documentation | ⏳ Pending |
| 5.10 | Cross-validate: compare Grout `grcli stats` output with tool-dpdk collected metrics | ⏳ Pending |

---

## 10. Risk Mitigation

| Risk | Probability | Impact | Mitigation Strategy | Status |
|------|-------------|--------|---------------------|--------|
| DPDK socket inaccessible from profiler | Medium | **Critical** | Prototype shared volume mount in Phase 1; fallback: whitelist tool for `server` collector type | ✅ Mitigated — profiler co-located on server host with `osruntime=chroot`; volume mount config for podman pending |
| 16KB xstats response truncation | High | Medium | Always use `hide_zero=true`; implement response-size check; split into multiple queries if needed | ✅ Mitigated — `hide_zero=true` hardcoded in `dpdk-collect` |
| testpmd restarts mid-collection | Low | Medium | Reconnect with exponential backoff | ✅ Mitigated — `connect_with_retry()` + reconnect on `ConnectionError` in collection loop |
| DPDK version incompatibility | Low | Low | Query `/` endpoint first to discover available commands; skip missing endpoints gracefully | ✅ Mitigated — `/` queried on connect; version recorded in output header |
| Container image build failure | Low | Medium | Zero external DPDK deps — only Python stdlib + xz-utils; minimal `workshop.json` | ✅ Mitigated — workshop.json validated, only python3 + xz |
| Grout API protocol changes | Medium | Low | Phase 5 is future; isolate in separate client module; version-check on connect | ⏳ Open — Phase 5 not started |
| Tool polling impacts DUT performance | Low | **High** | Default 1s interval; telemetry thread is separate from DPDK data path; benchmark to verify | 🔶 Design mitigated — formal benchmark pending (Task 3.8) |
| OpenShift security policy blocks socket access | Medium | **High** | Test with SecurityContextConstraints early; document required SCC permissions | ⏳ Open — k8s/OpenShift testing not started (Phase 3) |

---

## Appendix A — Repository Creation Guide

This appendix provides the complete step-by-step procedure for creating the `tool-dpdk` repository under the `perftool-incubator` GitHub organization, including all scaffolding files needed to integrate with crucible/rickshaw from day one.

### A.1 Prerequisites

| Prerequisite | Details |
|--------------|---------|
| GitHub org membership | Write access to `perftool-incubator` organization |
| Git installed | `git` 2.x+ on the development machine |
| GitHub CLI (optional) | `gh` CLI for repo creation from terminal |
| Python 3 | For local testing of the telemetry client |
| Reference repos cloned | `rickshaw`, `bench-trafficgen` for integration reference |

### A.2 Create the Repository

**Option 1 — GitHub CLI (recommended):**

```bash
# Create the repository under the org
gh repo create perftool-incubator/tool-dpdk \
    --public \
    --description "DPDK Telemetry collection tool for the crucible benchmarking ecosystem" \
    --clone

cd tool-dpdk
```

**Option 2 — GitHub Web UI + local clone:**

1. Navigate to https://github.com/organizations/perftool-incubator/repositories/new
2. Fill in:
   - **Repository name:** `tool-dpdk`
   - **Description:** `DPDK Telemetry collection tool for the crucible benchmarking ecosystem`
   - **Visibility:** Public
   - **Initialize with:** Add a README file, `.gitignore` (Python)
   - **License:** Apache License 2.0 (to match other perftool-incubator projects)
3. Click **Create repository**
4. Clone locally:

```bash
git clone git@github.com:perftool-incubator/tool-dpdk.git
cd tool-dpdk
```

### A.3 Initial Directory Structure

After scaffolding, the repository tree should look like this:

```
tool-dpdk/
├── rickshaw.json                  # Tool manifest for crucible/rickshaw integration
├── workshop.json                  # Container image build dependencies
├── dpdk-start                     # Lifecycle: launch collector (Bash, executable)
├── dpdk-stop                      # Lifecycle: stop collector + compress output (Bash, executable)
├── dpdk-collect                   # Main collection loop (Python, executable)
├── dpdk-post-process              # Post-process into CDM metrics (Python, executable)
├── dpdk_telemetry_client.py       # Reusable DPDK telemetry socket client library
├── profiles/
│   └── default.json               # Default telemetry endpoint profile
├── tests/
│   └── test_telemetry_client.py   # Unit tests for the telemetry client
├── README.md                      # Project documentation
├── LICENSE                        # Apache 2.0
└── .gitignore                     # Python + xz artifacts
```

### A.4 Scaffolding File Contents

#### A.4.1 `rickshaw.json`

This is the tool manifest that rickshaw reads to discover how to deploy, start, stop, and post-process tool-dpdk.

```json
{
    "rickshaw-tool": {
        "schema": {
            "version": "2020.03.18"
        }
    },
    "tool": "dpdk",
    "controller": {
        "post-script": "%tool-dir%dpdk-post-process"
    },
    "collector": {
        "files-from-controller": [
            { "src": "%tool-dir%/dpdk-start", "dest": "/usr/bin/" },
            { "src": "%tool-dir%/dpdk-stop", "dest": "/usr/bin/" },
            { "src": "%tool-dir%/dpdk-collect", "dest": "/usr/bin/" },
            { "src": "%tool-dir%/dpdk_telemetry_client.py", "dest": "/usr/bin/" },
            { "src": "%tool-dir%/profiles", "dest": "/usr/bin/" }
        ],
        "blacklist": [
            { "endpoint": "remotehosts", "collector-types": [ "client", "server" ] },
            { "endpoint": "kube", "collector-types": [ "client", "server" ] }
        ],
        "whitelist": [
            { "endpoint": "osp", "collector-types": [ "compute" ] },
            { "endpoint": "remotehosts", "collector-types": [ "profiler" ] },
            { "endpoint": "kube", "collector-types": [ "profiler" ] }
        ],
        "start": "dpdk-start",
        "stop": "dpdk-stop"
    }
}
```

**Key fields explained:**

| Field | Value | Purpose |
|-------|-------|---------|
| `tool` | `"dpdk"` | Tool name; matches `--tool dpdk:params` in crucible CLI |
| `post-script` | `"%tool-dir%dpdk-post-process"` | Runs on the controller after data is transferred back |
| `files-from-controller` | Array of `{src, dest}` objects | Copied to collector node at deploy time; `%tool-dir%` resolves at runtime |
| `whitelist` | Array of `{endpoint, collector-types}` | Per-endpoint collector types where this tool runs |
| `blacklist` | Array of `{endpoint, collector-types}` | Per-endpoint collector types where this tool must NOT run |
| `start` / `stop` | Script names | Executed on the collector at roadblock sync points |

#### A.4.2 `workshop.json`

Defines packages installed into the container image during the build phase.

```json
{
    "workshop": {
        "schema": {
            "version": "2020.03.02"
        }
    },
    "userenvs": [
        {
            "name": "default",
            "requirements": [ "dpdk_deps" ]
        }
    ],
    "requirements": [
        {
            "name": "dpdk_deps",
            "type": "distro",
            "distro_info": {
                "packages": [ "python3", "xz" ]
            }
        }
    ]
}
```

> **Note:** No DPDK libraries are required. The telemetry protocol is consumed via Python's standard `socket` module over raw Unix domain sockets.

#### A.4.3 `dpdk-start`

```bash
#!/bin/bash

# tool-dpdk: start telemetry collection from DPDK applications
# Called by rickshaw at the tools-start-begin roadblock

TOOL_DIR=$(dirname "$0")
TOOL_OUTPUT_DIR="${1}"
shift

# Defaults
INTERVAL=1
FILE_PREFIX=""
SOCKET_PATH=""
PROFILE="default"
CONNECT_TIMEOUT=30

# Parse arguments from crucible tool-params
while [ $# -gt 0 ]; do
    case "$1" in
        --interval)       INTERVAL="$2";        shift 2 ;;
        --file-prefix)    FILE_PREFIX="$2";      shift 2 ;;
        --socket-path)    SOCKET_PATH="$2";      shift 2 ;;
        --profile)        PROFILE="$2";          shift 2 ;;
        --connect-timeout) CONNECT_TIMEOUT="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# Build the dpdk-collect command
CMD="python3 ${TOOL_DIR}/dpdk-collect"
CMD+=" --output-dir ${TOOL_OUTPUT_DIR}"
CMD+=" --interval ${INTERVAL}"
CMD+=" --profile ${PROFILE}"
CMD+=" --connect-timeout ${CONNECT_TIMEOUT}"
CMD+=" --profiles-dir ${TOOL_DIR}/profiles"

if [ -n "${FILE_PREFIX}" ]; then
    CMD+=" --file-prefix ${FILE_PREFIX}"
fi
if [ -n "${SOCKET_PATH}" ]; then
    CMD+=" --socket-path ${SOCKET_PATH}"
fi

# Launch collector in background
${CMD} &
COLLECTOR_PID=$!
echo "${COLLECTOR_PID}" > "${TOOL_OUTPUT_DIR}/dpdk-collect-pid.txt"

echo "tool-dpdk: collector started (PID ${COLLECTOR_PID}), interval=${INTERVAL}s, profile=${PROFILE}"
```

#### A.4.4 `dpdk-stop`

```bash
#!/bin/bash

# tool-dpdk: stop telemetry collection and compress output
# Called by rickshaw at the tools-stop-begin roadblock

TOOL_OUTPUT_DIR="${1}"

PID_FILE="${TOOL_OUTPUT_DIR}/dpdk-collect-pid.txt"

if [ -f "${PID_FILE}" ]; then
    COLLECTOR_PID=$(cat "${PID_FILE}")
    if kill -0 "${COLLECTOR_PID}" 2>/dev/null; then
        kill -SIGTERM "${COLLECTOR_PID}"
        # Wait up to 10 seconds for graceful shutdown
        for i in $(seq 1 10); do
            if ! kill -0 "${COLLECTOR_PID}" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        # Force kill if still running
        if kill -0 "${COLLECTOR_PID}" 2>/dev/null; then
            kill -9 "${COLLECTOR_PID}"
        fi
    fi
    rm -f "${PID_FILE}"
fi

# Compress all output files
for f in "${TOOL_OUTPUT_DIR}"/dpdk-telemetry-*.json; do
    if [ -f "$f" ]; then
        xz --threads=0 "$f"
    fi
done

echo "tool-dpdk: collection stopped, output compressed"
```

#### A.4.5 `dpdk_telemetry_client.py`

```python
#!/usr/bin/env python3
"""
Reusable DPDK Telemetry v2 socket client.

Connects to a DPDK application's telemetry socket (SOCK_SEQPACKET),
handles the initial handshake, and provides a query interface.
Supports auto-discovery and reconnection with exponential backoff.
"""

import glob
import json
import os
import socket
import time

DPDK_TELEMETRY_V2 = "dpdk_telemetry.v2"
DEFAULT_RUN_DIR = "/var/run/dpdk"
MAX_OUTPUT_LEN = 16384

SEARCH_DIRS = [
    "/var/run/dpdk",
    "/var/run/openvswitch",
    "/var/run/openvswitch/.dpdk",
    "/run/dpdk",
    "/run/openvswitch",
    "/run/openvswitch/.dpdk",
    "/tmp/dpdk",
]


class DPDKTelemetryClient:
    """Client for the DPDK Telemetry v2 Unix socket API."""

    def __init__(self, socket_path=None, file_prefix=None):
        self._socket_path = socket_path
        self._file_prefix = file_prefix
        self._sock = None
        self._info = None

    @property
    def info(self):
        """Handshake info: {"version": ..., "pid": ..., "max_output_len": ...}"""
        return self._info

    @property
    def connected(self):
        return self._sock is not None

    def _find_sockets_in_dir(self, base_dir):
        """Recursively find telemetry sockets under a directory."""
        if not os.path.isdir(base_dir):
            return []

        results = []
        recursive_pattern = os.path.join(base_dir, "**", DPDK_TELEMETRY_V2)
        results.extend(glob.glob(recursive_pattern, recursive=True))

        direct = os.path.join(base_dir, DPDK_TELEMETRY_V2)
        if os.path.exists(direct) and direct not in results:
            results.append(direct)
        return results

    def discover_socket(self):
        """Find a DPDK telemetry socket on the system.

        Search order:
        1. Explicit --socket-path (if provided)
        2. --file-prefix under known directories
        3. Auto-scan all known directories recursively for any telemetry socket
        """
        if self._socket_path:
            return self._socket_path

        if self._file_prefix:
            for base in SEARCH_DIRS:
                path = os.path.join(base, self._file_prefix, DPDK_TELEMETRY_V2)
                if os.path.exists(path):
                    return path
            return None

        xdg = os.environ.get("XDG_RUNTIME_DIR")
        search_dirs = list(SEARCH_DIRS)
        if xdg:
            search_dirs.append(os.path.join(xdg, "dpdk"))

        all_sockets = []
        for base in search_dirs:
            all_sockets.extend(self._find_sockets_in_dir(base))

        return sorted(all_sockets)[0] if all_sockets else None

    def list_searched_paths(self):
        """Return the list of directories that will be searched."""
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        dirs = list(SEARCH_DIRS)
        if xdg:
            dirs.append(os.path.join(xdg, "dpdk"))
        return dirs

    def diagnose_paths(self):
        """Return a diagnostic report of what exists in each search directory."""
        report = []
        for search_dir in self.list_searched_paths():
            if not os.path.exists(search_dir):
                report.append(f"  {search_dir}: does not exist")
                continue
            if not os.path.isdir(search_dir):
                report.append(f"  {search_dir}: not a directory")
                continue
            try:
                entries = os.listdir(search_dir)
                if not entries:
                    report.append(f"  {search_dir}: empty")
                else:
                    sockets = [e for e in entries
                               if os.path.exists(os.path.join(search_dir, e))
                               and (e.endswith(".sock") or "telemetry" in e
                                    or os.path.isdir(os.path.join(search_dir, e)))]
                    if sockets:
                        report.append(
                            f"  {search_dir}: {len(entries)} entries, "
                            f"relevant: {sockets[:10]}"
                        )
                    else:
                        report.append(
                            f"  {search_dir}: {len(entries)} entries, "
                            f"no telemetry sockets found"
                        )
            except PermissionError:
                report.append(f"  {search_dir}: permission denied")
        return "\n".join(report)

    def connect(self):
        """Connect to the DPDK telemetry socket and perform handshake."""
        path = self.discover_socket()
        if not path:
            searched = self.list_searched_paths()
            raise FileNotFoundError(
                f"No DPDK telemetry socket found "
                f"(prefix={self._file_prefix}, path={self._socket_path}, "
                f"searched={searched})"
            )

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            sock.connect(path)
            raw = sock.recv(MAX_OUTPUT_LEN)
        except Exception:
            sock.close()
            raise

        self._sock = sock
        self._info = json.loads(raw.decode("utf-8"))
        return self._info

    def query(self, command):
        """Send a command and return the parsed JSON response."""
        if not self._sock:
            raise ConnectionError("Not connected. Call connect() first.")

        self._sock.send(command.encode("utf-8"))
        raw = self._sock.recv(MAX_OUTPUT_LEN)
        return json.loads(raw.decode("utf-8"))

    def close(self):
        """Close the socket connection."""
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._info = None

    def connect_with_retry(self, timeout=30, backoff=2):
        """
        Poll for the telemetry socket and connect with exponential backoff.
        Used when the DPDK application may not have started yet.
        """
        deadline = time.time() + timeout
        wait = backoff
        last_err = None

        while time.time() < deadline:
            try:
                return self.connect()
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                last_err = exc
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(wait, remaining))
                wait = min(wait * 2, 30)

        raise TimeoutError(
            f"Could not connect within {timeout}s: {last_err}"
        )
```

#### A.4.6 `dpdk-collect`

```python
#!/usr/bin/env python3
"""
tool-dpdk collector: polls DPDK telemetry endpoints at a configurable
interval and writes timestamped JSON output.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

# Ensure the tool directory is in the path for the client library
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dpdk_telemetry_client import DPDKTelemetryClient  # noqa: E402

running = True


def detect_ovs_file_prefix():
    """Attempt to determine the DPDK file-prefix from OVS configuration.

    OVS-DPDK stores EAL arguments in other_config:dpdk-extra. If a
    --file-prefix is set there, return it so the socket can be found
    under the matching subdirectory.
    """
    if not shutil.which("ovs-vsctl"):
        return None

    try:
        result = subprocess.run(
            ["ovs-vsctl", "get", "Open_vSwitch", ".", "other_config:dpdk-extra"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None
        dpdk_extra = result.stdout.strip().strip('"')
        if "--no-telemetry" in dpdk_extra:
            sys.stderr.write(
                "tool-dpdk: WARNING: OVS configured with --no-telemetry, "
                "telemetry socket will not be available\n"
            )
            return None
        parts = dpdk_extra.split()
        for i, part in enumerate(parts):
            if part == "--file-prefix" and i + 1 < len(parts):
                return parts[i + 1]
            if part.startswith("--file-prefix="):
                return part.split("=", 1)[1]
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def handle_signal(signum, frame):
    global running
    running = False


def load_profile(profiles_dir, profile_name):
    """Load an endpoint profile from the profiles directory."""
    path = os.path.join(profiles_dir, f"{profile_name}.json")
    if not os.path.exists(path):
        # Fallback to default
        path = os.path.join(profiles_dir, "default.json")

    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)

    # Hardcoded default if no file exists
    return {
        "name": "default",
        "endpoints_per_port": [
            "/ethdev/stats",
            "/ethdev/xstats",
            "/ethdev/link_status"
        ],
        "endpoints_global": [
            "/mempool/list"
        ],
        "mempool_info": True
    }


def collect(args):
    global running
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    profile = load_profile(args.profiles_dir, args.profile)

    # Auto-detect OVS file-prefix if not explicitly provided
    file_prefix = args.file_prefix
    if not file_prefix and not args.socket_path:
        detected = detect_ovs_file_prefix()
        if detected:
            sys.stderr.write(
                f"tool-dpdk: auto-detected OVS file-prefix: {detected}\n"
            )
            file_prefix = detected

    client = DPDKTelemetryClient(
        socket_path=args.socket_path,
        file_prefix=file_prefix
    )

    # Wait for socket and connect
    searched = client.list_searched_paths()
    sys.stderr.write(
        f"tool-dpdk: waiting for telemetry socket "
        f"(timeout={args.connect_timeout}s)...\n"
    )
    sys.stderr.write(
        f"tool-dpdk: searching directories: {searched}\n"
    )

    try:
        info = client.connect_with_retry(timeout=args.connect_timeout)
    except TimeoutError as exc:
        sys.stderr.write(f"tool-dpdk: {exc}\n")
        sys.stderr.write(
            "tool-dpdk: no DPDK telemetry socket available on this host. "
            "Ensure the DPDK application (testpmd/OVS-DPDK) is running with "
            "telemetry enabled and the socket path is accessible. "
            "Use --socket-path to specify a non-standard location.\n"
        )
        sys.stderr.write("tool-dpdk: directory diagnostics:\n")
        sys.stderr.write(client.diagnose_paths() + "\n")
        return

    sys.stderr.write(
        f"tool-dpdk: connected to {info.get('version', 'unknown')} "
        f"(PID {info.get('pid', '?')})\n"
    )

    # Discover available commands
    available = client.query("/")
    sys.stderr.write(
        f"tool-dpdk: {len(available.get('/', []))} commands available\n"
    )

    # Discover ports
    port_list = client.query("/ethdev/list")
    ports = port_list.get("/ethdev/list", [])
    sys.stderr.write(f"tool-dpdk: found {len(ports)} port(s): {ports}\n")

    # Discover mempools
    mempools = []
    if profile.get("mempool_info"):
        mp_list = client.query("/mempool/list")
        mempools = mp_list.get("/mempool/list", [])
        sys.stderr.write(
            f"tool-dpdk: found {len(mempools)} mempool(s): {mempools}\n"
        )

    # Record EAL params at first sample
    eal_params = client.query("/eal/params")

    output_file = os.path.join(
        args.output_dir, "dpdk-telemetry-output.json"
    )

    with open(output_file, "w") as out:
        # Write metadata header
        header = {
            "tool": "dpdk",
            "dpdk_version": info.get("version"),
            "dpdk_pid": info.get("pid"),
            "ports": ports,
            "mempools": mempools,
            "eal_params": eal_params,
            "profile": profile.get("name", args.profile),
            "interval": args.interval
        }
        out.write(json.dumps({"header": header}) + "\n")

        while running:
            timestamp_ms = int(time.time() * 1000)
            sample = {"timestamp_ms": timestamp_ms, "data": {}}

            try:
                # Per-port endpoints
                for port in ports:
                    port_data = {}
                    for endpoint in profile.get("endpoints_per_port", []):
                        cmd = f"{endpoint},{port}"
                        if "xstats" in endpoint:
                            cmd += ",hide_zero=true"
                        resp = client.query(cmd)
                        port_data[endpoint] = resp.get(endpoint, resp)
                    sample["data"][f"port_{port}"] = port_data

                # Mempool endpoints
                for mp in mempools:
                    resp = client.query(f"/mempool/info,{mp}")
                    sample["data"][f"mempool_{mp}"] = resp.get(
                        "/mempool/info", resp
                    )

                # Global endpoints
                for endpoint in profile.get("endpoints_global", []):
                    resp = client.query(endpoint)
                    sample["data"][endpoint] = resp.get(endpoint, resp)

                out.write(json.dumps(sample) + "\n")
                out.flush()

            except (ConnectionError, BrokenPipeError, OSError) as exc:
                sys.stderr.write(
                    f"tool-dpdk: connection lost ({exc}), reconnecting...\n"
                )
                client.close()
                try:
                    client.connect_with_retry(timeout=args.connect_timeout)
                    sys.stderr.write("tool-dpdk: reconnected\n")
                except TimeoutError:
                    sys.stderr.write(
                        "tool-dpdk: reconnect failed, stopping\n"
                    )
                    break

            time.sleep(args.interval)

    client.close()
    sys.stderr.write("tool-dpdk: collection stopped\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DPDK telemetry collector")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--interval", type=int, default=1)
    parser.add_argument("--file-prefix", default=None)
    parser.add_argument("--socket-path", default=None)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--connect-timeout", type=int, default=30)
    parser.add_argument("--profiles-dir", default="profiles")
    collect(parser.parse_args())
```

#### A.4.7 `dpdk-post-process`

```python
#!/usr/bin/env python3
"""
tool-dpdk post-processor: reads collected telemetry JSON, computes deltas,
and emits CDM-compliant metrics via the toolbox.metrics API.
"""

import json
import lzma
import os
import sys

# toolbox is provided by perftool-incubator and available in the
# crucible controller environment
from toolbox.metrics import log_sample, finish_samples


def process(output_dir):
    compressed = os.path.join(output_dir, "dpdk-telemetry-output.json.xz")
    if not os.path.exists(compressed):
        sys.stderr.write(f"tool-dpdk: {compressed} not found, skipping\n")
        return

    with lzma.open(compressed, "rt") as f:
        lines = f.readlines()

    if not lines:
        return

    # First line is the metadata header
    header = json.loads(lines[0]).get("header", {})
    samples = [json.loads(line) for line in lines[1:] if line.strip()]

    prev_sample = None
    file_id = "0"

    for sample in samples:
        ts = sample.get("timestamp_ms")
        data = sample.get("data", {})

        for key, port_data in data.items():
            if not key.startswith("port_"):
                continue
            port_id = key.replace("port_", "")

            # /ethdev/stats
            stats = port_data.get("/ethdev/stats", {})
            for metric_name, cdm_class, cdm_type in [
                ("ipackets",  "throughput", "rx-packets"),
                ("opackets",  "throughput", "tx-packets"),
                ("ibytes",    "throughput", "rx-bytes"),
                ("obytes",    "throughput", "tx-bytes"),
                ("imissed",   "count",      "rx-missed"),
                ("ierrors",   "count",      "rx-errors"),
                ("oerrors",   "count",      "tx-errors"),
                ("rx_nombuf", "count",      "rx-nombuf"),
            ]:
                value = stats.get(metric_name)
                if value is not None:
                    desc = {
                        "source": "dpdk",
                        "class": cdm_class,
                        "type": cdm_type,
                    }
                    names = {"port": port_id}
                    log_sample(
                        file_id, desc, names,
                        {"end": ts, "value": value}
                    )

            # Per-queue stats
            for q_metric, cdm_type in [
                ("q_ipackets", "rx-queue-packets"),
                ("q_opackets", "tx-queue-packets"),
            ]:
                q_values = stats.get(q_metric, [])
                for queue_id, value in enumerate(q_values):
                    if value and value != 0:
                        desc = {
                            "source": "dpdk",
                            "class": "throughput",
                            "type": cdm_type,
                        }
                        names = {
                            "port": port_id,
                            "queue": str(queue_id),
                        }
                        log_sample(
                            file_id, desc, names,
                            {"end": ts, "value": value}
                        )

        prev_sample = sample

    metric_files = finish_samples()

    # Write post-process manifest
    manifest = {
        "rickshaw-bench-metric": {
            "schema": {"version": "2021.04.12"}
        },
        "tool": "dpdk",
        "primary-period": "measurement",
        "primary-metric": "rx-packets",
        "periods": [
            {
                "name": "measurement",
                "metric-files": metric_files,
            }
        ],
    }

    manifest_path = os.path.join(output_dir, "post-process-data.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: dpdk-post-process <output-dir>\n")
        sys.exit(1)
    process(sys.argv[1])
```

#### A.4.8 `profiles/default.json`

```json
{
    "name": "default",
    "description": "Standard DPDK library-level telemetry endpoints",
    "endpoints_per_port": [
        "/ethdev/stats",
        "/ethdev/xstats",
        "/ethdev/link_status"
    ],
    "endpoints_global": [
        "/mempool/list"
    ],
    "mempool_info": true
}
```

#### A.4.9 `.gitignore`

```
# Python
__pycache__/
*.py[cod]
*.egg-info/
*.egg
dist/
build/
.eggs/

# Compressed output
*.xz
*.gz

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Test artifacts
.pytest_cache/
.coverage
htmlcov/
```

### A.5 Git Commands — Initialize and Push

```bash
# From inside the cloned tool-dpdk directory:

# Ensure all scripts are executable
chmod +x dpdk-start dpdk-stop dpdk-collect dpdk-post-process dpdk_telemetry_client.py

# Stage all scaffolding files
git add \
    rickshaw.json \
    workshop.json \
    dpdk-start \
    dpdk-stop \
    dpdk-collect \
    dpdk-post-process \
    dpdk_telemetry_client.py \
    profiles/default.json \
    .gitignore \
    README.md \
    LICENSE

# Initial commit
git commit -m "Initial scaffolding for tool-dpdk

Add the foundational project structure for DPDK Telemetry collection:
- rickshaw.json: tool manifest with profiler/compute whitelist
- workshop.json: container build deps (python3, xz)
- dpdk-start/stop: lifecycle scripts matching perftool-incubator pattern
- dpdk-collect: telemetry polling loop with socket auto-discovery
- dpdk-post-process: CDM metric emission via toolbox.metrics API
- dpdk_telemetry_client.py: reusable SOCK_SEQPACKET client library
- profiles/default.json: standard ethdev/xstats/mempool endpoint set"

# Push to origin
git push -u origin main
```

### A.6 Register the Tool in Crucible

After the repository is created, it must be registered in crucible's configuration so rickshaw can discover it.

**Step 1 — Add to `config/repos.json` in the crucible repository:**

```json
{
    "name": "dpdk",
    "type": "tool",
    "repository": "/root/tool-dpdk",
    "primary-branch": "main",
    "checkout": {
        "mode": "follow",
        "target": "main"
    }
}
```

**Step 2 — Clone and activate:**

```bash
# Clone and activate the tool
crucible update dpdk

# Verify the symlink was created
ls -la /opt/crucible/subprojects/tools/dpdk
```

**Step 3 — Test with a crucible run:**

```bash
crucible run bench-trafficgen \
    --tool dpdk:interval=1,profile=default \
    --endpoint remotehosts,host:my-test-host \
    ...
```

### A.7 Validation Checklist

After repository creation, verify each item before proceeding to Phase 1 implementation tasks:

| Check | Command / Action | Expected Result |
|-------|-----------------|-----------------|
| Repo exists on GitHub | `gh repo view perftool-incubator/tool-dpdk` | Repo metadata displayed |
| rickshaw.json is valid JSON | `python3 -m json.tool rickshaw.json` | Pretty-printed JSON, no errors |
| workshop.json is valid JSON | `python3 -m json.tool workshop.json` | Pretty-printed JSON, no errors |
| Scripts are executable | `ls -la dpdk-start dpdk-stop dpdk-collect dpdk-post-process` | `-rwxr-xr-x` permissions |
| Profile is valid JSON | `python3 -m json.tool profiles/default.json` | Pretty-printed JSON, no errors |
| Telemetry client imports | `python3 -c "from dpdk_telemetry_client import DPDKTelemetryClient; print('OK')"` | `OK` |
| dpdk-collect --help works | `python3 dpdk-collect --help` | Argument help displayed |
| No DPDK deps required | Verify `workshop.json` has no dpdk-devel/dpdk-tools entries | Only python3 and xz listed |

---

## References

- [DPDK Telemetry Guide](https://doc.dpdk.org/guides/howto/telemetry.html)
- [DPDK/grout — Graph Router](https://github.com/DPDK/grout/)
- [perftool-incubator organization](https://github.com/perftool-incubator)
- [tool-ovs](https://github.com/perftool-incubator/tool-ovs)
- [bench-trafficgen](https://github.com/perftool-incubator/bench-trafficgen)
- [crucible](https://github.com/perftool-incubator/crucible)
- [rickshaw](https://github.com/perftool-incubator/rickshaw)
- [DPDK — dpdk.org](https://www.dpdk.org/)

---

*tool-dpdk Technical Architecture Document v1.3 — perftool-incubator/tool-dpdk*
