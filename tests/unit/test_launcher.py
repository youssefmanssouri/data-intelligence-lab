"""
Data Intelligence Lab - Unit Tests for Unified Windows Launcher
Mocks Win32 API operations and tests startup, readiness, failure,
timeout, port validation, handle ownership, ABI signatures, and shutdown logic.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
from typing import Callable, Dict, List, Optional, Set, Tuple
import urllib.error
import urllib.request

import pytest

import launcher
from launcher import (
    BYTE,
    CREATE_UNICODE_ENVIRONMENT,
    DWORD,
    ERROR_INSUFFICIENT_BUFFER,
    EXTENDED_STARTUPINFO_PRESENT,
    HANDLE,
    HANDLE_FLAG_INHERIT,
    IO_COUNTERS,
    IWin32Adapter,
    JOBOBJECT_BASIC_LIMIT_INFORMATION,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    LauncherError,
    MINIMUM_CLIENT_BUILD,
    MINIMUM_SERVER_BUILD,
    MINIMUM_WINDOWS_BUILD,
    MINIMUM_WINDOWS_MAJOR,
    OSVERSIONINFOEXW,
    PROCESS_INFORMATION,
    PlatformCompatibilityError,
    PortFileError,
    ProcessCleanupObligation,
    ProcessCreationCleanupError,
    ProcessEarlyExitError,
    ProcessHandle,
    ProcessQueryError,
    ProcessState,
    ProcessStatusResult,
    SIZE_T,
    STARTF_USESTDHANDLES,
    STARTUPINFOEXW,
    STARTUPINFOW,
    STILL_ACTIVE,
    ServiceReadinessError,
    ThreadCleanupObligation,
    UnifiedLauncher,
    VALID_PRODUCT_TYPES,
    VER_NT_DOMAIN_CONTROLLER,
    VER_NT_SERVER,
    VER_NT_UNKNOWN,
    VER_NT_WORKSTATION,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    Win32APIError,
    Win32Adapter,
    WindowsVersionInfo,
    format_environment_block,
)


class MockWin32Adapter(IWin32Adapter):
    """Deterministic in-memory mock of the Win32 API adapter."""

    def __init__(self) -> None:
        super().__init__()
        self.is_windows_val = True
        self.version_info = WindowsVersionInfo(
            major=10, minor=0, build=19045, product_type=VER_NT_WORKSTATION
        )
        self.fail_check_version = False
        self.next_handle_id = 100

        self.created_job_handles: Set[int] = set()
        self.created_process_handles: Set[int] = set()
        self.open_handles: Set[int] = set()
        self.closed_handles: Set[int] = set()
        self.terminated_processes: List[Tuple[int, int]] = []

        self.process_exit_codes: Dict[int, Optional[int]] = {}
        self.process_query_fails: Set[int] = set()
        self.spawn_history: List[Dict[str, object]] = []

        # Configurable failure injections
        self.fail_create_job = False
        self.fail_set_kill_on_close = False
        self.fail_create_process = False
        self.fail_create_process_for: Optional[str] = None  # "backend" or "frontend"
        self.fail_close_thread = False
        self.fail_query_status = False
        self.fail_terminate = False
        self.fail_close_handle = False
        self.fail_close_handle_for: Set[int] = set()
        self.fail_wait = False
        self.simulate_wait_timeout = False

        # Callbacks on spawn
        self.on_spawn: Optional[Callable[[int, str, Optional[Path]], None]] = None

    @property
    def version_val(self) -> Tuple[int, int]:
        return self.version_info.major, self.version_info.build

    @version_val.setter
    def version_val(self, val: Tuple[int, int]) -> None:
        self.version_info = WindowsVersionInfo(
            major=val[0], minor=0, build=val[1], product_type=self.version_info.product_type
        )

    def is_windows(self) -> bool:
        return self.is_windows_val

    def check_version(self) -> WindowsVersionInfo:
        if self.fail_check_version:
            raise Win32APIError("RtlGetVersion failed with NTSTATUS -1073741823")
        return self.version_info

    def create_job_object(self) -> int:
        if self.fail_create_job:
            raise Win32APIError("CreateJobObjectW failed with error code 5")
        h = self.next_handle_id
        self.next_handle_id += 1
        self.created_job_handles.add(h)
        self.open_handles.add(h)
        return h

    def set_job_kill_on_close(self, h_job: int) -> None:
        if self.fail_set_kill_on_close:
            raise Win32APIError("SetInformationJobObject failed with error code 87")
        if h_job not in self.open_handles:
            raise Win32APIError(f"Invalid Job handle: {h_job}")

    def create_process_in_job(
        self,
        h_job: int,
        command_line: str,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        redirect_stdin: bool = False,
    ) -> Tuple[int, int, Optional[int]]:
        if self.fail_create_process:
            raise Win32APIError("CreateProcessW failed with error code 2")
        if self.fail_create_process_for and self.fail_create_process_for in command_line:
            raise Win32APIError(f"CreateProcessW failed for {self.fail_create_process_for}")
        if h_job not in self.open_handles:
            raise Win32APIError("Job Object handle is closed or invalid during process creation")

        h_proc = self.next_handle_id
        self.next_handle_id += 1
        pid = h_proc + 1000

        h_stdin_write = None
        if redirect_stdin:
            h_stdin_write = self.next_handle_id
            self.next_handle_id += 1
            self.open_handles.add(h_stdin_write)

        self.created_process_handles.add(h_proc)
        self.open_handles.add(h_proc)
        self.process_exit_codes[h_proc] = None  # Running

        self.spawn_history.append({
            "h_job": h_job,
            "command_line": command_line,
            "cwd": cwd,
            "env": env,
            "redirect_stdin": redirect_stdin,
            "h_proc": h_proc,
            "pid": pid,
            "h_stdin_write": h_stdin_write,
        })

        if self.on_spawn:
            self.on_spawn(h_proc, command_line, cwd)

        if self.fail_close_thread:
            thread_err = 6  # ERROR_INVALID_HANDLE
            thread_handle = self.next_handle_id
            self.next_handle_id += 1
            thread_ob = ThreadCleanupObligation(
                h_thread=thread_handle,
                adapter=self,
            )
            self.outstanding_cleanup_obligations.append(thread_ob)

            obligation = self._cleanup_aborted_process(
                h_process=h_proc,
                pid=pid,
                timeout_ms=2000,
            )
            obligation.h_thread = thread_handle
            obligation.thread_obligation = thread_ob
            if not obligation.is_complete:
                self.outstanding_cleanup_obligations.append(obligation)

            sec_msg = (
                f"; secondary cleanup errors: {'; '.join(obligation.secondary_errors)}"
                if obligation.secondary_errors
                else ""
            )
            raise ProcessCreationCleanupError(
                f"CloseHandle failed for initial thread handle with error code {thread_err}; "
                f"process creation aborted{sec_msg}",
                primary_error=thread_err,
                obligation=obligation,
                secondary_errors=obligation.secondary_errors,
                thread_obligation=thread_ob,
            )

        return h_proc, pid, h_stdin_write

    def _cleanup_aborted_process(
        self, h_process: int, pid: int, timeout_ms: int = 2000
    ) -> ProcessCleanupObligation:
        obligation = ProcessCleanupObligation(
            h_process=h_process,
            pid=pid,
            adapter=self,
        )
        if not h_process:
            obligation.secondary_errors.append("Cannot clean up aborted process: missing or invalid handle (0)")
            return obligation

        # Step 1: Attempt termination
        try:
            self.terminate_process(h_process, 1)
            obligation.termination_requested = True
        except Exception as exc:
            obligation.secondary_errors.append(f"TerminateProcess failed for handle {h_process}: {exc}")

        # Step 2: Bounded wait
        try:
            wait_res = self.wait_for_process(h_process, timeout_ms)
            if wait_res == WAIT_OBJECT_0:
                obligation.termination_confirmed = True
            elif wait_res == WAIT_TIMEOUT:
                obligation.secondary_errors.append(f"WaitForSingleObject timed out after {timeout_ms}ms")
            else:
                obligation.secondary_errors.append(f"WaitForSingleObject unexpected status: {wait_res}")
        except Exception as exc:
            obligation.secondary_errors.append(f"WaitForSingleObject error for handle {h_process}: {exc}")

        # Step 3: Handle closure if termination confirmed
        if obligation.termination_confirmed:
            try:
                self.close_handle(h_process)
                obligation.handle_closed = True
                obligation.h_process = 0
            except Exception as exc:
                obligation.secondary_errors.append(f"CloseHandle failed for process handle {h_process}: {exc}")
        else:
            obligation.secondary_errors.append(
                f"Process handle {h_process} retained because child termination was not confirmed"
            )

        return obligation

    def wait_for_process(self, h_process: int, timeout_ms: int) -> int:
        if self.fail_wait:
            raise Win32APIError(f"WaitForSingleObject failed for handle {h_process} with error code 6")
        if self.simulate_wait_timeout:
            return WAIT_TIMEOUT
        if h_process not in self.open_handles:
            return WAIT_OBJECT_0
        code = self.process_exit_codes.get(h_process)
        return WAIT_OBJECT_0 if code is not None else WAIT_TIMEOUT

    def query_process_status(self, h_process: int) -> ProcessStatusResult:
        if self.fail_query_status or h_process in self.process_query_fails:
            return ProcessStatusResult(
                state=ProcessState.QUERY_FAILED,
                error=f"GetExitCodeProcess failed with error code 6 for handle {h_process}",
            )
        if h_process not in self.open_handles:
            return ProcessStatusResult(ProcessState.EXITED, exit_code=-1)

        code = self.process_exit_codes.get(h_process)
        if code is None:
            return ProcessStatusResult(state=ProcessState.RUNNING)
        return ProcessStatusResult(state=ProcessState.EXITED, exit_code=code)

    def get_process_exit_code(self, h_process: int) -> Optional[int]:
        res = self.query_process_status(h_process)
        if res.state == ProcessState.EXITED:
            return res.exit_code
        elif res.state == ProcessState.RUNNING:
            return None
        raise Win32APIError(f"Process query failed: {res.error}")

    def terminate_process(self, h_process: int, exit_code: int = 1) -> None:
        if self.fail_terminate:
            raise Win32APIError(f"TerminateProcess failed for handle {h_process} with error code 5")
        if h_process in self.open_handles:
            self.terminated_processes.append((h_process, exit_code))
            self.process_exit_codes[h_process] = exit_code

    def close_handle(self, handle: int) -> None:
        if self.fail_close_handle or handle in self.fail_close_handle_for:
            raise Win32APIError(f"CloseHandle failed for handle {handle} with error code 6")
        if handle in self.open_handles:
            self.open_handles.remove(handle)
            self.closed_handles.add(handle)

    def register_mock_process(
        self, name: str = "test", exit_code: Optional[int] = None, fail_query: bool = False
    ) -> ProcessHandle:
        """Helper to create and register an open process handle for testing."""
        h_proc = self.next_handle_id
        self.next_handle_id += 1
        pid = h_proc + 1000
        self.open_handles.add(h_proc)
        self.process_exit_codes[h_proc] = exit_code
        if fail_query:
            self.process_query_fails.add(h_proc)
        return ProcessHandle(name, h_proc, pid, self)


@pytest.fixture
def mock_app_tree(tmp_path: Path) -> Path:
    """Creates mock repository directory structure for launcher testing."""
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir(parents=True, exist_ok=True)
    (backend_dir / "main.py").write_text("# backend stub\n", encoding="utf-8")

    frontend_scripts = tmp_path / "frontend" / "scripts"
    frontend_scripts.mkdir(parents=True, exist_ok=True)
    (frontend_scripts / "server.mjs").write_text("// frontend stub\n", encoding="utf-8")

    return tmp_path


# ---------------------------------------------------------------------------
# F-01 & Structure Layout Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Win32 ABI tests require Windows platform")
def test_win32_adapter_signatures_and_prototypes() -> None:
    """F-01: Verifies that Win32Adapter declares explicit argtypes and restype for all native APIs."""
    adapter = Win32Adapter()
    k32 = adapter._kernel32

    # Verify functions have declared restype and argtypes
    expected_functions = [
        ("CreateJobObjectW", wintypes.HANDLE, 2),
        ("SetInformationJobObject", wintypes.BOOL, 4),
        ("InitializeProcThreadAttributeList", wintypes.BOOL, 4),
        ("UpdateProcThreadAttribute", wintypes.BOOL, 7),
        ("DeleteProcThreadAttributeList", None, 1),
        ("CreateProcessW", wintypes.BOOL, 10),
        ("WaitForSingleObject", wintypes.DWORD, 2),
        ("GetExitCodeProcess", wintypes.BOOL, 2),
        ("TerminateProcess", wintypes.BOOL, 2),
        ("CloseHandle", wintypes.BOOL, 1),
    ]

    for fn_name, expected_restype, expected_arg_count in expected_functions:
        fn = getattr(k32, fn_name)
        assert fn.restype == expected_restype, f"{fn_name} restype mismatch: {fn.restype} != {expected_restype}"
        assert fn.argtypes is not None, f"{fn_name} argtypes is None"
        assert len(fn.argtypes) == expected_arg_count, (
            f"{fn_name} argtypes length mismatch: {len(fn.argtypes)} != {expected_arg_count}"
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_win32_structure_sizes_and_alignments() -> None:
    """F-01: Verifies that 64-bit Windows ctypes struct sizes match the official Windows SDK."""
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(IO_COUNTERS) == 48
        assert ctypes.sizeof(JOBOBJECT_BASIC_LIMIT_INFORMATION) == 64
        assert ctypes.sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144
        assert ctypes.sizeof(STARTUPINFOW) == 104
        assert ctypes.sizeof(STARTUPINFOEXW) == 112
        assert ctypes.sizeof(PROCESS_INFORMATION) == 24


# ---------------------------------------------------------------------------
# F-02 & F-08: Environment Block Formatting & Ordering Tests
# ---------------------------------------------------------------------------

def test_format_environment_block_ordering_and_double_null() -> None:
    """F-02 & F-08: Verifies case-insensitive alphabetical sorting and double-null termination."""
    env = {
        "zebra": "final",
        "APPLE": "fruit",
        "banana": "yellow",
        "AARDVARK": "animal",
    }
    buf = format_environment_block(env)
    # buf.value stops at the first null; buf[:] retrieves the entire wide-character buffer
    raw_str = buf[:]
    assert raw_str.endswith("\0\0")

    # Extract non-empty entries separated by null characters
    entries = [entry for entry in raw_str.rstrip("\0").split("\0") if entry]
    assert entries == [
        "AARDVARK=animal",
        "APPLE=fruit",
        "banana=yellow",
        "zebra=final",
    ]


def test_format_environment_block_unicode_and_empty_values() -> None:
    """F-02 & F-08: Verifies non-ASCII Unicode characters and empty string values."""
    env = {
        "UNICODE_JP": "こんにちは",
        "UNICODE_RU": "данные",
        "EMPTY_VAR": "",
    }
    buf = format_environment_block(env)
    raw_str = buf[:]
    assert raw_str.endswith("\0\0")

    entries = [entry for entry in raw_str.rstrip("\0").split("\0") if entry or entry == "EMPTY_VAR="]
    # Check that EMPTY_VAR is present as 'EMPTY_VAR='
    assert "EMPTY_VAR=" in entries
    assert "UNICODE_JP=こんにちは" in entries
    assert "UNICODE_RU=данные" in entries


def test_format_environment_block_empty_dict() -> None:
    """F-02 & F-08: Verifies formatting of an empty environment dictionary."""
    buf = format_environment_block({})
    raw_str = buf[:]
    assert raw_str.startswith("\0\0")


# ---------------------------------------------------------------------------
# F-06: Windows Version & Platform Compatibility Tests
# ---------------------------------------------------------------------------

def test_validate_platform_rejects_non_windows(mock_app_tree: Path) -> None:
    adapter = MockWin32Adapter()
    adapter.is_windows_val = False
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    with pytest.raises(PlatformCompatibilityError, match="requires Windows"):
        l.validate_platform()


@pytest.mark.parametrize(
    "major, build",
    [
        (6, 7601),   # Windows 7
        (6, 9600),   # Windows 8.1
        (10, 10240), # Windows 10 Version 1507 (Initial Release)
        (10, 10586), # Windows 10 Version 1511 (November Update)
        (10, 14393), # Windows 10 Version 1607 (Anniversary Update)
    ],
)
def test_validate_platform_rejects_below_minimum_versions(
    mock_app_tree: Path, major: int, build: int
) -> None:
    """F-06: Verifies that Windows releases prior to Build 15063 are rejected."""
    adapter = MockWin32Adapter()
    adapter.version_val = (major, build)
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    with pytest.raises(PlatformCompatibilityError, match="requires Windows 10"):
        l.validate_platform()


@pytest.mark.parametrize(
    "major, build",
    [
        (10, 15063), # Windows 10 Version 1703 (Creators Update - Exact Minimum)
        (10, 19045), # Windows 10 Version 22H2
        (10, 22631), # Windows 11 Version 23H2
    ],
)
def test_validate_platform_accepts_supported_versions(
    mock_app_tree: Path, major: int, build: int
) -> None:
    """F-06: Verifies that Windows 10 Build 15063 and later builds pass validation."""
    adapter = MockWin32Adapter()
    adapter.version_val = (major, build)
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)
    l.validate_platform()  # Does not raise


def test_validate_paths_fails_when_backend_missing(tmp_path: Path) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    with pytest.raises(LauncherError, match="Backend entry point missing"):
        l.validate_paths()


def test_validate_paths_fails_when_frontend_missing(tmp_path: Path) -> None:
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    (backend_dir / "main.py").write_text("# stub", encoding="utf-8")

    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    with pytest.raises(LauncherError, match="Frontend entry point missing"):
        l.validate_paths()


# ---------------------------------------------------------------------------
# F-04: Process Exit-Query & Liveness Representation Tests
# ---------------------------------------------------------------------------

def test_process_status_three_states() -> None:
    """F-04: Verifies distinct representation of RUNNING, EXITED, and QUERY_FAILED states."""
    adapter = MockWin32Adapter()

    # 1. Running process
    proc_running = adapter.register_mock_process("running_proc", exit_code=None)
    status_running = proc_running.poll_status()
    assert status_running.state == ProcessState.RUNNING
    assert proc_running.is_alive() is True
    assert proc_running.poll() is None

    # 2. Exited process
    proc_exited = adapter.register_mock_process("exited_proc", exit_code=42)
    status_exited = proc_exited.poll_status()
    assert status_exited.state == ProcessState.EXITED
    assert status_exited.exit_code == 42
    assert proc_exited.is_alive() is False
    assert proc_exited.poll() == 42

    # 3. Query failed process
    proc_failed = adapter.register_mock_process("failed_proc", fail_query=True)
    status_failed = proc_failed.poll_status()
    assert status_failed.state == ProcessState.QUERY_FAILED
    assert "GetExitCodeProcess failed" in (status_failed.error or "")

    # Query failure must raise Win32APIError on is_alive() and poll()
    with pytest.raises(Win32APIError, match="Failed to query status"):
        proc_failed.is_alive()
    with pytest.raises(Win32APIError, match="Failed to query status"):
        proc_failed.poll()


def test_read_port_file_detects_query_failure(tmp_path: Path) -> None:
    """F-04: Verifies that a status query failure during port polling raises ProcessQueryError."""
    adapter = MockWin32Adapter()
    proc = adapter.register_mock_process("backend", fail_query=True)

    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    port_file = tmp_path / "test.port"

    with pytest.raises(ProcessQueryError, match="Failed to query status for service 'backend'"):
        l.read_port_file(port_file, proc, timeout_s=1.0)


def test_probe_http_detects_query_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """F-04: Verifies that a status query failure during HTTP probe raises ProcessQueryError."""
    adapter = MockWin32Adapter()
    proc = adapter.register_mock_process("backend", fail_query=True)

    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    with pytest.raises(ProcessQueryError, match="Failed to query status for service 'backend' during probe"):
        l.probe_http("http://127.0.0.1:8000/health", expected_status=200, timeout_s=1.0, process=proc)


def test_supervise_once_handles_query_failure(tmp_path: Path) -> None:
    """F-04: Verifies that supervise_once surfaces status query failures."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    b_proc = adapter.register_mock_process("backend", exit_code=None)
    f_proc = adapter.register_mock_process("frontend", exit_code=None)
    l.backend_proc = b_proc
    l.frontend_proc = f_proc

    # Invalidate query for backend
    adapter.process_query_fails.add(b_proc.h_process)
    err = l.supervise_once()
    assert err is not None
    assert "status query failed" in err


