#!/usr/bin/env python3
# -*- mode: python; indent-tabs-mode: nil; python-indent-level: 4 -*-
# vim: autoindent tabstop=4 shiftwidth=4 expandtab softtabstop=4 filetype=python
"""Regression tests for dpdk-post-process: multi-instance source naming,
queue dimension handling, and input/output path separation."""

import json
import lzma
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SAMPLE_HEADER = {
    "header": {
        "tool": "dpdk",
        "dpdk_version": "DPDK 23.11.0",
        "dpdk_pid": 12345,
        "ports": [0],
        "port_info": {"0": {"name": "0000:4b:00.0", "driver": "net_ice"}},
        "mempools": ["mb_pool_0"],
        "eal_params": {"/eal/params": "-n 4"},
        "profile": "default",
        "interval": 1,
    }
}

SAMPLE_DATA_T1 = {
    "timestamp_ms": 1700000000000,
    "data": {
        "port_0": {
            "/ethdev/stats": {
                "ipackets": 1000,
                "opackets": 990,
                "ibytes": 64000,
                "obytes": 63360,
                "imissed": 0,
                "ierrors": 0,
                "oerrors": 0,
                "rx_nombuf": 0,
            },
            "/ethdev/link_status": {"status": "UP", "speed": 25000},
            "/ethdev/info": {"speed": 25000},
            "/ethdev/xstats": {"rx_good_packets": 1000, "tx_good_packets": 990},
        },
        "mempool_mb_pool_0": {"count": 4096, "size": 8192},
    },
}

SAMPLE_DATA_T2 = {
    "timestamp_ms": 1700000001000,
    "data": {
        "port_0": {
            "/ethdev/stats": {
                "ipackets": 2000,
                "opackets": 1990,
                "ibytes": 128000,
                "obytes": 127360,
                "imissed": 0,
                "ierrors": 0,
                "oerrors": 0,
                "rx_nombuf": 0,
            },
            "/ethdev/link_status": {"status": "UP", "speed": 25000},
            "/ethdev/info": {"speed": 25000},
            "/ethdev/xstats": {"rx_good_packets": 2000, "tx_good_packets": 1990},
        },
        "mempool_mb_pool_0": {"count": 4100, "size": 8192},
    },
}


def write_telemetry_xz(output_dir, header, *samples):
    """Write a compressed JSONL telemetry file."""
    path = os.path.join(output_dir, "dpdk-telemetry-output.json.xz")
    lines = [json.dumps(header) + "\n"]
    for s in samples:
        lines.append(json.dumps(s) + "\n")
    with lzma.open(path, "wt") as f:
        f.writelines(lines)


def write_engine_env(output_dir, tool_name, extra_vars=None):
    """Write an engine-env.txt file with tool_name and optional extras."""
    path = os.path.join(output_dir, "engine-env.txt")
    lines = []
    if extra_vars:
        for k, v in extra_vars.items():
            lines.append(f"{k}={v}\n")
    lines.append(f"tool_name={tool_name}\n")
    with open(path, "w") as f:
        f.writelines(lines)


