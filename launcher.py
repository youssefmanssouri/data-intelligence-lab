"""
Data Intelligence Lab - Unified Windows Application Launcher
Orchestrates backend and frontend services using Windows Job Object creation-time
process assignment (PROC_THREAD_ATTRIBUTE_JOB_LIST) and dynamic loopback port allocation.

Project Owner Accepted Residual Risk:
    Abrupt termination of the launcher while CreateProcessW is executing has
    undocumented cleanup behavior under the public Microsoft Windows API contract.
    Transactional rollback and guaranteed cleanup in this specific interruption
    scenario are not established. This residual risk is accepted for the
    Data Intelligence Lab v1.0 Windows release.
    This architecture does NOT claim an absolute zero-orphan guarantee.

Supported Platforms:
    Windows 10 Version 1703 (Build 15063) and later (64-bit)
    Windows 11 (all builds) (64-bit)
    Windows Server 2016 (Build 14393) and later (64-bit)
    Windows Server 2019 / 2022 / 2025 (all builds) (64-bit)
    Operating system version and product type (client vs server) are explicitly queried
    using the Windows native kernel API RtlGetVersion (OSVERSIONINFOEXW.wProductType).
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import dataclasses
import enum
import logging
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("dil.launcher")

# ---------------------------------------------------------------------------
# Win32 Constants & Type Definitions
# ---------------------------------------------------------------------------

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
STILL_ACTIVE = 259
ERROR_INSUFFICIENT_BUFFER = 122

# Product types (wProductType in OSVERSIONINFOEXW from winnt.h)
VER_NT_UNKNOWN = 0x0000000
VER_NT_WORKSTATION = 0x0000001
VER_NT_DOMAIN_CONTROLLER = 0x0000002
VER_NT_SERVER = 0x0000003
VALID_PRODUCT_TYPES = {VER_NT_WORKSTATION, VER_NT_DOMAIN_CONTROLLER, VER_NT_SERVER}

# Minimum Windows version requirements:
# Authoritative Microsoft documentation confirms PROC_THREAD_ATTRIBUTE_JOB_LIST
# is supported on Windows 10 Version 1703 (Creators Update, Build 15063) for Client,
# and on Windows Server 2016 (Build 14393) for Server.
MINIMUM_WINDOWS_MAJOR = 10
MINIMUM_CLIENT_BUILD = 15063  # Windows 10 Version 1703 (Creators Update)

# Microsoft Visual C++ 2015-2022 Redistributable (x64). The bundle ships no Microsoft CRT;
# the bundled PyArrow and CPython load it from the system, and PyArrow's MSVC build needs
# msvcp140.dll from 14.44 or later. Never downloaded or installed by the application.
MINIMUM_VC_RUNTIME = (14, 44)
VC_RUNTIME_REG_KEY = r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
VC_RUNTIME_DOWNLOAD_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
MINIMUM_SERVER_BUILD = 14393  # Windows Server 2016 RTM
# Backward-compatibility alias
MINIMUM_WINDOWS_BUILD = MINIMUM_CLIENT_BUILD

SIZE_T = ctypes.c_size_t
DWORD = wintypes.DWORD
HANDLE = wintypes.HANDLE
BOOL = wintypes.BOOL
LPWSTR = wintypes.LPWSTR
LPVOID = wintypes.LPVOID
WORD = wintypes.WORD
BYTE = wintypes.BYTE


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", LPWSTR),
        ("lpDesktop", LPWSTR),
        ("lpTitle", LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", WORD),
        ("cbReserved2", WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class OSVERSIONINFOEXW(ctypes.Structure):
    _fields_ = [
        ("dwOSVersionInfoSize", DWORD),
        ("dwMajorVersion", DWORD),
        ("dwMinorVersion", DWORD),
        ("dwBuildNumber", DWORD),
        ("dwPlatformId", DWORD),
        ("szCSDVersion", wintypes.WCHAR * 128),
        ("wServicePackMajor", WORD),
        ("wServicePackMinor", WORD),
        ("wSuiteMask", WORD),
        ("wProductType", BYTE),
        ("wReserved", BYTE),
    ]


# ---------------------------------------------------------------------------
# Process State Representation & Custom Exceptions
# ---------------------------------------------------------------------------

class ProcessState(enum.Enum):
    """Represents the distinct observable execution states of an owned child process."""
    RUNNING = "RUNNING"
    EXITED = "EXITED"
    QUERY_FAILED = "QUERY_FAILED"


@dataclasses.dataclass(frozen=True)
class ProcessStatusResult:
    """Immutable result of a process state query."""
    state: ProcessState
    exit_code: Optional[int] = None
    error: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class WindowsVersionInfo:
    """
    Encapsulates Windows OS version, build, and product type.
    Distinguishes Windows Client (Workstation) from Windows Server.
    """
    major: int
    minor: int
    build: int
    product_type: Optional[int] = VER_NT_UNKNOWN
    platform_id: int = 2

    def __iter__(self):
        return iter((self.major, self.build))

    @property
    def is_valid_product_type(self) -> bool:
        return self.product_type in VALID_PRODUCT_TYPES

    @property
    def is_server(self) -> bool:
        return self.product_type in (VER_NT_SERVER, VER_NT_DOMAIN_CONTROLLER)

    @property
    def is_workstation(self) -> bool:
        return self.product_type == VER_NT_WORKSTATION


@dataclasses.dataclass
class ThreadCleanupObligation:
    """
    Represents an unfinalized thread cleanup obligation resulting from a failed initial thread closure.
    Retains ownership of the native thread handle, enabling safe discovery and bounded retry (Phase 1F.3.4.6).
    """
    h_thread: int
    adapter: IWin32Adapter
    handle_closed: bool = False
    secondary_errors: List[str] = dataclasses.field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.handle_closed

    def retry_cleanup(self, timeout_s: float = 2.0) -> bool:
        """
        Retries closing the initial thread handle.
        Returns True if the thread handle is successfully closed and cleanup completes.
        Does not close twice if already closed.
        Rejects missing or zero handles.
        """
        if self.handle_closed:
            return True

        if not self.h_thread:
            self.secondary_errors.append("Cannot retry thread cleanup: missing or invalid handle (0)")
            return False

        try:
            self.adapter.close_handle(self.h_thread)
            self.h_thread = 0
            self.handle_closed = True
            return True
        except Exception as exc:
            self.secondary_errors.append(f"Retry thread close_handle error: {exc}")
            return False


@dataclasses.dataclass
class ProcessCleanupObligation:
    """
    Represents an unfinalized process cleanup obligation resulting from an aborted creation.
    Retains ownership of the native process handle, PID, and termination status, enabling
    safe discovery and bounded retry of child termination and handle closure (Phase 1F.3.4.5, 1F.3.4.6).
    """
    h_process: int
    pid: int
    adapter: IWin32Adapter
    termination_requested: bool = False
    termination_confirmed: bool = False
    handle_closed: bool = False
    secondary_errors: List[str] = dataclasses.field(default_factory=list)
    h_thread: int = 0
    thread_obligation: Optional[ThreadCleanupObligation] = None

    @property
    def is_complete(self) -> bool:
        return self.handle_closed

    def retry_cleanup(self, timeout_s: float = 2.0) -> bool:
        """
        Retries child termination (if unconfirmed), bounded waiting, and handle closure.
        Returns True if the process handle is successfully closed and cleanup completes.
        Rejects missing or zero handles.
        Avoids redundant termination requests if termination is already confirmed.
        """
        if self.handle_closed:
            return True

        if not self.h_process:
            self.secondary_errors.append("Cannot retry process cleanup: missing or invalid handle (0)")
            return False

        timeout_ms = int(timeout_s * 1000)

        # Step 1 & 2: If termination not yet confirmed, request termination and wait
        if not self.termination_confirmed:
            if not self.termination_requested:
                try:
                    self.adapter.terminate_process(self.h_process, 1)
                    self.termination_requested = True
                except Exception as exc:
                    self.secondary_errors.append(f"Retry terminate error: {exc}")

            try:
                wait_res = self.adapter.wait_for_process(self.h_process, timeout_ms)
                if wait_res == WAIT_OBJECT_0:
                    self.termination_confirmed = True
                elif wait_res == WAIT_TIMEOUT:
                    self.secondary_errors.append(f"Retry wait timeout after {timeout_ms}ms")
                else:
                    self.secondary_errors.append(f"Retry wait unexpected status: {wait_res}")
            except Exception as exc:
                self.secondary_errors.append(f"Retry wait error: {exc}")

        # Step 3: Handle closure only if termination confirmed
        if self.termination_confirmed:
            try:
                self.adapter.close_handle(self.h_process)
                self.h_process = 0
                self.handle_closed = True
                return True
            except Exception as exc:
                self.secondary_errors.append(f"Retry close_handle error: {exc}")
                return False

        return False


class LauncherError(Exception):
    """Base exception for launcher errors."""


class PlatformCompatibilityError(LauncherError):
    """Raised when running on an unsupported OS or architecture."""


class Win32APIError(LauncherError):
    """Raised when a Win32 system call fails."""


class ProcessCreationCleanupError(Win32APIError):
    """
    Raised when initial thread-handle closure fails during process creation.
    Preserves the primary thread-handle failure, secondary cleanup errors,
    and retains ownership of unfinalized handles in retryable obligations (Phase 1F.3.4.5, 1F.3.4.6).
    """
    def __init__(
        self,
        message: str,
        primary_error: int,
        obligation: Optional[ProcessCleanupObligation] = None,
        secondary_errors: Optional[List[str]] = None,
        thread_obligation: Optional[ThreadCleanupObligation] = None,
    ) -> None:
        super().__init__(message)
        self.primary_error = primary_error
        self.obligation = obligation
        self.secondary_errors = list(secondary_errors or [])
        self.thread_obligation = thread_obligation


class PortFileError(LauncherError):
    """Raised when port file announcement is invalid, missing, or corrupt."""


class ServiceReadinessError(LauncherError):
    """Raised when a service fails liveness or readiness probes."""


class ProcessEarlyExitError(LauncherError):
    """Raised when a child process exits prematurely during startup."""


class ProcessQueryError(LauncherError):
    """Raised when process state or exit code cannot be determined."""


# ---------------------------------------------------------------------------
# Environment Formatting Helper
# ---------------------------------------------------------------------------

def format_environment_block(env: Dict[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    """
    Constructs a null-terminated, double-null-terminated Unicode (UTF-16) environment block.
    Entries are sorted alphabetically by variable name case-insensitively, in strict
    compliance with the Win32 CreateProcessW environment block contract.
    """
    sorted_items = sorted(env.items(), key=lambda item: (item[0].upper(), item[0]))
    env_lines = [f"{k}={v}" for k, v in sorted_items]
    env_string = "\0".join(env_lines) + "\0\0" if env_lines else "\0\0"
    return ctypes.create_unicode_buffer(env_string)


# ---------------------------------------------------------------------------
# Win32 API Adapter Interface & Production Implementation
# ---------------------------------------------------------------------------

class IWin32Adapter:
    """Interface for Win32 process creation and Job Object operations."""

    def __init__(self) -> None:
        self.outstanding_cleanup_obligations: List[Union[ProcessCleanupObligation, ThreadCleanupObligation]] = []

    def is_windows(self) -> bool:
        raise NotImplementedError

    def check_version(self) -> WindowsVersionInfo:
        raise NotImplementedError

    def create_job_object(self) -> int:
        raise NotImplementedError

    def set_job_kill_on_close(self, h_job: int) -> None:
        raise NotImplementedError

    def create_process_in_job(
        self,
        h_job: int,
        command_line: str,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        redirect_stdin: bool = False,
    ) -> Tuple[int, int, Optional[int]]:
        """Returns (hProcess, dwProcessId, Optional[h_stdin_write])."""
        raise NotImplementedError

    def wait_for_process(self, h_process: int, timeout_ms: int) -> int:
        raise NotImplementedError

    def query_process_status(self, h_process: int) -> ProcessStatusResult:
        raise NotImplementedError

    def get_process_exit_code(self, h_process: int) -> Optional[int]:
        raise NotImplementedError

    def terminate_process(self, h_process: int, exit_code: int = 1) -> None:
        raise NotImplementedError

    def close_handle(self, handle: int) -> None:
        raise NotImplementedError

    def query_vc_runtime_version(self) -> Optional[Tuple[int, int, int]]:
        """Returns the installed VC++ 2015-2022 x64 runtime (major, minor, build), or None."""
        raise NotImplementedError


class Win32Adapter(IWin32Adapter):
    """Production Win32 API adapter using ctypes.WinDLL('kernel32', use_last_error=True)."""

    def __init__(self) -> None:
        super().__init__()
        self.outstanding_cleanup_obligations: List[Union[ProcessCleanupObligation, ThreadCleanupObligation]] = []
        if sys.platform == "win32":
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            self._init_api_signatures()
        else:
            self._kernel32 = None
            self._ntdll = None

    def _init_api_signatures(self) -> None:
        """Declares explicit argtypes and restype for all Win32 functions to ensure 64-bit ABI safety."""
        k32 = self._kernel32

        # CreateJobObjectW
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]

        # SetInformationJobObject
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]

        # InitializeProcThreadAttributeList
        k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        k32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(SIZE_T),
        ]

        # UpdateProcThreadAttribute
        k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        k32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,  # DWORD_PTR / SIZE_T
            wintypes.LPVOID,
            SIZE_T,
            wintypes.LPVOID,
            ctypes.POINTER(SIZE_T),
        ]

        # DeleteProcThreadAttributeList
        k32.DeleteProcThreadAttributeList.restype = None
        k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

        # CreateProcessW
        k32.CreateProcessW.restype = wintypes.BOOL
        k32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_INFORMATION),
        ]

        # WaitForSingleObject
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

        # GetExitCodeProcess
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

        # TerminateProcess
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]

        # CloseHandle
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        # CreatePipe
        k32.CreatePipe.restype = wintypes.BOOL
        k32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.c_void_p,
            wintypes.DWORD,
        ]

        # SetHandleInformation
        k32.SetHandleInformation.restype = wintypes.BOOL
        k32.SetHandleInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        ]

        # RtlGetVersion (ntdll)
        if self._ntdll:
            self._ntdll.RtlGetVersion.restype = ctypes.c_long
            self._ntdll.RtlGetVersion.argtypes = [ctypes.POINTER(OSVERSIONINFOEXW)]

    def is_windows(self) -> bool:
        return sys.platform == "win32"

    def check_version(self) -> WindowsVersionInfo:
        """
        Queries Windows OS version and product type using RtlGetVersion.
        Falls back to sys.getwindowsversion() if RtlGetVersion fails or is unavailable.
        Fails closed with Win32APIError if neither can supply a valid product type.
        """
        if not self.is_windows():
            return WindowsVersionInfo(major=0, minor=0, build=0, product_type=VER_NT_UNKNOWN)

        rtl_exc: Optional[Exception] = None
        if self._ntdll:
            try:
                info = OSVERSIONINFOEXW()
                info.dwOSVersionInfoSize = ctypes.sizeof(OSVERSIONINFOEXW)
                status = self._ntdll.RtlGetVersion(ctypes.byref(info))
                if status != 0:
                    raise Win32APIError(f"RtlGetVersion failed with NTSTATUS {status}")

                return WindowsVersionInfo(
                    major=info.dwMajorVersion,
                    minor=info.dwMinorVersion,
                    build=info.dwBuildNumber,
                    product_type=info.wProductType,
                    platform_id=info.dwPlatformId,
                )
            except Exception as exc:
                rtl_exc = exc
                logger.warning(f"RtlGetVersion failed ({exc}); falling back to sys.getwindowsversion()")

        try:
            v = sys.getwindowsversion()
            product_type = getattr(v, "product_type", None)
            if product_type is None:
                raise Win32APIError(
                    "sys.getwindowsversion() does not provide product_type; cannot reliably determine Windows edition"
                )
            return WindowsVersionInfo(
                major=v.major,
                minor=v.minor,
                build=v.build,
                product_type=product_type,
                platform_id=getattr(v, "platform", 2),
            )
        except Exception as fallback_exc:
            cause_msg = f" (RtlGetVersion failure: {rtl_exc})" if rtl_exc else ""
            raise Win32APIError(
                f"Unable to query Windows OS version: {fallback_exc}{cause_msg}"
            ) from fallback_exc

    def create_job_object(self) -> int:
        # Anonymous Job Object (lpName = None), non-inheritable (lpJobAttributes = None)
        h = self._kernel32.CreateJobObjectW(None, None)
        if not h:
            err = ctypes.get_last_error()
            raise Win32APIError(f"CreateJobObjectW failed with error code {err}")
        return h

    def set_job_kill_on_close(self, h_job: int) -> None:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        res = self._kernel32.SetInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not res:
            err = ctypes.get_last_error()
            raise Win32APIError(f"SetInformationJobObject failed with error code {err}")

    def _cleanup_aborted_process(
        self, h_process: int, pid: int, timeout_ms: int = 2000
    ) -> ProcessCleanupObligation:
        """
        Executes bounded, fail-closed cleanup of a child process after creation-time failure.
        Attempts termination, verifies exit via bounded wait, and attempts process handle closure.
        Preserves ownership in a ProcessCleanupObligation if any step fails (Phase 1F.3.4.5, 1F.3.4.6).
        """
        obligation = ProcessCleanupObligation(
            h_process=h_process,
            pid=pid,
            adapter=self,
        )

        if not h_process:
            obligation.secondary_errors.append("Cannot clean up aborted process: missing or invalid handle (0)")
            return obligation

        # Step 1: Attempt child termination
        try:
            term_res = self._kernel32.TerminateProcess(h_process, 1)
            if term_res:
                obligation.termination_requested = True
            else:
                term_err = ctypes.get_last_error()
                # If process has already exited, TerminateProcess returns FALSE with ERROR_ACCESS_DENIED (5)
                exit_code_dw = DWORD(0)
                try:
                    if (
                        self._kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code_dw))
                        and exit_code_dw.value != STILL_ACTIVE
                    ):
                        obligation.termination_requested = True
                        obligation.termination_confirmed = True
                    else:
                        obligation.secondary_errors.append(
                            f"TerminateProcess failed for handle {h_process} with error code {term_err}"
                        )
                except Exception as get_code_exc:
                    obligation.secondary_errors.append(
                        f"TerminateProcess failed ({term_err}) and GetExitCodeProcess raised: {get_code_exc}"
                    )
        except Exception as term_exc:
            obligation.secondary_errors.append(f"TerminateProcess raised unexpected exception: {term_exc}")

        # Step 2: Establish whether child termination actually completed via bounded wait
        if not obligation.termination_confirmed:
            try:
                wait_res = self._kernel32.WaitForSingleObject(h_process, DWORD(timeout_ms))
                if wait_res == WAIT_OBJECT_0:
                    obligation.termination_confirmed = True
                elif wait_res == WAIT_TIMEOUT:
                    obligation.secondary_errors.append(
                        f"WaitForSingleObject timed out after {timeout_ms}ms: child termination not confirmed"
                    )
                elif wait_res == WAIT_FAILED:
                    wait_err = ctypes.get_last_error()
                    obligation.secondary_errors.append(
                        f"WaitForSingleObject failed for handle {h_process} with error code {wait_err}"
                    )
                else:
                    obligation.secondary_errors.append(
                        f"WaitForSingleObject returned unexpected status {wait_res}"
                    )
            except Exception as wait_exc:
                obligation.secondary_errors.append(f"WaitForSingleObject raised unexpected exception: {wait_exc}")

        # Step 3: Check and record process-handle closure results only if termination was confirmed
        if obligation.termination_confirmed:
            try:
                close_res = self._kernel32.CloseHandle(h_process)
                if close_res:
                    obligation.handle_closed = True
                    obligation.h_process = 0
                else:
                    close_err = ctypes.get_last_error()
                    obligation.secondary_errors.append(
                        f"CloseHandle failed for process handle {h_process} with error code {close_err}"
                    )
            except Exception as close_exc:
                obligation.secondary_errors.append(f"CloseHandle raised unexpected exception: {close_exc}")
        else:
            obligation.secondary_errors.append(
                f"Process handle {h_process} retained because child termination was not confirmed"
            )

        return obligation

    def create_process_in_job(
        self,
        h_job: int,
        command_line: str,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        redirect_stdin: bool = False,
    ) -> Tuple[int, int, Optional[int]]:
        """
        Creates a child process assigned to h_job at creation time using
        STARTUPINFOEXW and PROC_THREAD_ATTRIBUTE_JOB_LIST.
        If redirect_stdin is True, creates a dedicated anonymous pipe, configures
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherits only the read handle,
        closes the parent's read handle immediately upon successful process creation,
        and returns the write handle for EOF signaling.
        Primary thread handle is closed immediately upon return.
        """
        h_stdin_read: Optional[int] = None
        h_stdin_write: Optional[int] = None

        if redirect_stdin:
            h_read_raw = wintypes.HANDLE()
            h_write_raw = wintypes.HANDLE()
            pipe_res = self._kernel32.CreatePipe(
                ctypes.byref(h_read_raw),
                ctypes.byref(h_write_raw),
                None,
                0,
            )
            if not pipe_res:
                err = ctypes.get_last_error()
                raise Win32APIError(f"CreatePipe failed with error code {err}")

            # Mark only the read handle as inheritable
            set_res = self._kernel32.SetHandleInformation(
                h_read_raw,
                HANDLE_FLAG_INHERIT,
                HANDLE_FLAG_INHERIT,
            )
            if not set_res:
                err = ctypes.get_last_error()
                self._kernel32.CloseHandle(h_read_raw)
                self._kernel32.CloseHandle(h_write_raw)
                raise Win32APIError(f"SetHandleInformation failed with error code {err}")

            h_stdin_read = h_read_raw.value
            h_stdin_write = h_write_raw.value

        attr_count = 2 if redirect_stdin else 1
        size_required = SIZE_T(0)
        # Sizing call: MSDN specifies this returns FALSE and sets ERROR_INSUFFICIENT_BUFFER
        res = self._kernel32.InitializeProcThreadAttributeList(None, attr_count, 0, ctypes.byref(size_required))
        err = ctypes.get_last_error()
        if res or err != ERROR_INSUFFICIENT_BUFFER or size_required.value == 0:
            if h_stdin_read:
                self._kernel32.CloseHandle(h_stdin_read)
            if h_stdin_write:
                self._kernel32.CloseHandle(h_stdin_write)
            raise Win32APIError(
                f"InitializeProcThreadAttributeList sizing failed: expected FALSE and ERROR_INSUFFICIENT_BUFFER (122), "
                f"got res={res}, error code={err}, size={size_required.value}"
            )

        # Allocate buffer for attribute list and pin memory
        attr_list_buf = (BYTE * size_required.value)()
        p_attr_list = ctypes.cast(attr_list_buf, ctypes.c_void_p)

        init_res = self._kernel32.InitializeProcThreadAttributeList(
            p_attr_list, attr_count, 0, ctypes.byref(size_required)
        )
        if not init_res:
            err = ctypes.get_last_error()
            if h_stdin_read:
                self._kernel32.CloseHandle(h_stdin_read)
            if h_stdin_write:
                self._kernel32.CloseHandle(h_stdin_write)
            raise Win32APIError(f"InitializeProcThreadAttributeList init failed with error code {err}")

        # Attribute list initialized; guaranteed cleanup via finally block
        try:
            job_array = (HANDLE * 1)(HANDLE(h_job))
            update_res = self._kernel32.UpdateProcThreadAttribute(
                p_attr_list,
                0,
                PROC_THREAD_ATTRIBUTE_JOB_LIST,
                job_array,
                ctypes.sizeof(job_array),
                None,
                None,
            )
            if not update_res:
                err = ctypes.get_last_error()
                raise Win32APIError(f"UpdateProcThreadAttribute failed with error code {err}")

            if redirect_stdin and h_stdin_read:
                handle_array = (HANDLE * 1)(HANDLE(h_stdin_read))
                update_res2 = self._kernel32.UpdateProcThreadAttribute(
                    p_attr_list,
                    0,
                    PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                    handle_array,
                    ctypes.sizeof(handle_array),
                    None,
                    None,
                )
                if not update_res2:
                    err = ctypes.get_last_error()
                    raise Win32APIError(f"UpdateProcThreadAttribute failed with error code {err}")

            si_ex = STARTUPINFOEXW()
            si_ex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
            si_ex.lpAttributeList = p_attr_list.value

            if redirect_stdin and h_stdin_read:
                si_ex.StartupInfo.dwFlags |= STARTF_USESTDHANDLES
                si_ex.StartupInfo.hStdInput = HANDLE(h_stdin_read)
                b_inherit = True
            else:
                b_inherit = False

            pi = PROCESS_INFORMATION()

            # Command line must be in a writable Unicode buffer
            cmd_buf = ctypes.create_unicode_buffer(command_line)
            cwd_str = str(cwd.resolve()) if cwd is not None else None

            # Environment block formatting and creation flags
            creation_flags = EXTENDED_STARTUPINFO_PRESENT
            env_block = None
            if env is not None:
                creation_flags |= CREATE_UNICODE_ENVIRONMENT
                env_block = format_environment_block(env)

            create_res = self._kernel32.CreateProcessW(
                None,
                cmd_buf,
                None,
                None,
                b_inherit,
                creation_flags,
                env_block,
                cwd_str,
                ctypes.byref(si_ex.StartupInfo),
                ctypes.byref(pi),
            )

            if not create_res:
                err = ctypes.get_last_error()
                raise Win32APIError(f"CreateProcessW failed with error code {err}")

            # Process successfully created; close parent's read handle immediately.
            # The child process has inherited its own open handle.
            if h_stdin_read:
                self._kernel32.CloseHandle(h_stdin_read)
                h_stdin_read = None

            # Close the initial thread handle immediately; it is not needed.
            # If closing the initial thread handle fails, process creation must NOT be
            # reported as successful. We execute fail-closed cleanup, preserve the
            # thread and process handles in explicit obligations, and raise
            # ProcessCreationCleanupError (Phase 1F.3.4.5, 1F.3.4.6).
            if pi.hThread:
                close_thread_res = self._kernel32.CloseHandle(pi.hThread)
                if not close_thread_res:
                    thread_err = ctypes.get_last_error()
                    logger.error(
                        f"CloseHandle failed for initial thread handle: {thread_err}; aborting child process"
                    )

                    thread_obligation = ThreadCleanupObligation(
                        h_thread=pi.hThread,
                        adapter=self,
                    )
                    self.outstanding_cleanup_obligations.append(thread_obligation)

                    proc_obligation = None
                    cleanup_exceptions: List[str] = []
                    try:
                        proc_obligation = self._cleanup_aborted_process(
                            h_process=pi.hProcess,
                            pid=pi.dwProcessId,
                            timeout_ms=2000,
                        )
                    except Exception as cleanup_exc:
                        logger.error(f"Unexpected exception during aborted process cleanup: {cleanup_exc}")
                        cleanup_exceptions.append(f"Aborted process cleanup unexpected exception: {cleanup_exc}")
                        proc_obligation = ProcessCleanupObligation(
                            h_process=pi.hProcess,
                            pid=pi.dwProcessId,
                            adapter=self,
                            termination_requested=False,
                            termination_confirmed=False,
                            handle_closed=False,
                            secondary_errors=[f"Aborted process cleanup unexpected exception: {cleanup_exc}"],
                        )

                    proc_obligation.h_thread = pi.hThread
                    proc_obligation.thread_obligation = thread_obligation

                    if not proc_obligation.is_complete:
                        self.outstanding_cleanup_obligations.append(proc_obligation)

                    all_secondary_errors = list(cleanup_exceptions)
                    if proc_obligation:
                        all_secondary_errors.extend(proc_obligation.secondary_errors)
                    if thread_obligation.secondary_errors:
                        all_secondary_errors.extend(thread_obligation.secondary_errors)

                    sec_msg = (
                        f"; secondary cleanup errors: {'; '.join(all_secondary_errors)}"
                        if all_secondary_errors
                        else ""
                    )
                    raise ProcessCreationCleanupError(
                        f"CloseHandle failed for initial thread handle with error code {thread_err}; "
                        f"process creation aborted{sec_msg}",
                        primary_error=thread_err,
                        obligation=proc_obligation,
                        secondary_errors=all_secondary_errors,
                        thread_obligation=thread_obligation,
                    )

            return pi.hProcess, pi.dwProcessId, h_stdin_write
        except Exception:
            if h_stdin_read:
                try:
                    self._kernel32.CloseHandle(h_stdin_read)
                except Exception:
                    pass
            if h_stdin_write:
                try:
                    self._kernel32.CloseHandle(h_stdin_write)
                except Exception:
                    pass
            raise
        finally:
            self._kernel32.DeleteProcThreadAttributeList(p_attr_list)

    def wait_for_process(self, h_process: int, timeout_ms: int) -> int:
        if not h_process:
            return WAIT_OBJECT_0
        res = self._kernel32.WaitForSingleObject(h_process, DWORD(timeout_ms))
        if res == WAIT_FAILED:
            err = ctypes.get_last_error()
            raise Win32APIError(f"WaitForSingleObject failed for handle {h_process} with error code {err}")
        return res

    def query_process_status(self, h_process: int) -> ProcessStatusResult:
        if not h_process:
            return ProcessStatusResult(ProcessState.EXITED, exit_code=-1)

        exit_code = DWORD(0)
        res = self._kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
        if not res:
            err = ctypes.get_last_error()
            return ProcessStatusResult(
                state=ProcessState.QUERY_FAILED,
                error=f"GetExitCodeProcess failed with error code {err}",
            )

        if exit_code.value == STILL_ACTIVE:
            wait_res = self._kernel32.WaitForSingleObject(h_process, 0)
            if wait_res == WAIT_TIMEOUT:
                return ProcessStatusResult(state=ProcessState.RUNNING)
            elif wait_res == WAIT_OBJECT_0:
                # Process legitimately exited with code STILL_ACTIVE (259)
                return ProcessStatusResult(state=ProcessState.EXITED, exit_code=STILL_ACTIVE)
            elif wait_res == WAIT_FAILED:
                err = ctypes.get_last_error()
                return ProcessStatusResult(
                    state=ProcessState.QUERY_FAILED,
                    error=f"WaitForSingleObject failed with error code {err}",
                )
            else:
                return ProcessStatusResult(
                    state=ProcessState.QUERY_FAILED,
                    error=f"WaitForSingleObject returned unexpected status {wait_res}",
                )

        return ProcessStatusResult(state=ProcessState.EXITED, exit_code=exit_code.value)

    def get_process_exit_code(self, h_process: int) -> Optional[int]:
        res = self.query_process_status(h_process)
        if res.state == ProcessState.EXITED:
            return res.exit_code
        elif res.state == ProcessState.RUNNING:
            return None
        raise Win32APIError(f"Process query failed: {res.error}")

    def terminate_process(self, h_process: int, exit_code: int = 1) -> None:
        if not h_process:
            return
        res = self._kernel32.TerminateProcess(h_process, exit_code)
        if not res:
            err = ctypes.get_last_error()
            # If the process has already terminated, TerminateProcess returns FALSE with ERROR_ACCESS_DENIED (5)
            exit_code_dw = DWORD(0)
            if self._kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code_dw)):
                if exit_code_dw.value != STILL_ACTIVE:
                    return
            raise Win32APIError(f"TerminateProcess failed for handle {h_process} with error code {err}")

    def close_handle(self, handle: int) -> None:
        if handle:
            res = self._kernel32.CloseHandle(handle)
            if not res:
                err = ctypes.get_last_error()
                raise Win32APIError(f"CloseHandle failed for handle {handle} with error code {err}")

    def query_vc_runtime_version(self) -> Optional[Tuple[int, int, int]]:
        """
        Reads the registration Microsoft documents for the VC++ 2015-2022 x64 runtime
        (HKLM\\SOFTWARE[\\WOW6432Node]\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64).
        Returns the highest registered (major, minor, build) with Installed=1, or None.
        """
        import winreg

        found: List[Tuple[int, int, int]] = []
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, VC_RUNTIME_REG_KEY, 0,
                                    winreg.KEY_READ | view) as key:
                    installed = winreg.QueryValueEx(key, "Installed")[0]
                    major = winreg.QueryValueEx(key, "Major")[0]
                    minor = winreg.QueryValueEx(key, "Minor")[0]
                    build = winreg.QueryValueEx(key, "Bld")[0]
            except OSError:
                continue
            if installed == 1 and all(type(v) is int for v in (major, minor, build)):
                found.append((major, minor, build))
        return max(found) if found else None


# ---------------------------------------------------------------------------
# Process Handle Wrapper
# ---------------------------------------------------------------------------

class ProcessHandle:
    """Encapsulates an open Win32 process handle, tracking liveness and exit code."""

    def __init__(
        self,
        name: str,
        h_process: int,
        pid: int,
        adapter: IWin32Adapter,
        h_stdin_write: Optional[int] = None,
    ) -> None:
        self.name = name
        self.h_process = h_process
        self.pid = pid
        self._adapter = adapter
        self.h_stdin_write = h_stdin_write
        self._is_closed = False

    def close_stdin(self) -> None:
        """Closes the write end of the child stdin pipe, delivering EOF."""
        if self.h_stdin_write:
            h = self.h_stdin_write
            self.h_stdin_write = None
            self._adapter.close_handle(h)

    def poll_status(self) -> ProcessStatusResult:
        if self._is_closed or not self.h_process:
            return ProcessStatusResult(ProcessState.EXITED, exit_code=-1)
        return self._adapter.query_process_status(self.h_process)

    def is_alive(self) -> bool:
        res = self.poll_status()
        if res.state == ProcessState.QUERY_FAILED:
            raise Win32APIError(f"Failed to query status for '{self.name}': {res.error}")
        return res.state == ProcessState.RUNNING

    def poll(self) -> Optional[int]:
        res = self.poll_status()
        if res.state == ProcessState.QUERY_FAILED:
            raise Win32APIError(f"Failed to query status for '{self.name}': {res.error}")
        return res.exit_code if res.state == ProcessState.EXITED else None

    def wait(self, timeout_s: float = 0.0) -> int:
        if self._is_closed or not self.h_process:
            return WAIT_OBJECT_0
        timeout_ms = int(timeout_s * 1000) if timeout_s >= 0 else INFINITE
        return self._adapter.wait_for_process(self.h_process, timeout_ms)

    def terminate(self, exit_code: int = 1) -> None:
        if not self._is_closed and self.h_process:
            self._adapter.terminate_process(self.h_process, exit_code)

    def close(self) -> None:
        """
        Closes the native process handle and any remaining stdin pipe handle.
        Preserves handle ownership and unclosed state if CloseHandle fails (F-09).
        """
        if self.h_stdin_write:
            try:
                self.close_stdin()
            except Exception:
                pass
        if not self._is_closed and self.h_process:
            self._adapter.close_handle(self.h_process)
            self.h_process = 0
            self._is_closed = True


# ---------------------------------------------------------------------------
# Unified Application Launcher
# ---------------------------------------------------------------------------

class UnifiedLauncher:
    """
    Coordinates Data Intelligence Lab local execution services on dynamic loopback ports.
    Assigns both backend and frontend processes to an anonymous Win32 Job Object
    configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE at creation time.
    """

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        adapter: Optional[IWin32Adapter] = None,
        backend_timeout_s: float = 15.0,
        frontend_timeout_s: float = 25.0,
        readiness_timeout_s: float = 10.0,
    ) -> None:
        self.repo_root = (repo_root or Path(__file__).resolve().parent).resolve()
        self.adapter = adapter or Win32Adapter()
        self.backend_timeout_s = backend_timeout_s
        self.frontend_timeout_s = frontend_timeout_s
        self.readiness_timeout_s = readiness_timeout_s

        self.h_job: Optional[int] = None
        self.backend_proc: Optional[ProcessHandle] = None
        self.frontend_proc: Optional[ProcessHandle] = None

        self.backend_port: Optional[int] = None
        self.frontend_port: Optional[int] = None

        nonce = uuid.uuid4().hex[:8]
        # tempfile.gettempdir() honours TMPDIR/TEMP/TMP and, when none is set, falls back
        # to a directory that exists and is writable instead of a hardcoded C:\Temp.
        temp_dir = Path(tempfile.gettempdir())
        self.backend_port_file = temp_dir / f"dil_backend_{os.getpid()}_{nonce}.port"
        self.frontend_port_file = temp_dir / f"dil_frontend_{os.getpid()}_{nonce}.port"

        self._shutdown_requested = False
        self.last_shutdown_errors: List[str] = []

    @property
    def outstanding_cleanup_obligations(self) -> List[Union[ProcessCleanupObligation, ThreadCleanupObligation]]:
        """Returns any unfinalized cleanup obligations from creation-time failures."""
        return getattr(self.adapter, "outstanding_cleanup_obligations", [])

    def validate_platform(self) -> None:
        """
        Validates Windows version, build, and 64-bit architecture (Phase 1F.3.4.5, 1F.3.4.6).
        Enforces:
          - Windows 10 client: minimum build 15063 (Version 1703 Creators Update)
          - Windows Server 2016: minimum build 14393
        Rejects unrecognized or invalid product types fail-closed.
        Distinguishes client from server using OSVERSIONINFOEXW.wProductType.
        """
        if not self.adapter.is_windows():
            raise PlatformCompatibilityError(
                "Data Intelligence Lab unified launcher requires Windows."
            )

        try:
            ver = self.adapter.check_version()
        except Exception as exc:
            raise PlatformCompatibilityError(
                f"Failed to query Windows operating system version and product type: {exc}"
            ) from exc

        if not ver.is_valid_product_type:
            raise PlatformCompatibilityError(
                f"Unsupported or unrecognized Windows product type: {ver.product_type}. "
                f"Expected one of VER_NT_WORKSTATION ({VER_NT_WORKSTATION}), "
                f"VER_NT_DOMAIN_CONTROLLER ({VER_NT_DOMAIN_CONTROLLER}), or "
                f"VER_NT_SERVER ({VER_NT_SERVER})."
            )

        if ver.major < MINIMUM_WINDOWS_MAJOR:
            product_desc = "Windows Server" if ver.is_server else "Windows"
            raise PlatformCompatibilityError(
                f"Data Intelligence Lab requires Windows 10 (build {MINIMUM_CLIENT_BUILD}+) or "
                f"Windows Server 2016 (build {MINIMUM_SERVER_BUILD}+) "
                f"(detected {product_desc} major version {ver.major}, build {ver.build})."
            )

        if ver.is_server:
            if ver.build < MINIMUM_SERVER_BUILD:
                raise PlatformCompatibilityError(
                    f"Data Intelligence Lab requires Windows Server 2016 (build {MINIMUM_SERVER_BUILD}) or later "
                    f"(detected Windows Server build {ver.build})."
                )
        else:
            if ver.build < MINIMUM_CLIENT_BUILD:
                raise PlatformCompatibilityError(
                    f"Data Intelligence Lab requires Windows 10 Version 1703 (build {MINIMUM_CLIENT_BUILD}) or later "
                    f"(detected Windows client build {ver.build}). "
                    "PROC_THREAD_ATTRIBUTE_JOB_LIST is not supported on Windows 10 builds prior to 15063."
                )

        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise PlatformCompatibilityError(
                "Data Intelligence Lab launcher requires a 64-bit Python runtime."
            )

    def validate_vc_runtime(self) -> Tuple[int, int, int]:
        """
        Requires the Microsoft Visual C++ 2015-2022 Redistributable (x64) 14.44 or later.
        Fails closed with an actionable message; never downloads or installs it.
        """
        try:
            version = self.adapter.query_vc_runtime_version()
        except Exception as exc:
            raise PlatformCompatibilityError(
                f"Could not determine the installed Microsoft Visual C++ Redistributable (x64): {exc}"
            ) from exc
        required = ".".join(str(v) for v in MINIMUM_VC_RUNTIME)
        if version is None:
            raise PlatformCompatibilityError(
                f"Data Intelligence Lab requires the Microsoft Visual C++ 2015-2022 Redistributable (x64), "
                f"version {required} or later, which is not installed. "
                f"Install it from Microsoft ({VC_RUNTIME_DOWNLOAD_URL}) and start Data Intelligence Lab again."
            )
        if tuple(version[:2]) < MINIMUM_VC_RUNTIME:
            found = ".".join(str(v) for v in version)
            raise PlatformCompatibilityError(
                f"Data Intelligence Lab requires the Microsoft Visual C++ 2015-2022 Redistributable (x64), "
                f"version {required} or later (installed: {found}). "
                f"Update it from Microsoft ({VC_RUNTIME_DOWNLOAD_URL}) and start Data Intelligence Lab again."
            )
        return version

    def validate_paths(self) -> Tuple[Path, Path]:
        """Validates application paths and runtime entry points."""
        backend_entry = self.repo_root / "backend" / "main.py"
        frontend_entry = self.repo_root / "frontend" / "scripts" / "server.mjs"

        if not backend_entry.is_file():
            raise LauncherError(f"Backend entry point missing: {backend_entry}")
        if not frontend_entry.is_file():
            raise LauncherError(f"Frontend entry point missing: {frontend_entry}")

        # An installed copy (or a staged bundle) ships its own Node.js and a manifest.json.
        # There, a missing node.exe means a damaged installation: never fall back to whatever
        # node.exe is first on PATH. Only a developer checkout (no manifest) may use PATH.
        bundled_node = self.repo_root / "node" / ("node.exe" if self.adapter.is_windows() else "node")
        if (self.repo_root / "manifest.json").is_file() and not bundled_node.is_file():
            raise LauncherError(
                f"The bundled Node.js runtime is missing ({bundled_node}). "
                "The installation is damaged; reinstall Data Intelligence Lab."
            )

        return backend_entry, frontend_entry

    def init_job_object(self) -> int:
        """Initializes and configures the anonymous Win32 Job Object."""
        self.h_job = self.adapter.create_job_object()
        try:
            self.adapter.set_job_kill_on_close(self.h_job)
        except Exception:
            try:
                self.adapter.close_handle(self.h_job)
                self.h_job = None
            except Exception as close_exc:
                logger.warning(
                    f"Error closing Job handle after configuration failure (handle {self.h_job} retained): {close_exc}"
                )
            raise
        return self.h_job

    def _cleanup_port_files(self) -> None:
        """Safely unlinks only the invocation's own allocated port files."""
        for p in (self.backend_port_file, self.frontend_port_file):
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass

    def read_port_file(
        self,
        port_file_path: Path,
        process: ProcessHandle,
        timeout_s: float,
        poll_interval_s: float = 0.1,
    ) -> int:
        """
        Polls for the atomic creation of a port announcement file.
        Verifies process liveness on each iteration.
        Validates the port as a strict integer within [1, 65535].
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            # Check for early child process termination or query failure (F-04)
            status_res = process.poll_status()
            if status_res.state == ProcessState.EXITED:
                raise ProcessEarlyExitError(
                    f"Service '{process.name}' exited prematurely with code {status_res.exit_code} before port announcement."
                )
            elif status_res.state == ProcessState.QUERY_FAILED:
                raise ProcessQueryError(
                    f"Failed to query status for service '{process.name}': {status_res.error}"
                )

            if port_file_path.is_file():
                try:
                    content = port_file_path.read_text(encoding="utf-8").strip()
                    if content:
                        if not re.match(r"^\d+$", content):
                            raise PortFileError(f"Port file contains non-numeric data: {content!r}")
                        port = int(content)
                        if port < 1 or port > 65535:
                            raise PortFileError(f"Announced port out of range: {port}")
                        return port
                except (OSError, UnicodeDecodeError):
                    pass  # File write/rename race; retry until deadline

            time.sleep(poll_interval_s)

        raise PortFileError(
            f"Timed out after {timeout_s}s waiting for port announcement from '{process.name}'."
        )

    def probe_http(
        self,
        url: str,
        expected_status: int = 200,
        timeout_s: float = 10.0,
        process: Optional[ProcessHandle] = None,
        poll_interval_s: float = 0.2,
    ) -> None:
        """
        Polls an HTTP endpoint until it returns the expected status code.
        Checks process liveness on each iteration.
        """
        deadline = time.monotonic() + timeout_s
        headers = {"Host": urllib.parse.urlparse(url).netloc}

        while time.monotonic() < deadline:
            if process is not None:
                status_res = process.poll_status()
                if status_res.state == ProcessState.EXITED:
                    raise ProcessEarlyExitError(
                        f"Service '{process.name}' died during HTTP readiness probe with exit code {status_res.exit_code}."
                    )
                elif status_res.state == ProcessState.QUERY_FAILED:
                    raise ProcessQueryError(
                        f"Failed to query status for service '{process.name}' during probe: {status_res.error}"
                    )

            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == expected_status:
                        return
                    raise ServiceReadinessError(
                        f"Unexpected HTTP status {resp.status} from {url} (expected {expected_status})."
                    )
            except urllib.error.HTTPError as exc:
                # An HTTPError is also the response: close it on every path, or its connection
                # (or, on CPython 3.14, a temporary file) stays open until garbage collection.
                with exc:
                    if exc.code == expected_status:
                        return
                    if exc.code == 503:
                        raise ServiceReadinessError(
                            f"Service at {url} reported storage not ready (HTTP 503)."
                        ) from exc
            except (urllib.error.URLError, OSError):
                pass  # Connection refused or server still initializing; retry

            time.sleep(poll_interval_s)

        raise ServiceReadinessError(f"Readiness probe timed out after {timeout_s}s for {url}.")

    def start_backend(self) -> int:
        """Spawns the backend inside the Job Object and verifies readiness."""
        if not self.h_job:
            raise LauncherError("Job Object not initialized.")

        # Ensure target port file does not exist before launch
        if self.backend_port_file.is_file():
            self.backend_port_file.unlink()

        cmd = (
            # -B: the install directory is read-only by contract; the bundle ships
            # precompiled bytecode, so nothing is written next to the sources.
            f'"{sys.executable}" -B -m backend.main '
            f'--host 127.0.0.1 --port 0 --port-file "{self.backend_port_file}"'
        )

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = str(self.repo_root)

        h_proc, pid, _ = self.adapter.create_process_in_job(
            self.h_job, cmd, cwd=self.repo_root, env=env, redirect_stdin=False
        )
        self.backend_proc = ProcessHandle("backend", h_proc, pid, self.adapter)

        # Poll port announcement
        self.backend_port = self.read_port_file(
            self.backend_port_file, self.backend_proc, self.backend_timeout_s
        )

        # Stage 4: Backend liveness & storage readiness probes
        self.probe_http(
            f"http://127.0.0.1:{self.backend_port}/health",
            expected_status=200,
            timeout_s=self.readiness_timeout_s,
            process=self.backend_proc,
        )
        self.probe_http(
            f"http://127.0.0.1:{self.backend_port}/ready",
            expected_status=200,
            timeout_s=self.readiness_timeout_s,
            process=self.backend_proc,
        )

        return self.backend_port

    def start_frontend(self) -> int:
        """Spawns the frontend inside the Job Object and verifies readiness."""
        if not self.h_job:
            raise LauncherError("Job Object not initialized.")
        if self.backend_port is None:
            raise LauncherError("Cannot start frontend before backend is healthy and ready.")

        # Ensure target port file does not exist before launch
        if self.frontend_port_file.is_file():
            self.frontend_port_file.unlink()

        # Check for bundled Node.js executable first
        bundled_node = self.repo_root / "node" / ("node.exe" if self.adapter.is_windows() else "node")
        if bundled_node.is_file():
            node_cmd = f'"{bundled_node}"'
        else:
            node_cmd = "node.exe" if self.adapter.is_windows() else "node"

        frontend_dir = self.repo_root / "frontend"
        cmd = (
            f'{node_cmd} scripts/server.mjs '
            f'--port 0 --port-file "{self.frontend_port_file}"'
        )

        env = os.environ.copy()
        env["DIL_BACKEND_ORIGIN"] = f"http://127.0.0.1:{self.backend_port}"
        env["NEXT_TELEMETRY_DISABLED"] = "1"

        h_proc, pid, h_stdin_write = self.adapter.create_process_in_job(
            self.h_job, cmd, cwd=frontend_dir, env=env, redirect_stdin=True
        )
        self.frontend_proc = ProcessHandle("frontend", h_proc, pid, self.adapter, h_stdin_write=h_stdin_write)

        # Poll port announcement
        self.frontend_port = self.read_port_file(
            self.frontend_port_file, self.frontend_proc, self.frontend_timeout_s
        )

        # Stage 6: Frontend readiness
        # 1. Frontend-local listener test: probe GET / (served directly by Next.js)
        self.probe_http(
            f"http://127.0.0.1:{self.frontend_port}/",
            expected_status=200,
            timeout_s=self.readiness_timeout_s,
            process=self.frontend_proc,
        )
        # 2. Frontend proxy path test: probe GET /health (proxied to backend)
        self.probe_http(
            f"http://127.0.0.1:{self.frontend_port}/health",
            expected_status=200,
            timeout_s=self.readiness_timeout_s,
            process=self.frontend_proc,
        )

        return self.frontend_port

    def launch_browser(self) -> None:
        """Opens default browser to the verified frontend origin. Fails resiliently."""
        if self.frontend_port is None:
            return
        url = f"http://127.0.0.1:{self.frontend_port}"
        try:
            opened = webbrowser.open(url)
            if not opened:
                print(f"[WARN] Could not automatically open default browser. Please navigate to: {url}")
        except Exception as exc:
            print(f"[WARN] Could not automatically open browser ({exc}). Please navigate to: {url}")

    def startup(self) -> Tuple[int, int]:
        """
        Executes deterministic 8-step startup sequence.
        Cleans up and fails closed if any step fails.
        Preserves the primary exception without being masked by secondary cleanup errors.
        """
        try:
            self.validate_platform()
            self.validate_vc_runtime()
            self.validate_paths()
            self.init_job_object()
            b_port = self.start_backend()
            f_port = self.start_frontend()
            self.launch_browser()
            return b_port, f_port
        except Exception as primary_exc:
            try:
                self.shutdown()
            except Exception as cleanup_exc:
                logger.error(f"Secondary error during shutdown cleanup: {cleanup_exc}")
            raise primary_exc

    def supervise_once(self) -> Optional[str]:
        """
        Single supervision tick. Checks child process liveness.
        Returns error description if either process has died or status query fails, or None if healthy.
        """
        for proc in (self.backend_proc, self.frontend_proc):
            if proc is not None:
                res = proc.poll_status()
                if res.state == ProcessState.EXITED:
                    return f"{proc.name.capitalize()} process (PID {proc.pid}) exited unexpectedly with code {res.exit_code}"
                elif res.state == ProcessState.QUERY_FAILED:
                    return f"{proc.name.capitalize()} process (PID {proc.pid}) status query failed: {res.error}"

        return None

    def run_supervision_loop(self, poll_interval_s: float = 0.5) -> int:
        """Runs the main supervision loop until shutdown or unexpected child exit."""
        def sig_handler(signum: int, frame: Any) -> None:
            self._shutdown_requested = True

        signal.signal(signal.SIGINT, sig_handler)
        signal.signal(signal.SIGTERM, sig_handler)

        try:
            while not self._shutdown_requested:
                err = self.supervise_once()
                if err:
                    # Ctrl+C reaches every process in the console at once: a child that exits
                    # because the user asked to stop is not a failure.
                    time.sleep(min(poll_interval_s, 0.2))
                    if self._shutdown_requested:
                        break
                    print(f"[ERROR] {err}")
                    return 1
                time.sleep(poll_interval_s)
            return 0
        finally:
            self.shutdown()

    def shutdown(self, timeout_s: float = 5.0) -> List[str]:
        """
        Executes controlled shutdown:
        1. Closes stdin pipe to frontend (delivering EOF to trigger graceful server.close -> closeAllConnections -> app.close).
        2. Bounded wait for graceful exit.
        3. For any process still running after timeout (or backend), issues TerminateProcess.
        4. Bounded wait for termination confirmation.
        5. Closes process handles for confirmed terminated processes.
        6. Cleans port announcement files.
        7. Closes Job Object handle (kernel kill-on-close backstop).
        8. Retries outstanding cleanup obligations.
        """
        cleanup_errors: List[str] = []
        procs = [p for p in (self.frontend_proc, self.backend_proc) if p is not None]

        # Step 1: Attempt graceful shutdown via stdin EOF where supported (e.g. frontend)
        for p in procs:
            if p.h_stdin_write:
                try:
                    p.close_stdin()
                except Exception as exc:
                    cleanup_errors.append(f"Error closing stdin for {p.name}: {exc}")

        # Step 2: Bounded wait for graceful exit
        graceful_deadline = time.monotonic() + min(timeout_s, 5.0)
        still_running: List[ProcessHandle] = []
        for p in procs:
            try:
                remaining = max(0.0, graceful_deadline - time.monotonic())
                wait_res = p.wait(timeout_s=remaining)
                if wait_res != WAIT_OBJECT_0:
                    still_running.append(p)
            except Exception as exc:
                cleanup_errors.append(f"Wait error during graceful phase for {p.name}: {exc}")
                still_running.append(p)

        # Step 3: For processes that did not exit gracefully, request forced termination
        for p in still_running:
            try:
                status = p.poll_status()
                if status.state == ProcessState.RUNNING:
                    p.terminate(exit_code=1)
                elif status.state == ProcessState.QUERY_FAILED:
                    cleanup_errors.append(
                        f"Query status error before termination for {p.name}: {status.error}"
                    )
                    p.terminate(exit_code=1)
                elif status.state == ProcessState.EXITED:
                    pass
            except Exception as exc:
                cleanup_errors.append(f"Terminate error for {p.name}: {exc}")

        # Step 4: Bounded wait for termination confirmation
        terminated_procs: List[ProcessHandle] = []
        term_deadline = time.monotonic() + 2.0
        for p in procs:
            try:
                remaining = max(0.0, term_deadline - time.monotonic())
                wait_res = p.wait(timeout_s=remaining)
                if wait_res == WAIT_OBJECT_0:
                    terminated_procs.append(p)
                elif wait_res == WAIT_TIMEOUT:
                    cleanup_errors.append(
                        f"Wait timeout for {p.name}: process did not exit within timeout"
                    )
                else:
                    cleanup_errors.append(f"Unexpected wait result for {p.name}: {wait_res}")
            except Exception as exc:
                cleanup_errors.append(f"Wait error for {p.name}: {exc}")

        # Step 5: Close process handles for confirmed terminated processes
        for p in procs:
            if p in terminated_procs:
                try:
                    p.close()
                except Exception as exc:
                    cleanup_errors.append(f"Close handle error for {p.name}: {exc}")
            else:
                cleanup_errors.append(
                    f"Process handle for {p.name} retained because exit was not confirmed"
                )

        # Discard process references ONLY if the handle was successfully closed
        if self.backend_proc is not None and self.backend_proc._is_closed:
            self.backend_proc = None
        if self.frontend_proc is not None and self.frontend_proc._is_closed:
            self.frontend_proc = None

        # Step 4: Clean port announcement files
        try:
            self._cleanup_port_files()
        except Exception as exc:
            cleanup_errors.append(f"Port file cleanup error: {exc}")

        # Step 5: Close Job Object handle so Windows kernel applies last-handle kill
        if self.h_job:
            try:
                self.adapter.close_handle(self.h_job)
                self.h_job = None
            except Exception as exc:
                err_msg = f"Job handle closure error for handle {self.h_job}: {exc}"
                cleanup_errors.append(err_msg)
                logger.error(err_msg)

        # Step 6: Retry any outstanding cleanup obligations from aborted creations (Phase 1F.3.4.5, 1F.3.4.6)
        remaining_obligations: List[Union[ProcessCleanupObligation, ThreadCleanupObligation]] = []
        for ob in self.outstanding_cleanup_obligations:
            if not ob.is_complete:
                try:
                    success = ob.retry_cleanup(timeout_s=1.0)
                    if not success:
                        if isinstance(ob, ProcessCleanupObligation):
                            cleanup_errors.append(
                                f"Outstanding cleanup obligation for PID {ob.pid} (handle {ob.h_process}) could not be completed"
                            )
                        else:
                            cleanup_errors.append(
                                f"Outstanding thread cleanup obligation (handle {ob.h_thread}) could not be completed"
                            )
                        remaining_obligations.append(ob)
                except Exception as exc:
                    cleanup_errors.append(f"Error retrying cleanup obligation: {exc}")
                    remaining_obligations.append(ob)
        self.adapter.outstanding_cleanup_obligations = remaining_obligations

        self.last_shutdown_errors = list(cleanup_errors)
        if cleanup_errors:
            logger.warning(
                f"Encountered {len(cleanup_errors)} cleanup issue(s) during shutdown: "
                f"{'; '.join(cleanup_errors)}"
            )

        return cleanup_errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Data Intelligence Lab - Unified Launcher")
    parser.add_argument("--repo-root", default=None, help="Root path of the application repository")
    args = parser.parse_args()

    repo_root = Path(args.repo_root) if args.repo_root else None
    launcher = UnifiedLauncher(repo_root=repo_root)

    try:
        b_port, f_port = launcher.startup()
        print(f"Data Intelligence Lab running:")
        print(f"  Backend:  http://127.0.0.1:{b_port}")
        print(f"  Frontend: http://127.0.0.1:{f_port}")
        print("Close this window or press Ctrl+C to stop Data Intelligence Lab.", flush=True)
        code = launcher.run_supervision_loop()
        if code == 0:
            print("Data Intelligence Lab stopped.", flush=True)
        return code
    except Exception as exc:
        print(f"[FATAL] Launcher failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