# ---------------------------------------------------------------------------
# Job Object & Failure Paths
# ---------------------------------------------------------------------------

def test_init_job_object_success(mock_app_tree: Path) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    h_job = l.init_job_object()
    assert h_job in adapter.open_handles
    assert h_job in adapter.created_job_handles


def test_init_job_object_create_failure(mock_app_tree: Path) -> None:
    adapter = MockWin32Adapter()
    adapter.fail_create_job = True
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    with pytest.raises(Win32APIError, match="CreateJobObjectW failed"):
        l.init_job_object()
    assert len(adapter.open_handles) == 0


def test_init_job_object_set_info_failure_closes_handle(mock_app_tree: Path) -> None:
    adapter = MockWin32Adapter()
    adapter.fail_set_kill_on_close = True
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    with pytest.raises(Win32APIError, match="SetInformationJobObject failed"):
        l.init_job_object()
    assert len(adapter.open_handles) == 0
    assert len(adapter.closed_handles) == 1


# ---------------------------------------------------------------------------
# Port File Parsing and Validation Tests
# ---------------------------------------------------------------------------

def test_read_port_file_success(tmp_path: Path) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    port_file = tmp_path / "test.port"
    port_file.write_text("54321\n", encoding="utf-8")

    proc = adapter.register_mock_process("test", exit_code=None)
    port = l.read_port_file(port_file, proc, timeout_s=1.0)
    assert port == 54321