def import_post_process():
    """Import dpdk-post-process with toolbox.metrics mocked."""
    import importlib.util
    import types

    mock_metrics = types.ModuleType("toolbox.metrics")
    mock_metrics.log_sample = mock.MagicMock()
    mock_metrics.finish_samples = mock.MagicMock(return_value=[])

    mock_toolbox = types.ModuleType("toolbox")
    mock_toolbox.metrics = mock_metrics

    mod_name = "dpdk_post_process"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    saved = {}
    for key in ("toolbox", "toolbox.metrics"):
        saved[key] = sys.modules.get(key)
        sys.modules[key] = mock_toolbox if key == "toolbox" else mock_metrics

    try:
        script_path = os.path.join(
            os.path.dirname(__file__), "..", "dpdk-post-process"
        )
        loader = importlib.machinery.SourceFileLoader(mod_name, script_path)
        spec = importlib.util.spec_from_loader(mod_name, loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    finally:
        for key, val in saved.items():
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val

    return mod, mock_metrics


def get_all_sources(mock_metrics):
    """Extract all source values from log_sample mock calls."""
    sources = set()
    for call in mock_metrics.log_sample.call_args_list:
        desc = call[0][1]
        sources.add(desc["source"])
    return sources


class TestGetToolSourceName(unittest.TestCase):
    """Test get_tool_source_name() in isolation."""

    def setUp(self):
        self._mod, _ = import_post_process()

    def test_default_no_env_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self._mod.get_tool_source_name(tmpdir)
            self.assertEqual(result, "dpdk")

    def test_reads_tool_name_from_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_engine_env(tmpdir, "dpdk-ovs")
            result = self._mod.get_tool_source_name(tmpdir)
            self.assertEqual(result, "dpdk-ovs")

    def test_multiline_env_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_engine_env(tmpdir, "dpdk-testpmd", extra_vars={
                "HOME": "/root",
                "PATH": "/usr/bin:/bin",
                "CRUCIBLE_HOME": "/opt/crucible",
                "cs_label": "profiler-remotehosts-1-dpdk-testpmd-1",
            })
            result = self._mod.get_tool_source_name(tmpdir)
            self.assertEqual(result, "dpdk-testpmd")

    def test_empty_env_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "engine-env.txt"), "w") as f:
                f.write("")
            result = self._mod.get_tool_source_name(tmpdir)
            self.assertEqual(result, "dpdk")

    def test_env_file_without_tool_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "engine-env.txt"), "w") as f:
                f.write("HOME=/root\nPATH=/usr/bin\n")
            result = self._mod.get_tool_source_name(tmpdir)
            self.assertEqual(result, "dpdk")


class TestPostProcessSourceNaming(unittest.TestCase):
    """Test that process() uses the correct source in CDM metrics."""

    def test_source_default_without_env_file(self):
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(tmpdir, SAMPLE_HEADER, SAMPLE_DATA_T1)
            mod.process(pp_dir, input_dir=tmpdir)
            sources = get_all_sources(mock_metrics)
            self.assertEqual(sources, {"dpdk"})

    def test_source_from_env_file(self):
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(tmpdir, SAMPLE_HEADER, SAMPLE_DATA_T1)
            write_engine_env(tmpdir, "dpdk-ovs")
            mod.process(pp_dir, input_dir=tmpdir)
            sources = get_all_sources(mock_metrics)
            self.assertEqual(sources, {"dpdk-ovs"})

    def test_source_testpmd_instance(self):
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(tmpdir, SAMPLE_HEADER, SAMPLE_DATA_T1)
            write_engine_env(tmpdir, "dpdk-testpmd")
            mod.process(pp_dir, input_dir=tmpdir)
            sources = get_all_sources(mock_metrics)
            self.assertEqual(sources, {"dpdk-testpmd"})

    def test_rate_metrics_use_instance_source(self):
        """Two samples produce rate metrics; verify they use instance source."""
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(
                tmpdir, SAMPLE_HEADER, SAMPLE_DATA_T1, SAMPLE_DATA_T2
            )
            write_engine_env(tmpdir, "dpdk-ovs")
            mod.process(pp_dir, input_dir=tmpdir)
            sources = get_all_sources(mock_metrics)
            self.assertEqual(sources, {"dpdk-ovs"})

            rate_calls = [
                c for c in mock_metrics.log_sample.call_args_list
                if c[0][1]["type"] in ("rx-pps", "tx-pps", "rx-Gbps",
                                       "tx-Gbps", "rx-missed-sec")
            ]
            self.assertGreater(len(rate_calls), 0)
            for call in rate_calls:
                self.assertEqual(call[0][1]["source"], "dpdk-ovs")

    def test_backward_compatibility_no_env_file(self):
        """Without engine-env.txt, all metrics must use source 'dpdk'."""
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(
                tmpdir, SAMPLE_HEADER, SAMPLE_DATA_T1, SAMPLE_DATA_T2
            )
            mod.process(pp_dir, input_dir=tmpdir)
            sources = get_all_sources(mock_metrics)
            self.assertEqual(sources, {"dpdk"})
            self.assertGreater(mock_metrics.log_sample.call_count, 0)

    def test_multi_instance_distinct_sources(self):
        """Two separate tool dirs produce distinct source names."""
        with tempfile.TemporaryDirectory() as base:
            dir_ovs = os.path.join(base, "dpdk-ovs")
            dir_tpmd = os.path.join(base, "dpdk-testpmd")
            os.makedirs(os.path.join(dir_ovs, "postprocess"))
            os.makedirs(os.path.join(dir_tpmd, "postprocess"))

            write_telemetry_xz(dir_ovs, SAMPLE_HEADER, SAMPLE_DATA_T1)
            write_engine_env(dir_ovs, "dpdk-ovs")

            write_telemetry_xz(dir_tpmd, SAMPLE_HEADER, SAMPLE_DATA_T1)
            write_engine_env(dir_tpmd, "dpdk-testpmd")

            mod_ovs, mock_ovs = import_post_process()
            mod_ovs.process(
                os.path.join(dir_ovs, "postprocess"), input_dir=dir_ovs
            )
            sources_ovs = get_all_sources(mock_ovs)

            mod_tpmd, mock_tpmd = import_post_process()
            mod_tpmd.process(
                os.path.join(dir_tpmd, "postprocess"), input_dir=dir_tpmd
            )
            sources_tpmd = get_all_sources(mock_tpmd)

            self.assertEqual(sources_ovs, {"dpdk-ovs"})
            self.assertEqual(sources_tpmd, {"dpdk-testpmd"})
            self.assertTrue(sources_ovs.isdisjoint(sources_tpmd))


