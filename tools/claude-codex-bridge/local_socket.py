"""Minimal RFC 6455 client for Codex's private Unix socket (stdlib only)."""
import base64
import hashlib
import json
import os
import socket
import struct
import threading

from contracts import BridgeError

LIMIT = 16 * 1024 * 1024


class LocalSocket:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.writer = threading.Lock()
        try:
            self.socket.settimeout(10)
            self.socket.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            self.socket.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + key + "\r\n\r\n").encode())
            header = b""
            while not header.endswith(b"\r\n\r\n") and len(header) < 16384:
                header += self.exact(1)
            lines = header.decode("ascii").split("\r\n")
            fields = {k.strip().lower(): v.strip() for line in lines[1:] if ":" in line
                      for k, v in [line.split(":", 1)]}
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if " 101 " not in lines[0] or fields.get("sec-websocket-accept") != expected:
                raise BridgeError("Private Codex socket rejected the WebSocket handshake")
            self.socket.settimeout(None)
        except BaseException:
            self.socket.close()
            raise

    def exact(self, size):
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.socket.recv(size - len(chunks))
            if not chunk:
                raise EOFError("Private Codex socket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def frame(self, opcode, data):
        if len(data) > LIMIT:
            raise BridgeError("Local protocol message exceeds 16 MiB")
        size = len(data)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | size]) if size < 126 else (
            b"\xfe" + struct.pack("!H", size) if size < 65536 else b"\xff" + struct.pack("!Q", size))
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.writer:
            self.socket.sendall(head + mask + masked)

    def send(self, message):
        self.frame(1, json.dumps(message).encode())

    def receive(self):
        message = bytearray()
        started = False
        while True:
            first, second = self.exact(2)
            opcode, final = first & 0xf, bool(first & 0x80)
            if first & 0x70 or second & 0x80:
                raise BridgeError("Unsupported masked or extended server frame")
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self.exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.exact(8))[0]
            if size > LIMIT or len(message) + size > LIMIT:
                raise BridgeError("Local protocol message exceeds 16 MiB")
            data = self.exact(size)
            if opcode >= 8:
                if not final or size > 125:
                    raise BridgeError("Invalid control frame")
                if opcode == 8:
                    raise EOFError("Private Codex socket closed")
                if opcode == 9:
                    self.frame(10, data)
                elif opcode != 10:
                    raise BridgeError("Unsupported control frame")
                continue
            if (not started and opcode != 1) or (started and opcode != 0):
                raise BridgeError("Expected a text message or its continuation")
            started = True
            message.extend(data)
            if final:
                return json.loads(message)

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