@pytest.mark.parametrize("invalid_content", ["", "   ", "abc", "-1", "0", "65536", "8000.5", "8000\nextra"])
def test_read_port_file_rejects_invalid_values(tmp_path: Path, invalid_content: str) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    port_file = tmp_path / "test.port"
    port_file.write_text(invalid_content, encoding="utf-8")

    proc = adapter.register_mock_process("test", exit_code=None)
    with pytest.raises(PortFileError):
        l.read_port_file(port_file, proc, timeout_s=0.2, poll_interval_s=0.05)


def test_read_port_file_early_exit_detection(tmp_path: Path) -> None:
    adapter = MockWin32Adapter()
    proc = adapter.register_mock_process("backend", exit_code=42)

    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    port_file = tmp_path / "test.port"

    with pytest.raises(ProcessEarlyExitError, match="exited prematurely with code 42"):
        l.read_port_file(port_file, proc, timeout_s=1.0)


def test_read_port_file_timeout(tmp_path: Path) -> None:
    adapter = MockWin32Adapter()
    proc = adapter.register_mock_process("backend", exit_code=None)

    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    port_file = tmp_path / "nonexistent.port"

    with pytest.raises(PortFileError, match="Timed out after 0.1s waiting for port announcement"):
        l.read_port_file(port_file, proc, timeout_s=0.1, poll_interval_s=0.02)


# ---------------------------------------------------------------------------
# HTTP Probes & Sequential Readiness Enforcement
# ---------------------------------------------------------------------------

def test_probe_http_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    class MockResponse:
        status = 200

        def __enter__(self) -> "MockResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: MockResponse())
    l.probe_http("http://127.0.0.1:8000/health", expected_status=200, timeout_s=1.0)