SAMPLE_DATA_WITH_QUEUES = {
    "timestamp_ms": 1700000000000,
    "data": {
        "port_0": {
            "/ethdev/stats": {
                "ipackets": 1000,
                "opackets": 990,
                "ibytes": 64000,
                "obytes": 63360,
                "imissed": 0,
                "ierrors": 0,
                "oerrors": 0,
                "rx_nombuf": 0,
                "q_ipackets": [500, 300, 200, 0],
                "q_opackets": [495, 295, 200, 0],
            },
        },
    },
}


class TestPostProcessQueueDimension(unittest.TestCase):
    """Test that per-queue metrics use 'queue' as a named dimension."""

    def test_queue_packets_uses_queue_dimension(self):
        """queue-packets type with queue in names, not baked into type."""
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(tmpdir, SAMPLE_HEADER, SAMPLE_DATA_WITH_QUEUES)
            mod.process(pp_dir, input_dir=tmpdir)

            queue_calls = [
                c for c in mock_metrics.log_sample.call_args_list
                if c[0][1]["type"] == "queue-packets"
            ]
            self.assertGreater(len(queue_calls), 0)

            for call in queue_calls:
                desc = call[0][1]
                call_names = call[0][2]
                self.assertEqual(desc["type"], "queue-packets")
                self.assertIn("queue", call_names)
                self.assertIn("direction", call_names)
                self.assertIn(call_names["direction"], ("rx", "tx"))

    def test_no_queue_id_in_type_name(self):
        """Ensure no metric type contains queue-q{N} pattern."""
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(tmpdir, SAMPLE_HEADER, SAMPLE_DATA_WITH_QUEUES)
            mod.process(pp_dir, input_dir=tmpdir)

            import re
            old_pattern = re.compile(r"queue-q\d+-packets")
            for call in mock_metrics.log_sample.call_args_list:
                desc = call[0][1]
                self.assertIsNone(
                    old_pattern.match(desc["type"]),
                    f"Found old queue-q{{N}} pattern in type: {desc['type']}",
                )

    def test_queue_dimension_values(self):
        """Verify correct queue IDs and directions are emitted."""
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(tmpdir, SAMPLE_HEADER, SAMPLE_DATA_WITH_QUEUES)
            mod.process(pp_dir, input_dir=tmpdir)

            queue_calls = [
                c for c in mock_metrics.log_sample.call_args_list
                if c[0][1]["type"] == "queue-packets"
            ]
            rx_queues = {
                c[0][2]["queue"] for c in queue_calls
                if c[0][2]["direction"] == "rx"
            }
            tx_queues = {
                c[0][2]["queue"] for c in queue_calls
                if c[0][2]["direction"] == "tx"
            }
            self.assertEqual(rx_queues, {"0", "1", "2"})
            self.assertEqual(tx_queues, {"0", "1", "2"})

    def test_zero_queues_not_emitted(self):
        """Queues with value 0 should not produce metrics."""
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(tmpdir, SAMPLE_HEADER, SAMPLE_DATA_WITH_QUEUES)
            mod.process(pp_dir, input_dir=tmpdir)

            queue_calls = [
                c for c in mock_metrics.log_sample.call_args_list
                if c[0][1]["type"] == "queue-packets"
            ]
            all_queue_ids = {c[0][2]["queue"] for c in queue_calls}
            self.assertNotIn("3", all_queue_ids)


