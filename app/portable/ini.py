"""Read cfsmcp2.ini next to the portable exe."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


LOCAL_HOST = "127.0.0.1"
NETWORK_HOST = "0.0.0.0"
DEFAULT_PORT = 8561


@dataclass
class PortableIni:
    host: str = LOCAL_HOST
    port: int = DEFAULT_PORT
    lm_studio_url: str = "http://127.0.0.1:1234"
    minimize_to_tray: bool = True
    tray_hint_shown: bool = False
    first_run_hint_shown: bool = False
    autostart_server: bool = False


def is_network_bind(host: str) -> bool:
    return (host or "").strip() in (NETWORK_HOST, "::")


def loopback_host(host: str) -> str:
    """Host for browser/health when server binds to all interfaces."""
    if is_network_bind(host):
        return LOCAL_HOST
    return (host or LOCAL_HOST).strip() or LOCAL_HOST


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_portable_ini(path: Path) -> PortableIni:
    cfg = PortableIni()
    if not path.is_file():
        return cfg
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if parser.has_section("server"):
        sec = parser["server"]
        if sec.get("host", "").strip():
            cfg.host = sec.get("host", cfg.host).strip()
        if sec.get("port", "").strip():
            try:
                cfg.port = int(sec.get("port", str(cfg.port)).strip())
            except ValueError:
                pass
    if parser.has_section("embeddings"):
        sec = parser["embeddings"]
        if sec.get("lm_studio_url", "").strip():
            cfg.lm_studio_url = sec.get("lm_studio_url", cfg.lm_studio_url).strip()
    if parser.has_section("launcher"):
        sec = parser["launcher"]
        cfg.minimize_to_tray = _parse_bool(sec.get("minimize_to_tray"), cfg.minimize_to_tray)
        cfg.tray_hint_shown = _parse_bool(sec.get("tray_hint_shown"), cfg.tray_hint_shown)
        cfg.first_run_hint_shown = _parse_bool(sec.get("first_run_hint_shown"), cfg.first_run_hint_shown)
        cfg.autostart_server = _parse_bool(sec.get("autostart_server"), cfg.autostart_server)
    return cfg


def save_portable_ini(path: Path, cfg: PortableIni) -> None:
    parser = configparser.ConfigParser()
    parser["server"] = {"host": cfg.host, "port": str(cfg.port)}
    parser["embeddings"] = {"lm_studio_url": cfg.lm_studio_url}
    parser["launcher"] = {
        "minimize_to_tray": "true" if cfg.minimize_to_tray else "false",
        "tray_hint_shown": "true" if cfg.tray_hint_shown else "false",
        "first_run_hint_shown": "true" if cfg.first_run_hint_shown else "false",
        "autostart_server": "true" if cfg.autostart_server else "false",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        parser.write(fh)
