"""Decode native command output without opening console windows in a GUI."""

import ctypes
import locale
import os
import subprocess
import sys


def decode_output(value, *, windows=False, oem_encoding=None):
    if isinstance(value, str):
        return value
    if not value:
        return ""
    if value.startswith((b"\xff\xfe", b"\xfe\xff")):
        return value.decode("utf-16", errors="replace")
    encodings = ["utf-8-sig"]
    if windows:
        if oem_encoding is None and sys.platform == "win32":
            oem_encoding = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        encodings.append(oem_encoding or locale.getpreferredencoding(False))
    for encoding in encodings:
        try:
            return value.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    return value.decode(encodings[-1], errors="replace")


def run_command(command, *, timeout, system=None, stable_locale=False):
    windows = (system or sys.platform).lower() in {"windows", "win32"}
    options = {}
    if windows and sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    if stable_locale:
        options["env"] = dict(os.environ, LC_ALL="C", LANG="C")
    result = subprocess.run(command, capture_output=True, timeout=timeout, check=False, **options)
    result.stdout = decode_output(result.stdout, windows=windows)
    result.stderr = decode_output(result.stderr, windows=windows)
    return result
