#!/usr/bin/env python3
"""ServerPulse: lightweight Linux metrics dashboard. Python standard library only."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
HOST = os.environ.get("SERVERPULSE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SERVERPULSE_PORT", "8764"))
INTERFACE = os.environ.get("SERVERPULSE_INTERFACE", "").strip()
REFRESH_HINT = max(1, int(os.environ.get("SERVERPULSE_REFRESH", "1")))
DATA_DIR = Path(os.environ.get("SERVERPULSE_DATA_DIR", "/var/lib/serverpulse"))
HISTORY_DB = DATA_DIR / "history.sqlite3"
HISTORY_INTERVAL = max(15, int(os.environ.get("SERVERPULSE_HISTORY_INTERVAL", "60")))
TEMP_WARN_C = float(os.environ.get("SERVERPULSE_TEMP_WARN_C", "80"))
MEMORY_WARN_PERCENT = float(os.environ.get("SERVERPULSE_MEMORY_WARN_PERCENT", "90"))

_prev_cpu = None
_prev_proc = {}
_prev_net = None
_prev_time = None
_monitor_cache = {}
_monitor_lock = threading.Lock()
_history_lock = threading.Lock()
_last_history_at = 0.0
_last_history_cleanup = 0.0
_last_snapshot = {}


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


def _connect_history():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(HISTORY_DB, timeout=5)
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS samples (
            timestamp INTEGER PRIMARY KEY,
            cpu REAL NOT NULL,
            memory REAL NOT NULL,
            memory_used INTEGER NOT NULL,
            rx_rate INTEGER NOT NULL,
            tx_rate INTEGER NOT NULL
        )"""
    )
    return connection


def record_history(cpu, memory, network):
    global _last_history_at, _last_history_cleanup
    now = time.time()
    with _history_lock:
        if now - _last_history_at < HISTORY_INTERVAL:
            return
        with closing(_connect_history()) as connection:
            with connection:
                connection.execute(
                    """INSERT OR REPLACE INTO samples
                       (timestamp, cpu, memory, memory_used, rx_rate, tx_rate)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (int(now), cpu["percent"], memory["percent"], memory["used"],
                     network["rx_bytes_sec"], network["tx_bytes_sec"]),
                )
                if now - _last_history_cleanup >= 3600:
                    connection.execute(
                        "DELETE FROM samples WHERE timestamp < ?", (int(now - 8 * 86400),)
                    )
                    _last_history_cleanup = now
        _last_history_at = now


def history_info(period):
    hours = {"24h": 24, "7d": 24 * 7}.get(period)
    if hours is None:
        raise ValueError("period must be 24h or 7d")
    now = int(time.time())
    cutoff = now - hours * 3600
    bucket_seconds = 300 if period == "24h" else 1800
    with closing(_connect_history()) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT (timestamp / ?) * ? AS bucket,
                      AVG(cpu) AS cpu, MAX(cpu) AS cpu_peak,
                      AVG(memory) AS memory, MAX(memory) AS memory_peak,
                      AVG(rx_rate) AS rx_rate, MAX(rx_rate) AS rx_peak,
                      AVG(tx_rate) AS tx_rate, MAX(tx_rate) AS tx_peak
               FROM samples WHERE timestamp >= ?
               GROUP BY bucket ORDER BY bucket""",
            (bucket_seconds, bucket_seconds, cutoff),
        ).fetchall()
        peak = connection.execute(
            """SELECT MAX(cpu) AS cpu, MAX(memory) AS memory,
                      MAX(rx_rate) AS rx_rate, MAX(tx_rate) AS tx_rate
               FROM samples WHERE timestamp >= ?""",
            (cutoff,),
        ).fetchone()
        peak_memory = connection.execute(
            """SELECT memory_used FROM samples WHERE timestamp >= ?
               ORDER BY memory DESC, timestamp DESC LIMIT 1""",
            (cutoff,),
        ).fetchone()
        peak_times = {}
        for metric in ("cpu", "memory", "rx_rate", "tx_rate"):
            row = connection.execute(
                f"SELECT timestamp FROM samples WHERE timestamp >= ? "
                f"ORDER BY {metric} DESC, timestamp DESC LIMIT 1",
                (cutoff,),
            ).fetchone()
            peak_times[metric] = row["timestamp"] if row else None
    return {
        "period": period,
        "interval_seconds": HISTORY_INTERVAL,
        "points": [
            {
                "timestamp": row["bucket"],
                "cpu": round(row["cpu"], 1),
                "cpu_peak": round(row["cpu_peak"], 1),
                "memory": round(row["memory"], 1),
                "memory_peak": round(row["memory_peak"], 1),
                "rx_rate": round(row["rx_rate"]),
                "rx_peak": row["rx_peak"],
                "tx_rate": round(row["tx_rate"]),
                "tx_peak": row["tx_peak"],
            }
            for row in rows
        ],
        "peaks": {
            "cpu_percent": peak["cpu"] or 0,
            "memory_percent": peak["memory"] or 0,
            "memory_used": peak_memory["memory_used"] if peak_memory else 0,
            "rx_bytes_sec": peak["rx_rate"] or 0,
            "tx_bytes_sec": peak["tx_rate"] or 0,
            "timestamps": peak_times,
        },
    }


