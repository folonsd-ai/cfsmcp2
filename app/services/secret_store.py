"""Windows DPAPI secret storage for dump profile passwords."""
from __future__ import annotations

import sys
from dataclasses import dataclass

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretStoreError(RuntimeError):
    """Raised when DPAPI is unavailable on this platform."""


@dataclass(frozen=True, slots=True)
class SecretDecryptResult:
    ok: bool
    value: str = ""
    error: str = ""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise SecretStoreError(
            "DPAPI secret store is supported only on Windows (current platform: "
            f"{sys.platform})"
        )


def _blob_from_bytes(data: bytes):
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    blob = DATA_BLOB()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char))
    return blob


def _bytes_from_blob(blob) -> bytes:
    import ctypes

    if not blob.cbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def encrypt_secret(plaintext: str) -> bytes:
    """Encrypt *plaintext* for the current Windows user (DPAPI)."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    crypt32 = ctypes.windll.crypt32
    in_blob = _blob_from_bytes(plaintext.encode("utf-8"))
    out_blob = _blob_from_bytes(b"")
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise OSError("CryptProtectData failed")
    try:
        return _bytes_from_blob(out_blob)
    finally:
        if out_blob.pbData:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def decrypt_secret(blob: bytes) -> SecretDecryptResult:
    """Decrypt DPAPI blob; failure is a normal result (other user / machine)."""
    if not blob:
        return SecretDecryptResult(ok=False, error="empty blob")
    _require_windows()
    import ctypes

    crypt32 = ctypes.windll.crypt32
    in_blob = _blob_from_bytes(blob)
    out_blob = _blob_from_bytes(b"")
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        return SecretDecryptResult(ok=False, error="CryptUnprotectData failed")
    try:
        raw = _bytes_from_blob(out_blob)
        return SecretDecryptResult(ok=True, value=raw.decode("utf-8"))
    except UnicodeDecodeError:
        return SecretDecryptResult(ok=False, error="invalid utf-8 in decrypted secret")
    finally:
        if out_blob.pbData:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
