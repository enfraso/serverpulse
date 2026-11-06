#!/usr/bin/env python3
"""ServerPulse: lightweight Linux metrics dashboard. Python standard library only."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
HOST = os.environ.get("SERVERPULSE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SERVERPULSE_PORT", "8765"))
INTERFACE = os.environ.get("SERVERPULSE_INTERFACE", "").strip()
REFRESH_HINT = max(1, int(os.environ.get("SERVERPULSE_REFRESH", "1")))

_prev_cpu = None
_prev_proc = {}
_prev_net = None
_prev_time = None


def read_text(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="replace")
    except (OSError, PermissionError):
        return default


def cpu_times():
    line = read_text("/proc/stat").splitlines()[0].split()
    values = [int(x) for x in line[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def cpu_usage():
    global _prev_cpu
    current = cpu_times()
    usage = 0.0
    if _prev_cpu:
        total_delta = current[0] - _prev_cpu[0]
        idle_delta = current[1] - _prev_cpu[1]
        if total_delta > 0:
            usage = max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))
    _prev_cpu = current
    return round(usage, 1)


def memory_info():
    values = {}
    for line in read_text("/proc/meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                values[parts[0].rstrip(":")] = int(parts[1]) * 1024
            except ValueError:
                pass
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return {
        "total": total, "used": used, "available": available,
        "percent": round(used / total * 100, 1) if total else 0,
        "swap_total": swap_total, "swap_used": max(0, swap_total - swap_free),
        "swap_percent": round((swap_total - swap_free) / swap_total * 100, 1) if swap_total else 0,
    }


def disk_info():
    result = []
    seen = set()
    try:
        lines = read_text("/proc/mounts").splitlines()
        for line in lines:
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mount, fstype = parts[:3]
            if mount in seen or fstype in {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs",
                                            "cgroup", "cgroup2", "overlay", "squashfs",
                                            "securityfs", "pstore", "debugfs", "tracefs",
                                            "configfs", "mqueue", "hugetlbfs", "rpc_pipefs"}:
                continue
            if not mount.startswith("/") or not device.startswith("/dev/"):
                continue
            seen.add(mount)
            try:
                usage = shutil.disk_usage(mount)
            except OSError:
                continue
            result.append({
                "device": device, "mount": mount, "fstype": fstype,
                "total": usage.total, "used": usage.used, "free": usage.free,
                "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
            })
    except OSError:
        pass
    result.sort(key=lambda x: (x["mount"] != "/", x["mount"]))
    return result[:12]


def network_info():
    global _prev_net, _prev_time
    counters = {}
    for line in read_text("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        name, data = line.split(":", 1)
        name = name.strip()
        if name == "lo" or (INTERFACE and name != INTERFACE):
            continue
        fields = data.split()
        if len(fields) >= 9:
            counters[name] = {"rx": int(fields[0]), "tx": int(fields[8])}
    now = time.monotonic()
    rx_rate = tx_rate = 0
    if _prev_net is not None and _prev_time is not None:
        elapsed = max(0.001, now - _prev_time)
        rx_rate = sum(max(0, v["rx"] - _prev_net.get(k, {}).get("rx", v["rx"])) for k, v in counters.items()) / elapsed
        tx_rate = sum(max(0, v["tx"] - _prev_net.get(k, {}).get("tx", v["tx"])) for k, v in counters.items()) / elapsed
    _prev_net, _prev_time = counters, now
    return {
        "interfaces": [{"name": k, **v} for k, v in counters.items()],
        "rx_bytes_sec": round(rx_rate), "tx_bytes_sec": round(tx_rate),
        "rx_total": sum(v["rx"] for v in counters.values()),
        "tx_total": sum(v["tx"] for v in counters.values()),
    }


def temperatures():
    found = []
    for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            raw = int(path.read_text().strip())
            celsius = raw / 1000 if raw > 1000 else float(raw)
            if -20 < celsius < 150:
                zone = path.parent / "type"
                label = zone.read_text().strip() if zone.exists() else path.parent.name
                found.append({"label": label, "celsius": round(celsius, 1)})
        except (OSError, ValueError):
            continue
    return found


def process_snapshot():
    """Return top processes by CPU using /proc; CPU percentages are interval deltas."""
    global _prev_proc
    current = {}
    rows = []
    try:
        clk = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, KeyError):
        clk, page_size = 100, 4096

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            close = stat.rfind(")")
            fields = stat[close + 2:].split()
            # fields[11], [12] are utime/stime; [19] is starttime; [21] is RSS.
            pid = int(entry.name)
            ticks = int(fields[11]) + int(fields[12])
            start = int(fields[19])
            rss = max(0, int(fields[21])) * page_size
            comm = stat[stat.find("(") + 1:close]
            status = (entry / "status").read_text(errors="replace")
            uid_match = re.search(r"^Uid:\s+(\d+)", status, re.M)
            uid = int(uid_match.group(1)) if uid_match else -1
            current[pid] = (ticks, start)
            previous = _prev_proc.get(pid)
            cpu_pct = 0.0
            if previous and previous[1] == start:
                cpu_pct = max(0.0, (ticks - previous[0]) / clk / REFRESH_HINT * 100)
            try:
                import pwd
                user = pwd.getpwuid(uid).pw_name
            except (ImportError, KeyError):
                user = str(uid)
            rows.append({"pid": pid, "name": comm, "user": user,
                         "cpu": round(cpu_pct, 1), "memory": rss})
        except (OSError, ValueError, IndexError, PermissionError):
            continue
    _prev_proc = current
    rows.sort(key=lambda x: (x["cpu"], x["memory"]), reverse=True)
    return rows[:12], len(current)


def system_info():
    uptime_parts = read_text("/proc/uptime", "0 0").split()
    uptime = float(uptime_parts[0]) if uptime_parts else 0
    load = read_text("/proc/loadavg", "0 0 0").split()
    try:
        loadavg = [float(x) for x in load[:3]]
    except ValueError:
        loadavg = [0, 0, 0]
    try:
        kernel = os.uname().release
        architecture = os.uname().machine
    except AttributeError:
        kernel, architecture = "unknown", "unknown"
    return {
        "hostname": socket.gethostname(), "kernel": kernel, "architecture": architecture,
        "platform": "Linux", "uptime_seconds": int(uptime),
        "load_average": loadavg, "cpu_count": os.cpu_count() or 1,
    }


def collect():
    processes, process_count = process_snapshot()
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "system": system_info(),
        "cpu": {"percent": cpu_usage(), "temperatures": temperatures()},
        "memory": memory_info(),
        "disks": disk_info(),
        "network": network_info(),
        "processes": processes,
        "process_count": process_count,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ServerPulse/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json({"ok": True, "service": "serverpulse"})
        elif path == "/api/metrics":
            self.send_json(collect())
        elif path == "/" or path == "/index.html":
            self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        else:
            self.send_json({"error": "not found"}, 404)

    def send_json(self, data, status=200):
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            body = path.read_bytes()
        except OSError:
            return self.send_json({"error": "dashboard file missing"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"ServerPulse listening on http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