def test_probe_http_storage_503_fails_immediately(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    def mock_urlopen_503(req: object, timeout: float) -> None:
        raise urllib.error.HTTPError("http://127.0.0.1:8000/ready", 503, "Storage Not Ready", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen_503)

    with pytest.raises(ServiceReadinessError, match="reported storage not ready"):
        l.probe_http("http://127.0.0.1:8000/ready", expected_status=200, timeout_s=0.2, poll_interval_s=0.05)


def test_probe_http_detects_child_early_death(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    adapter = MockWin32Adapter()
    proc = adapter.register_mock_process("backend", exit_code=1)

    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    with pytest.raises(ProcessEarlyExitError, match="died during HTTP readiness probe with exit code 1"):
        l.probe_http("http://127.0.0.1:8000/health", expected_status=200, timeout_s=1.0, process=proc)


# ---------------------------------------------------------------------------
# End-to-End Mock Startup Sequence
# ---------------------------------------------------------------------------

def test_successful_startup_sequence(mock_app_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    # When backend or frontend is spawned, write its port file
    def on_spawn(h_proc: int, cmd: str, cwd: Optional[Path]) -> None:
        if "backend.main" in cmd:
            l.backend_port_file.write_text("8080\n", encoding="utf-8")
        elif "server.mjs" in cmd:
            l.frontend_port_file.write_text("3030\n", encoding="utf-8")

    adapter.on_spawn = on_spawn

    class MockResponse:
        status = 200

        def __enter__(self) -> "MockResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: MockResponse())

    browser_opened = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: browser_opened.append(url) or True)

    b_port, f_port = l.startup()

    assert b_port == 8080
    assert f_port == 3030
    assert browser_opened == ["http://127.0.0.1:3030"]

    # Verify both processes were spawned inside the Job Object
    assert len(adapter.spawn_history) == 2
    for entry in adapter.spawn_history:
        assert entry["h_job"] == l.h_job

    # Verify frontend received DIL_BACKEND_ORIGIN pointing to backend port 8080
    frontend_spawn = adapter.spawn_history[1]
    assert frontend_spawn["env"]["DIL_BACKEND_ORIGIN"] == "http://127.0.0.1:8080"
    assert frontend_spawn["env"]["NEXT_TELEMETRY_DISABLED"] == "1"


def test_browser_launch_failure_does_not_abort_services(
    mock_app_tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    adapter.on_spawn = lambda h, cmd, cwd: (
        l.backend_port_file.write_text("8080\n") if "backend.main" in cmd else l.frontend_port_file.write_text("3030\n")
    )

    class MockResponse:
        status = 200

        def __enter__(self) -> "MockResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: MockResponse())
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: False)  # Failed to open browser

    b_port, f_port = l.startup()
    assert b_port == 8080
    assert f_port == 3030

    out, _ = capsys.readouterr()
    assert "[WARN] Could not automatically open default browser" in out


def test_backend_creation_failure_cleans_up(mock_app_tree: Path) -> None:
    adapter = MockWin32Adapter()
    adapter.fail_create_process = True
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    with pytest.raises(Win32APIError, match="CreateProcessW failed"):
        l.startup()

    # Confirms handles are cleaned up and no port files left
    assert len(adapter.open_handles) == 0
    assert not l.backend_port_file.is_file()
    assert not l.frontend_port_file.is_file()


def test_frontend_failure_terminates_backend_and_cleans_up(
    mock_app_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = MockWin32Adapter()
    adapter.fail_create_process_for = "server.mjs"
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    adapter.on_spawn = lambda h, cmd, cwd: (
        l.backend_port_file.write_text("8080\n") if "backend.main" in cmd else None
    )

    class MockResponse:
        status = 200

        def __enter__(self) -> "MockResponse":
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: MockResponse())

    with pytest.raises(Win32APIError, match="CreateProcessW failed for server.mjs"):
        l.startup()

    # Backend was spawned, but frontend failed. Verify backend was terminated and all handles closed.
    assert len(adapter.terminated_processes) == 1
    assert len(adapter.open_handles) == 0
    assert not l.backend_port_file.is_file()
    assert not l.frontend_port_file.is_file()


# ---------------------------------------------------------------------------
# F-05 & F-07: Supervision, Shutdown Semantics & Observability Tests
# ---------------------------------------------------------------------------

def test_supervise_once_detects_backend_exit(tmp_path: Path) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    b_proc = adapter.register_mock_process("backend", exit_code=None)
    f_proc = adapter.register_mock_process("frontend", exit_code=None)

    l.backend_proc = b_proc
    l.frontend_proc = f_proc

    # Both healthy
    assert l.supervise_once() is None

    # Backend dies
    adapter.process_exit_codes[b_proc.h_process] = 137
    err = l.supervise_once()
    assert err is not None
    assert f"Backend process (PID {b_proc.pid}) exited unexpectedly with code 137" in err


def test_supervise_once_detects_frontend_exit(tmp_path: Path) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    b_proc = adapter.register_mock_process("backend", exit_code=None)
    f_proc = adapter.register_mock_process("frontend", exit_code=None)

    l.backend_proc = b_proc
    l.frontend_proc = f_proc

    # Frontend dies
    adapter.process_exit_codes[f_proc.h_process] = 1
    err = l.supervise_once()
    assert err is not None
    assert f"Frontend process (PID {f_proc.pid}) exited unexpectedly with code 1" in err


def test_shutdown_is_idempotent(tmp_path: Path) -> None:
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    h_job = adapter.create_job_object()
    l.h_job = h_job

    b_proc = adapter.register_mock_process("backend", exit_code=None)
    l.backend_proc = b_proc

    l.shutdown()
    assert l.h_job is None
    assert l.backend_proc is None
    assert len(adapter.open_handles) == 0

    # Second shutdown call must be safe and idempotent
    l.shutdown()
    assert l.h_job is None


def test_shutdown_observability_and_job_closure_guarantee(tmp_path: Path) -> None:
    """F-05 & F-07: Verifies that shutdown closes Job handle even if process termination fails."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)

    h_job = adapter.create_job_object()
    l.h_job = h_job

    b_proc = adapter.register_mock_process("backend", exit_code=None)
    f_proc = adapter.register_mock_process("frontend", exit_code=None)
    l.backend_proc = b_proc
    l.frontend_proc = f_proc

    # Incur termination failure for backend
    adapter.fail_terminate = True

    # Shutdown should log/catch error and continue to close Job handle
    l.shutdown()

    assert l.h_job is None
    assert h_job in adapter.closed_handles
    assert not l.backend_port_file.is_file()
    assert not l.frontend_port_file.is_file()


def test_primary_startup_error_preserved_when_shutdown_has_errors(mock_app_tree: Path) -> None:
    """F-07: Verifies that a primary startup error is never masked by a secondary cleanup error."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    # Fail process creation
    adapter.fail_create_process = True
    # Fail close handle during cleanup
    adapter.fail_close_handle = True

    # Primary error must be raised
    with pytest.raises(Win32APIError, match="CreateProcessW failed"):
        l.startup()


# ---------------------------------------------------------------------------
# F-03 & F-02: Win32Adapter Native Logic & Attribute-List Lifecycle Tests
# ---------------------------------------------------------------------------

class FakeKernel32SizingAndInit:
    """Mock for Win32Adapter kernel32 testing without real process creation."""

    def __init__(
        self,
        sizing_return: int = 0,
        sizing_error: int = ERROR_INSUFFICIENT_BUFFER,
        sizing_size: int = 64,
        init_return: int = 1,
        update_return: int = 1,
        create_return: int = 1,
        thread_handle: int = 102,
        fail_close_thread: bool = False,
        fail_terminate: bool = False,
        wait_return: int = WAIT_OBJECT_0,
        fail_wait: bool = False,
        fail_close_process: bool = False,
    ) -> None:
        self.sizing_return = sizing_return
        self.sizing_error = sizing_error
        self.sizing_size = sizing_size
        self.init_return = init_return
        self.update_return = update_return
        self.create_return = create_return
        self.thread_handle = thread_handle
        self.fail_close_thread = fail_close_thread
        self.fail_terminate = fail_terminate
        self.wait_return = wait_return
        self.fail_wait = fail_wait
        self.fail_close_process = fail_close_process
        self.exit_code = STILL_ACTIVE

        self.deleted_called = False
        self.captured_flags: Optional[int] = None
        self.captured_env = None
        self.captured_attribute_count: Optional[int] = None
        self.captured_inherit: Optional[bool] = None
        self.captured_si_flags: Optional[int] = None
        self.captured_h_stdin: Optional[int] = None
        self.pipe_read_handle: int = 501
        self.pipe_write_handle: int = 502
        self.set_handle_info_calls: List[Tuple[int, int, int]] = []
        self.terminated_handles: List[Tuple[int, int]] = []
        self.closed_handles: List[int] = []
        self.close_handle_calls: List[int] = []

    def CreatePipe(self, p_read: object, p_write: object, p_attr: object, size: int) -> int:
        read_ptr = ctypes.cast(p_read, ctypes.POINTER(HANDLE))
        write_ptr = ctypes.cast(p_write, ctypes.POINTER(HANDLE))
        read_ptr.contents.value = self.pipe_read_handle
        write_ptr.contents.value = self.pipe_write_handle
        return 1

    def SetHandleInformation(self, h: object, mask: int, flags: int) -> int:
        h_val = getattr(h, "value", h) if not isinstance(h, int) else h
        self.set_handle_info_calls.append((h_val, mask, flags))
        return 1

    def InitializeProcThreadAttributeList(
        self, p_list: Optional[ctypes.c_void_p], count: int, flags: int, p_size: object
    ) -> int:
        self.captured_attribute_count = count
        size_ptr = ctypes.cast(p_size, ctypes.POINTER(SIZE_T))
        if p_list is None:
            ctypes.set_last_error(self.sizing_error)
            size_ptr.contents.value = self.sizing_size
            return self.sizing_return
        ctypes.set_last_error(0 if self.init_return else 87)
        return self.init_return

    def UpdateProcThreadAttribute(self, *args: object) -> int:
        ctypes.set_last_error(0 if self.update_return else 87)
        return self.update_return

    def CreateProcessW(
        self,
        app: object,
        cmd: object,
        p_attr: object,
        t_attr: object,
        inherit: bool,
        flags: int,
        env: object,
        cwd: object,
        si: object,
        pi: object,
    ) -> int:
        self.captured_flags = flags
        self.captured_env = env
        self.captured_inherit = inherit
        if si:
            si_ptr = ctypes.cast(si, ctypes.POINTER(STARTUPINFOW))
            self.captured_si_flags = si_ptr.contents.dwFlags
            self.captured_h_stdin = si_ptr.contents.hStdInput
        if self.create_return:
            pi_ptr = ctypes.cast(pi, ctypes.POINTER(PROCESS_INFORMATION))
            pi_ptr.contents.hProcess = 101
            pi_ptr.contents.dwProcessId = 2001
            pi_ptr.contents.hThread = self.thread_handle
            return 1
        ctypes.set_last_error(2)
        return 0

    def DeleteProcThreadAttributeList(self, p: object) -> None:
        self.deleted_called = True

    def CloseHandle(self, h: int) -> int:
        self.close_handle_calls.append(h)
        if h <= 0 or h == 0xFFFFFFFF:
            ctypes.set_last_error(6)  # ERROR_INVALID_HANDLE
            return 0
        if self.fail_close_thread and h == self.thread_handle:
            ctypes.set_last_error(6)  # ERROR_INVALID_HANDLE
            return 0
        if self.fail_close_process and h == 101:
            ctypes.set_last_error(6)  # ERROR_INVALID_HANDLE
            return 0
        self.closed_handles.append(h)
        return 1

    def TerminateProcess(self, h: int, code: int) -> int:
        if self.fail_terminate:
            ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED
            return 0
        self.terminated_handles.append((h, code))
        return 1

    def WaitForSingleObject(self, h: int, timeout_ms: int) -> int:
        if self.fail_wait:
            ctypes.set_last_error(6)
            return WAIT_FAILED
        if self.fail_terminate:
            return WAIT_TIMEOUT
        return self.wait_return

    def GetExitCodeProcess(self, h: int, p_code: object) -> int:
        code_ptr = ctypes.cast(p_code, ctypes.POINTER(DWORD))
        code_ptr.contents.value = self.exit_code
        return 1


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_win32_adapter_flags_include_unicode_env() -> None:
    """F-02: Verifies that CREATE_UNICODE_ENVIRONMENT is set when env is provided, and omitted when None."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit()
    adapter._kernel32 = fake_k32

    # Case 1: env provided -> includes CREATE_UNICODE_ENVIRONMENT
    adapter.create_process_in_job(100, "dummy.exe", env={"FOO": "BAR"})
    assert fake_k32.captured_flags is not None
    assert fake_k32.captured_flags & EXTENDED_STARTUPINFO_PRESENT
    assert fake_k32.captured_flags & CREATE_UNICODE_ENVIRONMENT
    assert fake_k32.deleted_called is True

    # Case 2: env is None -> omits CREATE_UNICODE_ENVIRONMENT
    fake_k32_no_env = FakeKernel32SizingAndInit()
    adapter._kernel32 = fake_k32_no_env
    adapter.create_process_in_job(100, "dummy.exe", env=None)
    assert fake_k32_no_env.captured_flags == EXTENDED_STARTUPINFO_PRESENT
    assert not (fake_k32_no_env.captured_flags & CREATE_UNICODE_ENVIRONMENT)
    assert fake_k32_no_env.deleted_called is True


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
@pytest.mark.parametrize(
    "sizing_ret, sizing_err, sizing_sz, expected_match",
    [
        (1, ERROR_INSUFFICIENT_BUFFER, 64, "expected FALSE"),
        (0, 87, 64, "ERROR_INSUFFICIENT_BUFFER"),
        (0, ERROR_INSUFFICIENT_BUFFER, 0, "size=0"),
    ],
)
def test_win32_adapter_sizing_failure_modes(
    sizing_ret: int, sizing_err: int, sizing_sz: int, expected_match: str
) -> None:
    """F-03: Verifies that attribute-list sizing call fails closed under unexpected results/errors."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        sizing_return=sizing_ret, sizing_error=sizing_err, sizing_size=sizing_sz
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(Win32APIError, match=expected_match):
        adapter.create_process_in_job(100, "dummy.exe")

    # When sizing fails, DeleteProcThreadAttributeList must NOT have been called
    assert fake_k32.deleted_called is False


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_win32_adapter_update_failure_calls_delete() -> None:
    """F-03: Verifies that if UpdateProcThreadAttribute fails, DeleteProcThreadAttributeList is called."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(update_return=0)
    adapter._kernel32 = fake_k32

    with pytest.raises(Win32APIError, match="UpdateProcThreadAttribute failed"):
        adapter.create_process_in_job(100, "dummy.exe")

    # Delete must be called in finally block
    assert fake_k32.deleted_called is True


# ---------------------------------------------------------------------------
# Phase 1F.3.4.4 Remediation Regression Tests (F-09 through F-15)
# ---------------------------------------------------------------------------

def test_process_handle_close_failure_preserves_handle_and_ownership() -> None:
    """F-09: Verifies ProcessHandle retains handle and unclosed state if CloseHandle fails."""
    adapter = MockWin32Adapter()
    proc = adapter.register_mock_process("backend", exit_code=0)
    original_handle = proc.h_process
    assert original_handle > 0
    assert proc._is_closed is False

    # Simulate CloseHandle failure
    adapter.fail_close_handle = True

    with pytest.raises(Win32APIError, match="CloseHandle failed"):
        proc.close()

    # Handle ownership and unclosed state must be preserved
    assert proc.h_process == original_handle
    assert proc._is_closed is False

    # Verify retryability: once CloseHandle succeeds, state transitions cleanly
    adapter.fail_close_handle = False
    proc.close()
    assert proc.h_process == 0
    assert proc._is_closed is True


def test_shutdown_job_handle_closure_failure_retains_handle(tmp_path: Path) -> None:
    """F-09 & F-12: Verifies shutdown preserves Job Object handle and records error if CloseHandle fails."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    h_job = adapter.create_job_object()
    l.h_job = h_job

    adapter.fail_close_handle_for.add(h_job)
    errors = l.shutdown()

    assert any(f"Job handle closure error for handle {h_job}" in err for err in errors)
    assert l.h_job == h_job  # Handle ownership must be retained, not discarded to None
    assert l.last_shutdown_errors == errors


def test_shutdown_wait_timeout_retains_process_reference_and_records_error(tmp_path: Path) -> None:
    """F-10 & F-11: Verifies shutdown does not treat WAIT_TIMEOUT as exit and retains process reference."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    b_proc = adapter.register_mock_process("backend", exit_code=None)
    l.backend_proc = b_proc

    # Simulate timeout on wait
    adapter.simulate_wait_timeout = True
    errors = l.shutdown(timeout_s=0.1)

    assert any("Wait timeout for backend" in err for err in errors)
    assert any("retained because exit was not confirmed" in err for err in errors)
    assert l.backend_proc is not None
    assert l.backend_proc.h_process == b_proc.h_process
    assert l.backend_proc._is_closed is False


def test_shutdown_wait_api_failure_retains_process_reference(tmp_path: Path) -> None:
    """F-10 & F-11: Verifies shutdown handles WaitForSingleObject API failure and retains reference."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    b_proc = adapter.register_mock_process("backend", exit_code=None)
    l.backend_proc = b_proc

    adapter.fail_wait = True
    errors = l.shutdown()

    assert any("Wait error for backend" in err for err in errors)
    assert l.backend_proc is not None
    assert l.backend_proc._is_closed is False


def test_shutdown_process_close_failure_retains_process_reference(tmp_path: Path) -> None:
    """F-09 & F-11: Verifies shutdown retains process reference if process CloseHandle fails."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    b_proc = adapter.register_mock_process("backend", exit_code=0)
    l.backend_proc = b_proc

    adapter.fail_close_handle_for.add(b_proc.h_process)
    errors = l.shutdown()

    assert any("Close handle error for backend" in err for err in errors)
    assert l.backend_proc is not None
    assert l.backend_proc._is_closed is False


def test_shutdown_distinguishes_already_exited_process(tmp_path: Path) -> None:
    """F-10: Verifies shutdown does not attempt termination on already exited processes."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    b_proc = adapter.register_mock_process("backend", exit_code=0)
    l.backend_proc = b_proc

    # Incur fail_terminate; if terminate is called on the already-exited process, it would fail
    adapter.fail_terminate = True

    errors = l.shutdown()
    assert len(errors) == 0
    assert len(adapter.terminated_processes) == 0  # No termination requested for exited process
    assert l.backend_proc is None
    assert b_proc._is_closed is True


# ---------------------------------------------------------------------------
# Phase 1F.3.4.5: Remediation A & B Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_thread_closure_failure_clean_termination_and_closure() -> None:
    """Remediation A: Initial thread closure failure triggers termination, bounded wait, and handle closure."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=777,
        wait_return=WAIT_OBJECT_0,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    err = exc_info.value
    assert err.primary_error == 6
    assert err.obligation is not None
    assert err.obligation.termination_requested is True
    assert err.obligation.termination_confirmed is True
    assert err.obligation.handle_closed is True
    assert err.obligation.is_complete is True
    assert err.obligation.h_process == 0

    assert (101, 1) in fake_k32.terminated_handles
    assert 101 in fake_k32.closed_handles
    assert fake_k32.deleted_called is True
    # Thread handle failed to close, so thread obligation is retained
    assert len(adapter.outstanding_cleanup_obligations) == 1
    assert adapter.outstanding_cleanup_obligations[0] is err.thread_obligation
    assert err.thread_obligation.h_thread == 777
    assert err.thread_obligation.handle_closed is False


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_thread_closure_failure_termination_fails_preserves_handle() -> None:
    """Remediation A: If TerminateProcess fails during cleanup, process handle is preserved and registered."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=777,
        fail_terminate=True,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    err = exc_info.value
    assert err.primary_error == 6
    assert err.obligation is not None
    assert err.obligation.termination_requested is False
    assert err.obligation.termination_confirmed is False
    assert err.obligation.handle_closed is False
    assert err.obligation.h_process == 101
    assert err.obligation.is_complete is False

    assert 101 not in fake_k32.closed_handles
    assert err.obligation in adapter.outstanding_cleanup_obligations
    assert any("TerminateProcess failed" in s for s in err.secondary_errors)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_thread_closure_failure_wait_timeout_preserves_handle() -> None:
    """Remediation A: If WaitForSingleObject times out, process handle is preserved and registered."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=777,
        wait_return=WAIT_TIMEOUT,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    err = exc_info.value
    assert err.obligation.termination_requested is True
    assert err.obligation.termination_confirmed is False
    assert err.obligation.handle_closed is False
    assert err.obligation.h_process == 101
    assert err.obligation.is_complete is False

    assert 101 not in fake_k32.closed_handles
    assert err.obligation in adapter.outstanding_cleanup_obligations
    assert any("WaitForSingleObject timed out" in s for s in err.secondary_errors)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_thread_closure_failure_process_close_fails_preserves_handle() -> None:
    """Remediation A: If CloseHandle on the process handle fails, handle ownership is preserved and registered."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=777,
        wait_return=WAIT_OBJECT_0,
        fail_close_process=True,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    err = exc_info.value
    assert err.obligation.termination_requested is True
    assert err.obligation.termination_confirmed is True
    assert err.obligation.handle_closed is False
    assert err.obligation.h_process == 101
    assert err.obligation.is_complete is False

    assert err.obligation in adapter.outstanding_cleanup_obligations
    assert any("CloseHandle failed for process handle" in s for s in err.secondary_errors)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_thread_closure_failure_cleanup_retry_succeeds() -> None:
    """Remediation A: An outstanding obligation can be retried and successfully transitions to complete."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=777,
        wait_return=WAIT_TIMEOUT,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    ob = exc_info.value.obligation
    assert ob.is_complete is False
    assert ob.h_process == 101

    # Simulate subsequent exit during retry
    fake_k32.wait_return = WAIT_OBJECT_0
    ok = ob.retry_cleanup(timeout_s=1.0)

    assert ok is True
    assert ob.is_complete is True
    assert ob.termination_confirmed is True
    assert ob.handle_closed is True
    assert ob.h_process == 0
    assert 101 in fake_k32.closed_handles


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_thread_closure_multiple_failures_preserves_primary_and_secondary_errors() -> None:
    """Remediation A: Thread handle error is preserved as primary error, secondary errors are preserved in exception."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=777,
        fail_terminate=True,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    err = exc_info.value
    assert err.primary_error == 6
    assert isinstance(err, Win32APIError)
    assert len(err.secondary_errors) >= 1
    assert "CloseHandle failed for initial thread handle" in str(err)
    assert "secondary cleanup errors" in str(err)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_thread_closure_outstanding_process_handle_registered_and_not_discarded(tmp_path: Path) -> None:
    """Remediation A: Uncleaned handles are tracked in outstanding_cleanup_obligations and retried during shutdown."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=777,
        wait_return=WAIT_TIMEOUT,
    )
    adapter._kernel32 = fake_k32

    l = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    with pytest.raises(ProcessCreationCleanupError):
        adapter.create_process_in_job(100, "dummy.exe")

    assert len(l.outstanding_cleanup_obligations) == 2
    assert any(isinstance(o, ThreadCleanupObligation) and o.h_thread == 777 for o in l.outstanding_cleanup_obligations)
    assert any(isinstance(o, ProcessCleanupObligation) and o.h_process == 101 for o in l.outstanding_cleanup_obligations)

    # Shutdown retries obligations
    fake_k32.wait_return = WAIT_OBJECT_0
    fake_k32.fail_close_thread = False
    errors = l.shutdown()
    assert len(l.outstanding_cleanup_obligations) == 0
    assert 101 in fake_k32.closed_handles
    assert 777 in fake_k32.closed_handles


def test_validate_platform_windows_10_client_build_boundaries(mock_app_tree: Path) -> None:
    """Remediation B: Windows 10 Client requires build 15063+; build 14393 on client is rejected."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    # Build 15063 (Client) passes
    adapter.version_info = WindowsVersionInfo(
        major=10, minor=0, build=15063, product_type=VER_NT_WORKSTATION
    )
    l.validate_platform()

    # Build 14393 (Client) is rejected
    adapter.version_info = WindowsVersionInfo(
        major=10, minor=0, build=14393, product_type=VER_NT_WORKSTATION
    )
    with pytest.raises(
        PlatformCompatibilityError,
        match=r"requires Windows 10 Version 1703 \(build 15063\) or later.*detected Windows client build 14393",
    ):
        l.validate_platform()


def test_validate_platform_windows_server_2016_accepted(mock_app_tree: Path) -> None:
    """Remediation B: Windows Server 2016 (build 14393) is accepted when product_type is Server/Domain Controller."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    # Server 2016 (VER_NT_SERVER) build 14393 passes
    adapter.version_info = WindowsVersionInfo(
        major=10, minor=0, build=14393, product_type=VER_NT_SERVER
    )
    l.validate_platform()

    # Server 2016 (VER_NT_DOMAIN_CONTROLLER) build 14393 passes
    adapter.version_info = WindowsVersionInfo(
        major=10, minor=0, build=14393, product_type=VER_NT_DOMAIN_CONTROLLER
    )
    l.validate_platform()


@pytest.mark.parametrize("build", [17763, 20348, 26100])
def test_validate_platform_windows_server_newer_accepted(mock_app_tree: Path, build: int) -> None:
    """Remediation B: Windows Server 2019 (17763), Server 2022 (20348), Server 2025 (26100) are accepted."""
    adapter = MockWin32Adapter()
    adapter.version_info = WindowsVersionInfo(
        major=10, minor=0, build=build, product_type=VER_NT_SERVER
    )
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)
    l.validate_platform()


@pytest.mark.parametrize(
    "major, build, product_type, desc",
    [
        (6, 7601, VER_NT_WORKSTATION, "Windows 7"),
        (6, 7601, VER_NT_SERVER, "Windows Server 2008 R2"),
        (6, 9600, VER_NT_WORKSTATION, "Windows 8.1"),
        (6, 9600, VER_NT_SERVER, "Windows Server 2012 R2"),
        (10, 10240, VER_NT_WORKSTATION, "Windows 10 Version 1507"),
        (10, 10586, VER_NT_SERVER, "Windows Server preview build 10586 (< 14393)"),
    ],
)
def test_validate_platform_unsupported_older_versions_rejected(
    mock_app_tree: Path, major: int, build: int, product_type: int, desc: str
) -> None:
    """Remediation B: Versions prior to Windows 10 (client) or Server 2016 (server) are rejected."""
    adapter = MockWin32Adapter()
    adapter.version_info = WindowsVersionInfo(
        major=major, minor=0, build=build, product_type=product_type
    )
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)
    with pytest.raises(PlatformCompatibilityError):
        l.validate_platform()


def test_validate_platform_version_query_failure(mock_app_tree: Path) -> None:
    """Remediation B: Failure to query Windows version/product type raises actionable PlatformCompatibilityError."""
    adapter = MockWin32Adapter()
    adapter.fail_check_version = True
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    with pytest.raises(
        PlatformCompatibilityError,
        match="Failed to query Windows operating system version and product type",
    ):
        l.validate_platform()


# ---------------------------------------------------------------------------
# Phase 1F.3.4.6 Regression Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_thread_closure_failure_retains_thread_handle() -> None:
    """1. Initial thread-handle closure failure retains the thread handle in an obligation."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=888,
        wait_return=WAIT_OBJECT_0,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    err = exc_info.value
    assert err.primary_error == 6
    assert err.thread_obligation is not None
    assert err.thread_obligation.h_thread == 888
    assert err.thread_obligation.handle_closed is False
    assert err.thread_obligation.is_complete is False
    assert err.thread_obligation in adapter.outstanding_cleanup_obligations
    assert 888 not in fake_k32.closed_handles


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_thread_handle_cleanup_retry_success() -> None:
    """2. Initial thread-handle cleanup retry succeeds and completes obligation."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=888,
        wait_return=WAIT_OBJECT_0,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    tob = exc_info.value.thread_obligation
    assert tob is not None
    assert tob.is_complete is False
    assert tob.h_thread == 888

    # Allow thread close to succeed on retry
    fake_k32.fail_close_thread = False
    ok = tob.retry_cleanup(timeout_s=1.0)
    assert ok is True
    assert tob.is_complete is True
    assert tob.handle_closed is True
    assert tob.h_thread == 0
    assert 888 in fake_k32.closed_handles


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_thread_handle_cleanup_retry_failure_retains_ownership() -> None:
    """3. Initial thread-handle cleanup retry failure retains ownership and exact handle."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=888,
        wait_return=WAIT_OBJECT_0,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    tob = exc_info.value.thread_obligation
    assert tob is not None

    # Thread close still fails on retry
    ok = tob.retry_cleanup(timeout_s=1.0)
    assert ok is False
    assert tob.is_complete is False
    assert tob.handle_closed is False
    assert tob.h_thread == 888  # Exact handle retained, not zeroed
    assert len(tob.secondary_errors) >= 1
    assert any("Retry thread close_handle error" in s for s in tob.secondary_errors)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_process_and_thread_handles_independent_cleanup_outcomes() -> None:
    """4. Process- and thread-handle cleanup obligations have independent outcomes."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=888,
        wait_return=WAIT_OBJECT_0,
        fail_close_process=True,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    err = exc_info.value
    pob = err.obligation
    tob = err.thread_obligation
    assert pob is not None and tob is not None
    assert pob.is_complete is False
    assert tob.is_complete is False

    # Scenario: retry thread succeeds, but process close still fails
    fake_k32.fail_close_thread = False
    fake_k32.fail_close_process = True

    t_ok = tob.retry_cleanup()
    p_ok = pob.retry_cleanup()

    assert t_ok is True
    assert tob.is_complete is True
    assert tob.h_thread == 0

    assert p_ok is False
    assert pob.is_complete is False
    assert pob.h_process == 101  # Process handle retained independently


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_process_handle_close_failure_retry_success() -> None:
    """5. Process-handle CloseHandle failure followed by successful retry without redundant termination."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=888,
        wait_return=WAIT_OBJECT_0,
        fail_close_process=True,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    pob = exc_info.value.obligation
    assert pob.termination_confirmed is True
    assert pob.handle_closed is False
    assert pob.h_process == 101

    initial_term_count = len(fake_k32.terminated_handles)

    # Now close succeeds on retry
    fake_k32.fail_close_process = False
    ok = pob.retry_cleanup(timeout_s=1.0)

    assert ok is True
    assert pob.is_complete is True
    assert pob.handle_closed is True
    assert pob.h_process == 0
    assert 101 in fake_k32.closed_handles
    # Redundant termination was not called because termination_confirmed was already True
    assert len(fake_k32.terminated_handles) == initial_term_count


def test_cleanup_obligation_rejects_missing_or_zero_handles() -> None:
    """6. Cleanup obligations reject missing or zero handles and do not mark complete."""
    adapter = MockWin32Adapter()

    # Process cleanup obligation with h_process = 0
    pob = ProcessCleanupObligation(
        h_process=0,
        pid=1234,
        adapter=adapter,
    )
    assert pob.retry_cleanup() is False
    assert pob.handle_closed is False
    assert pob.is_complete is False
    assert any("missing or invalid handle" in s for s in pob.secondary_errors)

    # Thread cleanup obligation with h_thread = 0
    tob = ThreadCleanupObligation(
        h_thread=0,
        adapter=adapter,
    )
    assert tob.retry_cleanup() is False
    assert tob.handle_closed is False
    assert tob.is_complete is False
    assert any("missing or invalid handle" in s for s in tob.secondary_errors)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_unexpected_cleanup_exception_preserves_primary_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """7. Unexpected exception during cleanup preserves the primary error and retains obligations."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=888,
    )
    adapter._kernel32 = fake_k32

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Kernel32 simulated internal crash")

    monkeypatch.setattr(adapter, "_cleanup_aborted_process", boom)

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    err = exc_info.value
    # Primary error preserved
    assert err.primary_error == 6
    # Process handle preserved
    assert err.obligation is not None
    assert err.obligation.h_process == 101
    assert err.obligation.handle_closed is False
    # Thread handle preserved
    assert err.thread_obligation is not None
    assert err.thread_obligation.h_thread == 888
    assert err.thread_obligation.handle_closed is False
    # Both obligations retained in adapter
    assert err.thread_obligation in adapter.outstanding_cleanup_obligations
    assert err.obligation in adapter.outstanding_cleanup_obligations
    # Crash recorded in secondary errors
    assert any("Kernel32 simulated internal crash" in s for s in err.secondary_errors)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_multiple_cleanup_failures_preserves_all_errors() -> None:
    """8. Multiple cleanup failures preserve all errors while keeping primary error."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=888,
        fail_terminate=True,
        fail_wait=True,
    )
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    err = exc_info.value
    assert err.primary_error == 6
    assert any("TerminateProcess failed" in s for s in err.secondary_errors)
    assert any("WaitForSingleObject" in s for s in err.secondary_errors)
    assert any("Process handle 101 retained" in s for s in err.secondary_errors)


def test_repeated_cleanup_attempts_do_not_double_close() -> None:
    """9. Repeated cleanup attempts do not double-close handles."""
    adapter = MockWin32Adapter()
    h = adapter.create_job_object()  # Creates open handle

    # Thread obligation test
    tob = ThreadCleanupObligation(h_thread=h, adapter=adapter)
    assert tob.retry_cleanup() is True
    assert tob.handle_closed is True
    assert tob.h_thread == 0
    assert h in adapter.closed_handles

    # Second retry must return True without calling close_handle again
    initial_close_count = len(adapter.closed_handles)
    assert tob.retry_cleanup() is True
    assert len(adapter.closed_handles) == initial_close_count

    # Process obligation test
    h_proc = adapter.create_job_object()  # Creates open handle
    pob = ProcessCleanupObligation(
        h_process=h_proc,
        pid=3000,
        adapter=adapter,
        termination_confirmed=True,
    )
    assert pob.retry_cleanup() is True
    assert pob.handle_closed is True
    assert pob.h_process == 0
    assert h_proc in adapter.closed_handles

    # Second retry must return True without calling close_handle again
    close_count_after = len(adapter.closed_handles)
    assert pob.retry_cleanup() is True
    assert len(adapter.closed_handles) == close_count_after


@pytest.mark.parametrize("invalid_type", [0, 4, 99, -1])
def test_validate_platform_rejects_invalid_product_types(mock_app_tree: Path, invalid_type: int) -> None:
    """10. validate_platform rejects invalid or unexpected product types fail-closed."""
    adapter = MockWin32Adapter()
    adapter.version_info = WindowsVersionInfo(
        major=10, minor=0, build=19045, product_type=invalid_type
    )
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)
    with pytest.raises(
        PlatformCompatibilityError,
        match="Unsupported or unrecognized Windows product type",
    ):
        l.validate_platform()