class TestPostProcessPathSeparation(unittest.TestCase):
    """Test that input is read from input_dir and output written to output_dir."""

    def test_reads_input_from_input_dir(self):
        """Telemetry and engine-env are read from input_dir, not output_dir."""
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = tmpdir
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(input_dir, SAMPLE_HEADER, SAMPLE_DATA_T1)
            write_engine_env(input_dir, "dpdk-ovs")
            mod.process(pp_dir, input_dir=input_dir)
            self.assertGreater(mock_metrics.log_sample.call_count, 0)
            sources = get_all_sources(mock_metrics)
            self.assertEqual(sources, {"dpdk-ovs"})

    def test_writes_manifest_to_output_dir(self):
        """post-process-data.json is written to output_dir."""
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(tmpdir, SAMPLE_HEADER, SAMPLE_DATA_T1)
            mod.process(pp_dir, input_dir=tmpdir)
            manifest_path = os.path.join(pp_dir, "post-process-data.json")
            self.assertTrue(os.path.exists(manifest_path))
            with open(manifest_path) as f:
                manifest = json.load(f)
            self.assertEqual(manifest["tool"], "dpdk")
            self.assertEqual(manifest["primary-metric"], "rx-pps")

    def test_no_manifest_in_input_dir(self):
        """post-process-data.json should NOT be in input_dir."""
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            write_telemetry_xz(tmpdir, SAMPLE_HEADER, SAMPLE_DATA_T1)
            mod.process(pp_dir, input_dir=tmpdir)
            self.assertFalse(
                os.path.exists(os.path.join(tmpdir, "post-process-data.json"))
            )

    def test_missing_input_skips_gracefully(self):
        """When input_dir has no telemetry file, process exits cleanly."""
        mod, mock_metrics = import_post_process()
        with tempfile.TemporaryDirectory() as tmpdir:
            pp_dir = os.path.join(tmpdir, "postprocess")
            os.makedirs(pp_dir)
            mod.process(pp_dir, input_dir=tmpdir)
            self.assertEqual(mock_metrics.log_sample.call_count, 0)

    def test_default_input_dir_is_cwd(self):
        """Without explicit input_dir, process reads from '.' (cwd)."""
        mod, _ = import_post_process()
        import inspect
        sig = inspect.signature(mod.process)
        self.assertEqual(sig.parameters["input_dir"].default, ".")


if __name__ == "__main__":
    unittest.main()
