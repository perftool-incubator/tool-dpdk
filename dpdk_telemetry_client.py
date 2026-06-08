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
        self._max_output = MAX_OUTPUT_LEN
        self._connected_path = None

    @property
    def info(self):
        """Handshake info: {"version": ..., "pid": ..., "max_output_len": ...}"""
        return self._info

    @property
    def connected(self):
        return self._sock is not None

    @property
    def discovered_path(self):
        """The socket path used for the current connection."""
        return self._connected_path

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
        3. Auto-scan all known directories for any telemetry socket
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
        self._max_output = self._info.get("max_output_len", MAX_OUTPUT_LEN)
        self._connected_path = path
        return self._info

    def query(self, command):
        """Send a command and return the parsed JSON response."""
        if not self._sock:
            raise ConnectionError("Not connected. Call connect() first.")

        self._sock.send(command.encode("utf-8"))
        raw = self._sock.recv(self._max_output)
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
            self._max_output = MAX_OUTPUT_LEN
            self._connected_path = None

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