def test_validate_platform_valid_product_types_classification(mock_app_tree: Path) -> None:
    """11. validate_platform correctly classifies VER_NT_WORKSTATION, VER_NT_DOMAIN_CONTROLLER, and VER_NT_SERVER."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    # 1. VER_NT_WORKSTATION: build 15063 passes, build 15062 fails
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=15063, product_type=VER_NT_WORKSTATION)
    l.validate_platform()
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=15062, product_type=VER_NT_WORKSTATION)
    with pytest.raises(PlatformCompatibilityError):
        l.validate_platform()

    # 2. VER_NT_DOMAIN_CONTROLLER: build 14393 passes, build 14392 fails
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=14393, product_type=VER_NT_DOMAIN_CONTROLLER)
    l.validate_platform()
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=14392, product_type=VER_NT_DOMAIN_CONTROLLER)
    with pytest.raises(PlatformCompatibilityError):
        l.validate_platform()

    # 3. VER_NT_SERVER: build 14393 passes, build 14392 fails
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=14393, product_type=VER_NT_SERVER)
    l.validate_platform()
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=14392, product_type=VER_NT_SERVER)
    with pytest.raises(PlatformCompatibilityError):
        l.validate_platform()


def test_check_version_rtl_get_version_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """12. check_version succeeds via RtlGetVersion."""
    adapter = Win32Adapter()

    class FakeNtDll:
        def RtlGetVersion(self, p_info: Any) -> int:
            info_ptr = ctypes.cast(p_info, ctypes.POINTER(OSVERSIONINFOEXW))
            info_ptr.contents.dwMajorVersion = 10
            info_ptr.contents.dwMinorVersion = 0
            info_ptr.contents.dwBuildNumber = 19045
            info_ptr.contents.wProductType = VER_NT_WORKSTATION
            info_ptr.contents.dwPlatformId = 2
            return 0  # STATUS_SUCCESS

    adapter._ntdll = FakeNtDll()
    monkeypatch.setattr(adapter, "is_windows", lambda: True)

    info = adapter.check_version()
    assert info.major == 10
    assert info.build == 19045
    assert info.product_type == VER_NT_WORKSTATION
    assert info.is_valid_product_type is True
    assert info.is_workstation is True


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_check_version_rtl_get_version_failure_fallback_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """13. check_version falls back to sys.getwindowsversion() when RtlGetVersion fails."""
    adapter = Win32Adapter()
    adapter._ntdll = None  # Force fallback

    class FakeWindowsVersion:
        major = 10
        minor = 0
        build = 20348
        platform = 2
        product_type = VER_NT_SERVER

    monkeypatch.setattr(sys, "getwindowsversion", lambda: FakeWindowsVersion())
    monkeypatch.setattr(adapter, "is_windows", lambda: True)

    info = adapter.check_version()
    assert info.major == 10
    assert info.build == 20348
    assert info.product_type == VER_NT_SERVER
    assert info.is_server is True


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_check_version_fallback_missing_product_type_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """14. check_version rejects fallback without product_type fail-closed."""
    adapter = Win32Adapter()
    adapter._ntdll = None  # Force fallback

    class FakeWindowsVersionNoProductType:
        major = 10
        minor = 0
        build = 19045
        platform = 2

    monkeypatch.setattr(sys, "getwindowsversion", lambda: FakeWindowsVersionNoProductType())
    monkeypatch.setattr(adapter, "is_windows", lambda: True)

    with pytest.raises(Win32APIError, match="does not provide product_type"):
        adapter.check_version()


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_check_version_query_exceptions_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """15. check_version surfaces version query exceptions."""
    adapter = Win32Adapter()
    adapter._ntdll = None  # Force fallback

    def broken_windows_version() -> Any:
        raise OSError("Simulated low-level OS query fault")

    monkeypatch.setattr(sys, "getwindowsversion", broken_windows_version)
    monkeypatch.setattr(adapter, "is_windows", lambda: True)

    with pytest.raises(Win32APIError, match="Unable to query Windows OS version.*Simulated low-level OS query fault"):
        adapter.check_version()


# ---------------------------------------------------------------------------
# Phase 1F.3.4.7 Regression Tests
# ---------------------------------------------------------------------------

# Finding A: Product-Type Fail-Closed Validation

def test_validate_platform_omitted_product_type_rejected(mock_app_tree: Path) -> None:
    """16. Omitted product_type defaults to VER_NT_UNKNOWN (0) and is rejected fail-closed."""
    ver = WindowsVersionInfo(major=10, minor=0, build=19045)
    assert ver.product_type == VER_NT_UNKNOWN
    assert ver.is_valid_product_type is False
    assert ver.is_workstation is False
    assert ver.is_server is False

    adapter = MockWin32Adapter()
    adapter.version_info = ver
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    with pytest.raises(
        PlatformCompatibilityError,
        match=r"Unsupported or unrecognized Windows product type: 0",
    ):
        l.validate_platform()


def test_validate_platform_zero_product_type_rejected(mock_app_tree: Path) -> None:
    """17. Explicit product_type=0 is rejected fail-closed."""
    ver = WindowsVersionInfo(major=10, minor=0, build=19045, product_type=0)
    assert ver.product_type == 0
    assert ver.is_valid_product_type is False
    assert ver.is_workstation is False
    assert ver.is_server is False

    adapter = MockWin32Adapter()
    adapter.version_info = ver
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    with pytest.raises(
        PlatformCompatibilityError,
        match=r"Unsupported or unrecognized Windows product type: 0",
    ):
        l.validate_platform()


def test_validate_platform_none_product_type_rejected(mock_app_tree: Path) -> None:
    """18. Explicit product_type=None is rejected fail-closed."""
    ver = WindowsVersionInfo(major=10, minor=0, build=19045, product_type=None)
    assert ver.product_type is None
    assert ver.is_valid_product_type is False
    assert ver.is_workstation is False
    assert ver.is_server is False

    adapter = MockWin32Adapter()
    adapter.version_info = ver
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    with pytest.raises(
        PlatformCompatibilityError,
        match=r"Unsupported or unrecognized Windows product type: None",
    ):
        l.validate_platform()


@pytest.mark.parametrize("unknown_type", [4, 5, 99, 255, -1])
def test_validate_platform_unknown_product_types_rejected(mock_app_tree: Path, unknown_type: int) -> None:
    """19. Unknown product_type integers are rejected fail-closed."""
    ver = WindowsVersionInfo(major=10, minor=0, build=19045, product_type=unknown_type)
    assert ver.is_valid_product_type is False
    assert ver.is_workstation is False
    assert ver.is_server is False

    adapter = MockWin32Adapter()
    adapter.version_info = ver
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    with pytest.raises(
        PlatformCompatibilityError,
        match=f"Unsupported or unrecognized Windows product type: {unknown_type}",
    ):
        l.validate_platform()


def test_validate_platform_valid_workstation_accepted_and_bounded(mock_app_tree: Path) -> None:
    """20. Valid workstation (VER_NT_WORKSTATION) accepts build 15063+, rejects build 15062."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    # Valid build 15063 passes
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=15063, product_type=VER_NT_WORKSTATION)
    assert adapter.version_info.is_valid_product_type is True
    assert adapter.version_info.is_workstation is True
    assert adapter.version_info.is_server is False
    l.validate_platform()

    # Below minimum build 15062 fails
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=15062, product_type=VER_NT_WORKSTATION)
    with pytest.raises(PlatformCompatibilityError, match=r"requires Windows 10 Version 1703 \(build 15063\) or later"):
        l.validate_platform()