def _cached_monitor(name, loader, refresh_seconds=15):
    now = time.monotonic()
    with _monitor_lock:
        cached = _monitor_cache.get(name)
        if cached and now - cached[0] < refresh_seconds:
            return cached[1]
        result = loader()
        _monitor_cache[name] = (now, result)
        return result


def _command_output(args):
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=3, check=False
        )
    except FileNotFoundError:
        return None, f"{args[0]} is not installed"
    except subprocess.TimeoutExpired:
        return None, f"{args[0]} timed out"
    if result.returncode:
        message = result.stderr.strip() or f"{args[0]} exited with status {result.returncode}"
        return None, message[:300]
    return result.stdout, None


def systemd_info():
    def load():
        if not shutil.which("systemctl"):
            return {"available": False, "error": "systemctl is not installed", "services": []}
        output, error = _command_output(
            ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager"]
        )
        if error:
            return {"available": False, "error": error, "services": []}
        services = []
        for line in output.splitlines():
            fields = line.split(None, 4)
            if len(fields) >= 4:
                services.append({
                    "unit": fields[0], "load": fields[1], "active": fields[2],
                    "sub": fields[3], "description": fields[4] if len(fields) > 4 else "",
                })
        return {"available": True, "error": None, "services": services}

    return _cached_monitor("systemd", load)


def docker_info():
    def load():
        if not shutil.which("docker"):
            return {"available": False, "error": "Docker is not installed", "containers": []}
        output, error = _command_output(["docker", "ps", "-a", "--format", "{{json .}}"])
        if error:
            return {"available": False, "error": error, "containers": []}
        containers = []
        try:
            for line in output.splitlines():
                item = json.loads(line)
                if not isinstance(item, dict):
                    return {"available": False, "error": "Docker returned invalid JSON", "containers": []}
                containers.append({
                    "name": item.get("Names", ""),
                    "image": item.get("Image", ""),
                    "state": item.get("State", ""),
                    "status": item.get("Status", ""),
                })
        except json.JSONDecodeError:
            return {"available": False, "error": "Docker returned invalid JSON", "containers": []}
        return {"available": True, "error": None, "containers": containers}

    return _cached_monitor("docker", load)


def parse_tcp_table(text, ipv6=False):
    listeners = []
    established = 0
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            address_hex, port_hex = fields[1].split(":")
            port = int(port_hex, 16)
        except (ValueError, IndexError):
            continue
        if fields[3] == "01":
            established += 1
        if fields[3] != "0A":
            continue
        try:
            raw_address = bytes.fromhex(address_hex)
            if ipv6:
                raw_address = b"".join(raw_address[i:i + 4][::-1] for i in range(0, 16, 4))
                address = socket.inet_ntop(socket.AF_INET6, raw_address)
            else:
                address = socket.inet_ntop(socket.AF_INET, raw_address[::-1])
        except (ValueError, OSError):
            address = address_hex
        listeners.append({"address": address, "port": port, "protocol": "tcp6" if ipv6 else "tcp"})
    return listeners, established


def tcp_info():
    listeners = []
    established = 0
    for path, ipv6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            text = Path(path).read_text(errors="replace")
        except OSError as error:
            return {
                "available": False, "error": str(error),
                "listeners": [], "established": 0,
            }
        found, count = parse_tcp_table(text, ipv6)
        listeners.extend(found)
        established += count
    listeners.sort(key=lambda item: (item["port"], item["protocol"], item["address"]))
    return {"available": True, "error": None, "listeners": listeners, "established": established}


