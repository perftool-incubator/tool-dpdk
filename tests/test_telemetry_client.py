#!/usr/bin/env python3
"""Unit tests for the DPDK Telemetry v2 client library."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dpdk_telemetry_client import (
    DPDKTelemetryClient,
    DPDK_TELEMETRY_V2,
    MAX_OUTPUT_LEN,
    SEARCH_DIRS,
)


class FakeTelemetryServer:
    """Minimal DPDK telemetry socket server for testing."""

    def __init__(self, socket_path):
        self._path = socket_path
        self._sock = None
        self._thread = None
        self._running = False
        self._commands = {
            "/": {"/": ["/ethdev/list", "/ethdev/stats", "/eal/params"]},
            "/ethdev/list": {"/ethdev/list": [0, 1]},
            "/eal/params": {"/eal/params": "-n 4 --lcores 0-3"},
        }
        self.handshake = {
            "version": "DPDK 23.11.0",
            "pid": 12345,
            "max_output_len": MAX_OUTPUT_LEN,
        }

    def add_command(self, cmd, response):
        self._commands[cmd] = response

    def start(self):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._sock.bind(self._path)
        self._sock.listen(1)
        self._sock.settimeout(2)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            conn.send(json.dumps(self.handshake).encode())

            while self._running:
                try:
                    data = conn.recv(MAX_OUTPUT_LEN)
                    if not data:
                        break
                    cmd = data.decode().strip()
                    base_cmd = cmd.split(",")[0]
                    resp = self._commands.get(
                        base_cmd, {base_cmd: "unknown command"}
                    )
                    conn.send(json.dumps(resp).encode())
                except OSError:
                    break
            conn.close()

    def stop(self):
        self._running = False
        if self._sock:
            self._sock.close()
        if self._thread:
            self._thread.join(timeout=3)


class TestDPDKTelemetryClientDiscover(unittest.TestCase):
    """Test socket discovery logic (no live socket needed)."""

    def test_explicit_socket_path(self):
        client = DPDKTelemetryClient(socket_path="/tmp/fake.sock")
        self.assertEqual(client.discover_socket(), "/tmp/fake.sock")

    def test_file_prefix_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefix_dir = os.path.join(tmpdir, "myprefix")
            os.makedirs(prefix_dir)
            sock_path = os.path.join(prefix_dir, DPDK_TELEMETRY_V2)
            with open(sock_path, "w") as f:
                f.write("")

            client = DPDKTelemetryClient(file_prefix="myprefix")
            import dpdk_telemetry_client as mod

            old_dirs = mod.SEARCH_DIRS[:]
            mod.SEARCH_DIRS[:] = [tmpdir]
            try:
                result = client.discover_socket()
                self.assertEqual(result, sock_path)
            finally:
                mod.SEARCH_DIRS[:] = old_dirs

    def test_auto_discover(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prefix_dir = os.path.join(tmpdir, "rte")
            os.makedirs(prefix_dir)
            sock_path = os.path.join(prefix_dir, DPDK_TELEMETRY_V2)
            with open(sock_path, "w") as f:
                f.write("")

            client = DPDKTelemetryClient()
            import dpdk_telemetry_client as mod

            old_dirs = mod.SEARCH_DIRS[:]
            mod.SEARCH_DIRS[:] = [tmpdir]
            try:
                result = client.discover_socket()
                self.assertEqual(result, sock_path)
            finally:
                mod.SEARCH_DIRS[:] = old_dirs

    def test_no_socket_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = DPDKTelemetryClient()
            import dpdk_telemetry_client as mod

            old_dirs = mod.SEARCH_DIRS[:]
            mod.SEARCH_DIRS[:] = [tmpdir]
            try:
                result = client.discover_socket()
                self.assertIsNone(result)
            finally:
                mod.SEARCH_DIRS[:] = old_dirs


class TestDPDKTelemetryClientConnect(unittest.TestCase):
    """Test connect, handshake, and query against a fake server."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._sock_path = os.path.join(self._tmpdir, "test_telemetry.sock")
        self._server = FakeTelemetryServer(self._sock_path)
        self._server.start()
        time.sleep(0.1)

    def tearDown(self):
        self._server.stop()
        if os.path.exists(self._sock_path):
            os.unlink(self._sock_path)
        os.rmdir(self._tmpdir)

    def test_connect_and_handshake(self):
        client = DPDKTelemetryClient(socket_path=self._sock_path)
        info = client.connect()
        self.assertEqual(info["version"], "DPDK 23.11.0")
        self.assertEqual(info["pid"], 12345)
        self.assertTrue(client.connected)
        client.close()
        self.assertFalse(client.connected)

    def test_query(self):
        client = DPDKTelemetryClient(socket_path=self._sock_path)
        client.connect()
        resp = client.query("/ethdev/list")
        self.assertEqual(resp["/ethdev/list"], [0, 1])
        client.close()

    def test_query_with_params(self):
        self._server.add_command(
            "/ethdev/stats",
            {"/ethdev/stats": {"ipackets": 100, "opackets": 99}},
        )
        client = DPDKTelemetryClient(socket_path=self._sock_path)
        client.connect()
        resp = client.query("/ethdev/stats,0")
        self.assertIn("/ethdev/stats", resp)
        client.close()

    def test_query_without_connect_raises(self):
        client = DPDKTelemetryClient(socket_path=self._sock_path)
        with self.assertRaises(ConnectionError):
            client.query("/")

    def test_connect_with_retry_success(self):
        client = DPDKTelemetryClient(socket_path=self._sock_path)
        info = client.connect_with_retry(timeout=5)
        self.assertEqual(info["pid"], 12345)
        client.close()

    def test_connect_with_retry_timeout(self):
        client = DPDKTelemetryClient(socket_path="/tmp/nonexistent.sock")
        with self.assertRaises(TimeoutError):
            client.connect_with_retry(timeout=1, backoff=0.2)


if __name__ == "__main__":
    unittest.main()