def test_validate_platform_valid_server_accepted_and_bounded(mock_app_tree: Path) -> None:
    """21. Valid server (VER_NT_SERVER) accepts build 14393+, rejects build 14392."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    # Valid build 14393 passes
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=14393, product_type=VER_NT_SERVER)
    assert adapter.version_info.is_valid_product_type is True
    assert adapter.version_info.is_workstation is False
    assert adapter.version_info.is_server is True
    l.validate_platform()

    # Below minimum build 14392 fails
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=14392, product_type=VER_NT_SERVER)
    with pytest.raises(PlatformCompatibilityError, match=r"requires Windows Server 2016 \(build 14393\) or later"):
        l.validate_platform()


def test_validate_platform_valid_domain_controller_accepted_and_bounded(mock_app_tree: Path) -> None:
    """22. Valid domain controller (VER_NT_DOMAIN_CONTROLLER) accepts build 14393+, rejects build 14392."""
    adapter = MockWin32Adapter()
    l = UnifiedLauncher(repo_root=mock_app_tree, adapter=adapter)

    # Valid build 14393 passes
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=14393, product_type=VER_NT_DOMAIN_CONTROLLER)
    assert adapter.version_info.is_valid_product_type is True
    assert adapter.version_info.is_workstation is False
    assert adapter.version_info.is_server is True
    l.validate_platform()

    # Below minimum build 14392 fails
    adapter.version_info = WindowsVersionInfo(major=10, minor=0, build=14392, product_type=VER_NT_DOMAIN_CONTROLLER)
    with pytest.raises(PlatformCompatibilityError, match=r"requires Windows Server 2016 \(build 14393\) or later"):
        l.validate_platform()


# Finding B: Fake Win32 Handle Integrity

@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_fake_kernel32_close_handle_rejects_zero_and_invalid_handles() -> None:
    """23. FakeKernel32SizingAndInit CloseHandle(0) and CloseHandle(-1) fail with ERROR_INVALID_HANDLE (6)."""
    fake_k32 = FakeKernel32SizingAndInit()
    assert fake_k32.thread_handle == 102  # Nonzero default

    # CloseHandle(0) must fail
    res_zero = fake_k32.CloseHandle(0)
    assert res_zero == 0
    assert ctypes.get_last_error() == 6
    assert 0 not in fake_k32.closed_handles
    assert 0 in fake_k32.close_handle_calls

    # CloseHandle(-1) must fail
    res_neg = fake_k32.CloseHandle(-1)
    assert res_neg == 0
    assert ctypes.get_last_error() == 6
    assert -1 not in fake_k32.closed_handles
    assert -1 in fake_k32.close_handle_calls


def test_fake_kernel32_valid_handles_close_normally() -> None:
    """24. FakeKernel32SizingAndInit closes valid nonzero handles normally."""
    fake_k32 = FakeKernel32SizingAndInit()

    # Valid thread handle
    res_thread = fake_k32.CloseHandle(102)
    assert res_thread == 1
    assert 102 in fake_k32.closed_handles

    # Valid process handle
    res_proc = fake_k32.CloseHandle(101)
    assert res_proc == 1
    assert 101 in fake_k32.closed_handles


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_fake_kernel32_failed_closure_retains_exact_handle_and_obligation() -> None:
    """25. Failed thread closure with nonzero default retains exact handle and cleanup obligation."""
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=102,
        wait_return=WAIT_OBJECT_0,
    )
    adapter = Win32Adapter()
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    err = exc_info.value
    assert err.primary_error == 6
    assert err.thread_obligation is not None
    assert err.thread_obligation.h_thread == 102
    assert err.thread_obligation.handle_closed is False
    assert err.thread_obligation.is_complete is False
    assert 102 not in fake_k32.closed_handles
    assert 102 in fake_k32.close_handle_calls
    assert err.thread_obligation in adapter.outstanding_cleanup_obligations


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_fake_kernel32_cleanup_retry_behavior_remains_correct() -> None:
    """26. Thread cleanup retry with nonzero default handle transitions cleanly to complete."""
    fake_k32 = FakeKernel32SizingAndInit(
        fail_close_thread=True,
        thread_handle=102,
        wait_return=WAIT_OBJECT_0,
    )
    adapter = Win32Adapter()
    adapter._kernel32 = fake_k32

    with pytest.raises(ProcessCreationCleanupError) as exc_info:
        adapter.create_process_in_job(100, "dummy.exe")

    tob = exc_info.value.thread_obligation
    assert tob is not None
    assert tob.is_complete is False
    assert tob.h_thread == 102

    # Transition to success on retry
    fake_k32.fail_close_thread = False
    ok = tob.retry_cleanup()
    assert ok is True
    assert tob.is_complete is True
    assert tob.handle_closed is True
    assert tob.h_thread == 0
    assert 102 in fake_k32.closed_handles


# ---------------------------------------------------------------------------
# Phase 1F.3.5.1 Packaging Stdin Pipe, Bundled Node, and Graceful Shutdown Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows platform")
def test_stdin_pipe_handle_inheritance_and_closure() -> None:
    """Verifies Win32Adapter handles stdin pipe creation, attribute inheritance, and parent closure."""
    adapter = Win32Adapter()
    fake_k32 = FakeKernel32SizingAndInit()
    adapter._kernel32 = fake_k32

    h_proc, pid, h_write = adapter.create_process_in_job(100, "dummy.exe", redirect_stdin=True)

    # 1. Attribute list count must be 2 when redirect_stdin is True
    assert fake_k32.captured_attribute_count == 2

    # 2. bInheritHandles must be True
    assert fake_k32.captured_inherit is True

    # 3. STARTF_USESTDHANDLES and hStdInput must be set to read handle
    assert fake_k32.captured_si_flags is not None
    assert fake_k32.captured_si_flags & STARTF_USESTDHANDLES
    assert fake_k32.captured_h_stdin == fake_k32.pipe_read_handle

    # 4. SetHandleInformation was called to mark read handle inheritable
    assert (fake_k32.pipe_read_handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) in fake_k32.set_handle_info_calls

    # 5. Parent copy of read handle was closed immediately post-spawn
    assert fake_k32.pipe_read_handle in fake_k32.closed_handles

    # 6. Write handle was returned to caller
    assert h_write == fake_k32.pipe_write_handle


def test_bundled_node_preferred_over_system_node(tmp_path: Path) -> None:
    """Verifies that start_frontend prefers repo_root/node/node.exe when present."""
    adapter = MockWin32Adapter()
    launcher = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    launcher.h_job = adapter.create_job_object()
    launcher.backend_port = 8000

    bundled_node_dir = tmp_path / "node"
    bundled_node_dir.mkdir(parents=True)
    bundled_node = bundled_node_dir / ("node.exe" if adapter.is_windows() else "node")
    bundled_node.write_text("# mock node binary", encoding="utf-8")

    def mock_on_spawn(h_proc: int, cmd: str, cwd: Optional[Path]) -> None:
        launcher.frontend_port_file.write_text("3001", encoding="utf-8")

    adapter.on_spawn = mock_on_spawn
    launcher.probe_http = lambda *args, **kwargs: None  # type: ignore[assignment]

    port = launcher.start_frontend()
    assert port == 3001
    assert len(adapter.spawn_history) == 1
    spawned_cmd = str(adapter.spawn_history[0]["command_line"])
    expected_node = f'"{bundled_node}"' if adapter.is_windows() else str(bundled_node)
    assert spawned_cmd.startswith(expected_node)
    assert adapter.spawn_history[0]["redirect_stdin"] is True


def test_bundled_node_path_with_spaces_properly_quoted(tmp_path: Path) -> None:
    """Verifies that bundled Node path with spaces is properly quoted."""
    space_root = tmp_path / "Program Files" / "Data Intelligence Lab"
    bundled_node_dir = space_root / "node"
    bundled_node_dir.mkdir(parents=True)
    bundled_node = bundled_node_dir / "node.exe"
    bundled_node.write_text("# mock node binary", encoding="utf-8")

    adapter = MockWin32Adapter()
    launcher = UnifiedLauncher(repo_root=space_root, adapter=adapter)
    launcher.h_job = adapter.create_job_object()
    launcher.backend_port = 8000

    def mock_on_spawn(h_proc: int, cmd: str, cwd: Optional[Path]) -> None:
        launcher.frontend_port_file.write_text("3002", encoding="utf-8")

    adapter.on_spawn = mock_on_spawn
    launcher.probe_http = lambda *args, **kwargs: None  # type: ignore[assignment]

    port = launcher.start_frontend()
    assert port == 3002
    spawned_cmd = str(adapter.spawn_history[0]["command_line"])
    assert spawned_cmd.startswith(f'"{bundled_node}"')


def test_shutdown_closes_stdin_first(tmp_path: Path) -> None:
    """Verifies that shutdown closes frontend stdin handle before terminating."""
    adapter = MockWin32Adapter()
    launcher = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    launcher.h_job = 100

    # Register frontend process with open stdin write handle
    frontend_proc = ProcessHandle("frontend", 101, 2001, adapter, h_stdin_write=502)
    adapter.created_process_handles.add(101)
    adapter.open_handles.add(101)
    adapter.open_handles.add(502)
    adapter.process_exit_codes[101] = None
    launcher.frontend_proc = frontend_proc

    # On stdin close, simulate process exiting gracefully
    original_close_handle = adapter.close_handle

    def mock_close_handle(h: int) -> None:
        if h == 502:
            adapter.process_exit_codes[101] = 0
        original_close_handle(h)

    adapter.close_handle = mock_close_handle  # type: ignore[assignment]

    errors = launcher.shutdown(timeout_s=1.0)
    assert not errors
    assert 502 in adapter.closed_handles
    assert 101 in adapter.closed_handles
    # Because exit was graceful, TerminateProcess should not have been called on 101
    assert not any(h == 101 for h, code in adapter.terminated_processes)


def test_shutdown_emergency_fallback_on_timeout(tmp_path: Path) -> None:
    """Verifies that if graceful shutdown times out, forced termination is triggered."""
    adapter = MockWin32Adapter()
    launcher = UnifiedLauncher(repo_root=tmp_path, adapter=adapter)
    launcher.h_job = 100

    # Register frontend process with open stdin write handle
    frontend_proc = ProcessHandle("frontend", 101, 2001, adapter, h_stdin_write=502)
    adapter.created_process_handles.add(101)
    adapter.open_handles.add(101)
    adapter.open_handles.add(502)
    adapter.process_exit_codes[101] = None  # Remains running even after stdin close
    launcher.frontend_proc = frontend_proc

    # Simulate timeout on initial graceful wait, then success after terminate
    call_count = [0]
    original_wait = adapter.wait_for_process

    def mock_wait(h: int, timeout_ms: int) -> int:
        call_count[0] += 1
        if call_count[0] == 1:
            return WAIT_TIMEOUT  # Graceful wait times out
        return WAIT_OBJECT_0  # After termination, wait succeeds

    adapter.wait_for_process = mock_wait  # type: ignore[assignment]

    errors = launcher.shutdown(timeout_s=0.1)
    # Stdin write handle was closed
    assert 502 in adapter.closed_handles
    # Forced termination was called as fallback
    assert any(h == 101 for h, code in adapter.terminated_processes)
    # Process handle was closed
    assert 101 in adapter.closed_handles