def availability_info():
    def load():
        checks = []
        entries = os.environ.get("SERVERPULSE_CHECKS", "").split(",")[:20]
        for entry in entries:
            if not entry.strip():
                continue
            name, separator, target = entry.strip().partition("=")
            name, target = name.strip(), target.strip()
            host, port_separator, port_text = target.rpartition(":")
            if not separator or not port_separator:
                checks.append({"name": name or entry.strip(), "target": target,
                               "available": False, "error": "expected name=host:port"})
                continue
            host = host.strip("[]")
            try:
                port = int(port_text)
                if not 1 <= port <= 65535:
                    raise ValueError
                with socket.create_connection((host, port), timeout=0.5):
                    pass
                checks.append({"name": name, "target": target, "available": True, "error": None})
            except (OSError, ValueError):
                checks.append({"name": name, "target": target, "available": False,
                               "error": "connection failed or invalid port"})
        return checks

    return _cached_monitor("availability", load)


def build_alerts(cpu, memory, systemd, docker, availability):
    alerts = []
    if memory["percent"] >= MEMORY_WARN_PERCENT:
        alerts.append({
            "severity": "warning", "source": "memory",
            "message": f"Memória em {memory['percent']:.1f}% (limite {MEMORY_WARN_PERCENT:g}%).",
        })
    for temperature in cpu["temperatures"]:
        if temperature["celsius"] >= TEMP_WARN_C:
            alerts.append({
                "severity": "warning", "source": "temperature",
                "message": f"Temperatura alta em {temperature['label']}: "
                           f"{temperature['celsius']:.1f}°C (limite {TEMP_WARN_C:g}°C).",
            })
    if systemd["available"]:
        for service in systemd["services"]:
            if service["active"] == "failed" or service["sub"] == "failed":
                alerts.append({
                    "severity": "critical", "source": "systemd",
                    "message": f"Serviço systemd indisponível: {service['unit']}.",
                })
    if docker["available"]:
        for container in docker["containers"]:
            if container["state"].lower() != "running":
                alerts.append({
                    "severity": "warning", "source": "docker",
                    "message": f"Container parado: {container['name']} ({container['state']}).",
                })
    for check in availability:
        if not check["available"]:
            alerts.append({
                "severity": "critical", "source": "availability",
                "message": f"Alvo indisponível: {check['name']} ({check['target']}).",
            })
    return alerts


def collect():
    processes, process_count = process_snapshot()
    cpu = {"percent": cpu_usage(), "temperatures": temperatures()}
    memory = memory_info()
    network = network_info()
    systemd = systemd_info()
    docker = docker_info()
    availability = availability_info()
    record_history(cpu, memory, network)
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "system": system_info(),
        "cpu": cpu,
        "memory": memory,
        "disks": disk_info(),
        "network": network,
        "processes": processes,
        "process_count": process_count,
        "systemd": systemd,
        "docker": docker,
        "tcp": tcp_info(),
        "availability": availability,
        "alerts": build_alerts(cpu, memory, systemd, docker, availability),
    }
    global _last_snapshot
    _last_snapshot = result
    return result


def search_snapshot(query):
    query = query.strip().lower()[:100]
    if not query:
        return {"query": query, "results": {}}
    snapshot = _last_snapshot or collect()
    groups = {
        "processes": snapshot["processes"],
        "systemd": snapshot["systemd"]["services"],
        "docker": snapshot["docker"]["containers"],
        "tcp": snapshot["tcp"]["listeners"],
        "availability": snapshot["availability"],
    }
    results = {
        name: [
            item for item in items
            if query in " ".join(str(value) for value in item.values()).lower()
        ]
        for name, items in groups.items()
    }
    return {"query": query, "results": results}


class Handler(BaseHTTPRequestHandler):
    server_version = "ServerPulse/2.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def do_GET(self):
        request = urlparse(self.path)
        try:
            if request.path == "/api/health":
                self.send_json({"ok": True, "service": "serverpulse", "version": "2.0"})
            elif request.path == "/api/metrics":
                self.send_json(collect())
            elif request.path == "/api/history":
                period = parse_qs(request.query).get("period", ["24h"])[0]
                if period not in {"24h", "7d"}:
                    self.send_json({"error": "period must be 24h or 7d"}, 400)
                else:
                    self.send_json(history_info(period))
            elif request.path == "/api/search":
                query = parse_qs(request.query).get("q", [""])[0]
                self.send_json(search_snapshot(query))
            elif request.path == "/" or request.path == "/index.html":
                self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            else:
                self.send_json({"error": "not found"}, 404)
        except ValueError as error:
            self.log_error("invalid monitoring data: %s", error)
            self.send_json({"error": "invalid monitoring data"}, 500)
        except (OSError, sqlite3.Error) as error:
            self.log_error("request failed: %s", error)
            self.send_json({"error": "monitoring data unavailable"}, 503)

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
