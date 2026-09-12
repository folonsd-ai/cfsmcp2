"""Network helpers for portable launcher."""

from __future__ import annotations

import os
import socket


def local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return "127.0.0.1"


def is_port_available(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_free_port(host: str, start: int, limit: int = 200) -> int | None:
    for port in range(start, min(start + limit, 65536)):
        if is_port_available(host, port):
            return port
    return None


def is_valid_http_url(url: str) -> bool:
    text = (url or "").strip().casefold()
    return text.startswith("http://") or text.startswith("https://")
