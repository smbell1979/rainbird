"""A fake LNK2 module on localhost, for tests and for developing the page.

Speaks the same encrypted JSON-RPC over HTTPS (self-signed) as the real
module and keeps controller state: one running zone with its own timer, a
rain delay and a rain sensor. Behaviour copies what was measured on Scott's
ESP-TM2: a new start replaces the running zone, manual runs work during a rain
delay, and each run stops by itself when its minutes are up.

sim_cert.pem / sim_key.pem are a throwaway localhost certificate. They protect
nothing and are not secret.
"""

from __future__ import annotations

import json
import random
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pyrainbird import encryption

HERE = Path(__file__).resolve().parent
ZONES = 12


class SimController:
    def __init__(self, password: str, speed: float = 1.0, port: int = 0,
                 no_answer_rate: float = 0.0, seed: int | None = None,
                 drop_writes: bool = True) -> None:
        self.password = password
        self.speed = speed  # plan seconds per real second, matching ScaledClock
        self.no_answer_rate = no_answer_rate
        self.drop_writes = drop_writes  # whether the random drops include start/stop/delay
        self.drop_next_starts = 0  # drop exactly this many zone starts, for tests
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.zone: int | None = None
        self.zone_ends = 0.0
        self.delay = 0
        self.sensor = False
        self.commands: list[tuple] = []  # everything that changed state, for tests
        self.requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(HERE / "sim_cert.pem", HERE / "sim_key.pem")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def start(self) -> "SimController":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- state ---------------------------------------------------------------

    def running(self) -> set[int]:
        with self.lock:
            if self.zone is not None and time.monotonic() >= self.zone_ends:
                self.commands.append(("timer-end", self.zone))
                self.zone = None
            return {self.zone} if self.zone is not None else set()

    def start_from_outside(self, zone: int, minutes: int) -> None:
        """What a start from the app or the panel looks like to the service."""
        with self.lock:
            self.zone = zone
            self.zone_ends = time.monotonic() + minutes * 60 / self.speed
            self.commands.append(("outside-start", zone, minutes))

    # --- protocol ------------------------------------------------------------

    def _sip(self, data: str) -> str:
        code = data[:2]
        if code == "02":
            return "82010A020A"  # ESP-TM2 (010A), protocol 2.10
        if code == "0B":
            return "8B021D0000"  # firmware 2.29.0
        if code == "03":
            return "8300FF0F0000"  # zones 1-12
        if code == "3F":
            mask = bytearray(4)
            for z in self.running():
                mask[(z - 1) // 8] |= 1 << ((z - 1) % 8)
            return "BF00" + mask.hex().upper()
        with self.lock:
            if code == "39":
                zone, minutes = int(data[2:6], 16), int(data[6:8], 16)
                self.zone, self.zone_ends = zone, time.monotonic() + minutes * 60 / self.speed
                self.commands.append(("start", zone, minutes))
                return "0139"
            if code == "40":
                self.zone = None
                self.commands.append(("stop",))
                return "0140"
            if code == "36":
                return f"B6{self.delay:04X}"
            if code == "37":
                self.delay = int(data[2:6], 16)
                self.commands.append(("delay", self.delay))
                return "0137"
            if code == "3E":
                return f"BE{int(self.sensor):02X}"
        return "00" + code + "01"  # NACK anything else (including schedule reads)

    def _handler(self):
        sim = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                sim.requests += 1
                body = self.rfile.read(int(self.headers["Content-Length"]))
                try:
                    plain = encryption.decrypt(body, sim.password).decode("utf-8").rstrip("\x00")
                    request = json.loads(plain)
                except Exception:
                    return self.send_error(403, "Password")
                code = request.get("params", {}).get("data", "")[:2]
                is_write = code in ("39", "40", "37")
                drop = False
                if code == "39" and sim.drop_next_starts > 0:
                    sim.drop_next_starts -= 1
                    drop = True
                elif sim.no_answer_rate and (sim.drop_writes or not is_write):
                    drop = sim.rng.random() < sim.no_answer_rate
                if drop:
                    self.close_connection = True  # lost before it was acted on
                    return
                if request["method"] == "tunnelSip":
                    reply = sim._sip(request["params"]["data"])
                    result = {"jsonrpc": "2.0", "id": request["id"],
                              "result": {"data": reply, "length": len(reply) // 2}}
                else:
                    result = {"jsonrpc": "2.0", "id": request["id"],
                              "error": {"code": -32601, "message": "Method not found"}}
                payload = encryption.encrypt(json.dumps(result), sim.password)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass

        return Handler
