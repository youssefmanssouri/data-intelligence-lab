"""
CI helper (Windows): run a command as another local user, with that user's own
profile and environment, and return its exit code.

CreateProcessWithLogonW with LOGON_WITH_PROFILE and a NULL environment makes
Windows build the environment from the target user's profile (USERPROFILE,
APPDATA, LOCALAPPDATA, TEMP), unlike Start-Process -Credential, which passes
the caller's environment. The password is read from a file, never argv.

Usage: python create_process_with_logon.py --user NAME --password-file FILE
           --command "CMDLINE" --cwd DIR [--timeout-minutes 40]
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import sys

LOGON_WITH_PROFILE = 0x00000001
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--timeout-minutes", type=float, default=40)
    args = parser.parse_args()
    password = open(args.password_file, encoding="utf-8").read().strip()

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.CreateProcessWithLogonW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR,
        wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessWithLogonW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    pi = PROCESS_INFORMATION()
    cmd = ctypes.create_unicode_buffer(args.command)
    ok = advapi32.CreateProcessWithLogonW(
        args.user, ".", password, LOGON_WITH_PROFILE, None, cmd,
        CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW, None, args.cwd, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        print(f"CreateProcessWithLogonW failed: Win32 error {ctypes.get_last_error()}", file=sys.stderr)
        return 2
    try:
        res = kernel32.WaitForSingleObject(pi.hProcess, int(args.timeout_minutes * 60_000))
        if res == WAIT_TIMEOUT:
            kernel32.TerminateProcess(pi.hProcess, 1)
            print(f"Timed out after {args.timeout_minutes} minutes", file=sys.stderr)
            return 3
        if res != WAIT_OBJECT_0:
            print(f"WaitForSingleObject returned {res:#x} (error {ctypes.get_last_error()})", file=sys.stderr)
            return 4
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)


if __name__ == "__main__":
    sys.exit(main())
