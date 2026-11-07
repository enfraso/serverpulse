import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import serverpulse


class HistoryTests(unittest.TestCase):
    def test_samples_are_persisted_and_peak_values_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch.multiple(
                serverpulse,
                DATA_DIR=data_dir,
                HISTORY_DB=data_dir / "history.sqlite3",
                HISTORY_INTERVAL=15,
                _last_history_at=0.0,
                _last_history_cleanup=0.0,
            ):
                serverpulse.record_history(
                    {"percent": 72.5},
                    {"percent": 81.0, "used": 810},
                    {"rx_bytes_sec": 2048, "tx_bytes_sec": 1024},
                )
                result = serverpulse.history_info("24h")

        self.assertEqual(len(result["points"]), 1)
        self.assertEqual(result["peaks"]["cpu_percent"], 72.5)
        self.assertEqual(result["peaks"]["memory_percent"], 81.0)
        self.assertEqual(result["peaks"]["rx_bytes_sec"], 2048)
        self.assertEqual(result["peaks"]["tx_bytes_sec"], 1024)
        self.assertIsNotNone(result["peaks"]["timestamps"]["cpu"])

    def test_history_rejects_unknown_period(self):
        with self.assertRaises(ValueError):
            serverpulse.history_info("30d")


class TcpTests(unittest.TestCase):
    def test_counts_listening_and_established_sockets(self):
        table = (
            "sl local_address rem_address st\n"
            "0: 0100007F:1F9C 00000000:0000 0A\n"
            "1: 0100007F:C350 0100007F:1F9C 01\n"
        )
        listeners, established = serverpulse.parse_tcp_table(table)
        self.assertEqual(listeners, [
            {"address": "127.0.0.1", "port": 8092, "protocol": "tcp"}
        ])
        self.assertEqual(established, 1)


class AlertTests(unittest.TestCase):
    def test_memory_temperature_and_unavailable_targets_alert(self):
        alerts = serverpulse.build_alerts(
            {"temperatures": [{"label": "CPU", "celsius": 85.0}]},
            {"percent": 95.0},
            {"available": True, "services": []},
            {"available": True, "containers": []},
            [{"name": "web", "target": "localhost:80", "available": False}],
        )
        self.assertEqual({item["source"] for item in alerts},
                         {"temperature", "memory", "availability"})


class SearchTests(unittest.TestCase):
    def test_search_filters_all_monitored_groups(self):
        snapshot = {
            "processes": [{"name": "python", "pid": 42}],
            "systemd": {"services": [{"unit": "sshd.service"}]},
            "docker": {"containers": [{"name": "api"}]},
            "tcp": {"listeners": [{"port": 8764}]},
            "availability": [],
        }
        with patch.object(serverpulse, "_last_snapshot", snapshot):
            results = serverpulse.search_snapshot("sshd")
        self.assertEqual(results["results"]["systemd"], [{"unit": "sshd.service"}])
        self.assertEqual(results["results"]["processes"], [])


class ApiTests(unittest.TestCase):
    def test_history_endpoint_and_invalid_period_status(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), serverpulse.Handler)
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with patch.multiple(
                    serverpulse,
                    DATA_DIR=data_dir,
                    HISTORY_DB=data_dir / "history.sqlite3",
                ):
                    with urlopen(base_url + "/api/history?period=24h") as response:
                        result = json.load(response)
                        self.assertEqual(response.status, 200)
                        self.assertEqual(result["period"], "24h")
                    with self.assertRaises(HTTPError) as error:
                        urlopen(base_url + "/api/history?period=30d")
                    self.assertEqual(error.exception.code, 400)
            finally:
                server.shutdown()
                worker.join()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
