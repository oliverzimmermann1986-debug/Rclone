"""VPN-Sensor: WireGuard-/Tailscale-Status und TCP-Erreichbarkeit des Backup-Servers."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config_store import get_config

MAX_PROBES = 10
CACHE_TTL_SEC = 30
_cache: tuple[float, tuple[str, ...], dict[str, Any]] | None = None
_cache_lock = threading.Lock()


def _run_cmd(cmd: list[str], run: Any) -> tuple[str | None, str | None]:
    """Liefert (stdout, None) oder (None, deutsche Fehlermeldung)."""
    name = cmd[0]
    try:
        proc = run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        return None, f"{name} ist nicht installiert."
    except subprocess.TimeoutExpired:
        return None, f"{name} antwortet nicht (Zeitüberschreitung)."
    except OSError as exc:
        return None, f"{name} nicht ausführbar: {exc.strerror or exc}"
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        detail = f": {lines[0][:120]}" if lines else ""
        return None, f"{name} fehlgeschlagen (Code {proc.returncode}{detail})."
    return proc.stdout or "", None


def wireguard_status(
    *, run: Any = subprocess.run, now: float | None = None
) -> dict[str, Any]:
    """Parst `wg show all dump`; Private/Preshared Keys werden nie übernommen."""
    ts = time.time() if now is None else now
    out, error = _run_cmd(["wg", "show", "all", "dump"], run)
    if out is None:
        return {"available": False, "interfaces": [], "error": error}
    interfaces: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        f = line.split("\t")
        try:
            if len(f) == 5:  # name, private, public, listen-port, fwmark
                interfaces[f[0]] = {"name": f[0], "listen_port": int(f[3]), "peers": []}
            elif (
                len(f) == 9 and f[0] in interfaces
            ):  # name, public, psk, endpoint, ips, hs, rx, tx, ka
                hs = int(f[5])
                interfaces[f[0]]["peers"].append(
                    {
                        "public_key_short": f[1][:8] + "…",
                        "endpoint": None if f[3] == "(none)" else f[3],
                        "allowed_ips": [] if f[4] == "(none)" else f[4].split(","),
                        "handshake_age_seconds": max(0, int(ts - hs)) if hs else None,
                        "rx_bytes": int(f[6]),
                        "tx_bytes": int(f[7]),
                    }
                )
        except ValueError:
            continue  # defekte Zeile überspringen
    return {"available": True, "interfaces": list(interfaces.values()), "error": None}


def tailscale_status(*, run: Any = subprocess.run) -> dict[str, Any]:
    """Parst `tailscale status --json`."""
    out, error = _run_cmd(["tailscale", "status", "--json"], run)
    data: Any = None
    if out is not None:
        try:
            data = json.loads(out)
        except ValueError:
            pass
        if not isinstance(data, dict):
            out, error = None, "Ausgabe von tailscale nicht lesbar."
    if out is None:
        return {"available": False, "self": None, "peers": [], "error": error}
    me = data.get("Self") or {}
    peers = [
        {
            "hostname": p.get("HostName"),
            "online": bool(p.get("Online")),
            "last_seen": None
            if str(p.get("LastSeen", "")).startswith("0001-")
            else p.get("LastSeen"),
            "rx_bytes": p.get("RxBytes"),
            "tx_bytes": p.get("TxBytes"),
            "os": p.get("OS"),
        }
        for p in (data.get("Peer") or {}).values()
    ]
    return {
        "available": True,
        "self": {
            "hostname": me.get("HostName"),
            "online": bool(me.get("Online")),
            "tailscale_ips": list(me.get("TailscaleIPs") or []),
        },
        "peers": peers,
        "error": None,
    }


def _split_target(target: Any) -> tuple[str, int]:
    """Zerlegt "host:port" bzw. "[v6]:port"; ValueError mit deutscher Meldung bei ungültigem Ziel."""
    if not isinstance(target, str):
        raise ValueError("Ziel muss Text sein.")
    host, sep, port = target.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    elif ":" in host:
        raise ValueError("IPv6-Adresse muss in [] stehen.")
    if not sep or not host:
        raise ValueError('Format "host:port" erwartet.')
    if len(host) > 253 or any(c.isspace() or not c.isprintable() for c in host):
        raise ValueError("Ungültiger Hostname.")
    if not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
        raise ValueError("Port muss zwischen 1 und 65535 liegen.")
    return host, int(port)


def probe_targets(
    targets: list[str], *, timeout: float = 2.0, connect: Any = socket.create_connection
) -> list[dict[str, Any]]:
    """TCP-Connect je Ziel (max. MAX_PROBES, parallel); ungültige Ziele ohne Verbindungsversuch."""

    def probe(target: Any) -> dict[str, Any]:
        res: dict[str, Any] = {
            "target": target,
            "host": None,
            "port": None,
            "reachable": False,
            "latency_ms": None,
            "error": None,
        }
        try:
            res["host"], res["port"] = _split_target(target)
        except ValueError as exc:
            res["error"] = str(exc)
            return res
        started = time.perf_counter()
        try:
            connect((res["host"], res["port"]), timeout=timeout).close()
        except TimeoutError:
            res["error"] = f"Zeitüberschreitung nach {timeout:g} s."
        except socket.gaierror:
            res["error"] = "Name nicht auflösbar."
        except OSError as exc:
            res["error"] = (
                f"Verbindung fehlgeschlagen: {exc.strerror or type(exc).__name__}"
            )
        else:
            res["reachable"] = True
            res["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return res

    targets = list(targets)[:MAX_PROBES]
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        return list(pool.map(probe, targets))


def vpn_snapshot(
    cfg: Any = None, *, now: float | None = None, refresh: bool = False
) -> dict[str, Any]:
    """Gesamtstatus; 30 s gecacht (refresh=True umgeht den Cache)."""
    global _cache
    ts = time.time() if now is None else now
    raw = (cfg or get_config()).get("backup", "vpn_probes", default=[])
    targets = (
        tuple(t for t in raw if isinstance(t, str)) if isinstance(raw, list) else ()
    )
    with _cache_lock:  # serialisiert auch parallele Aufrufe: ein Messlauf statt vieler
        if (
            not refresh
            and _cache
            and _cache[1] == targets
            and 0 <= ts - _cache[0] < CACHE_TTL_SEC
        ):
            return _cache[2]
        snap = {
            "wireguard": wireguard_status(now=ts),
            "tailscale": tailscale_status(),
            "probes": probe_targets(list(targets)),
            "checked_at": int(ts),
        }
        _cache = (ts, targets, snap)
        return snap
