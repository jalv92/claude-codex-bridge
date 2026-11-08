"""Wire framing and Codex turn-output handling, without any CLI or network service."""
import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import queue
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_peer import CodexPeer, Rpc, native_config
from contracts import BridgeError
from local_socket import LIMIT, LocalSocket


class SocketTests(unittest.TestCase):
    def test_handshake_masking_fragmentation_ping_and_rpc(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "socket"
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(path)); server.listen(1)
            errors = []
            def handle():
                try:
                    with server.accept()[0] as conn:
                        f = conn.makefile("rb", buffering=0)
                        header = b""
                        while not header.endswith(b"\r\n\r\n"):
                            header += f.read(1)
                        key = next(line.split(b": ", 1)[1] for line in header.split(b"\r\n")
                                   if line.lower().startswith(b"sec-websocket-key:"))
                        digest = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + digest + b"\r\n\r\n")
                        def exact(size):
                            data = bytearray()
                            while len(data) < size:
                                chunk = f.read(size - len(data))
                                if not chunk:
                                    raise EOFError("Fixture client closed before a complete frame")
                                data.extend(chunk)
                            return bytes(data)
                        def read_frame():
                            first, second = exact(2)
                            self.assertTrue(second & 128)
                            size = second & 127
                            if size == 126: size = struct.unpack("!H", exact(2))[0]
                            if size == 127: size = struct.unpack("!Q", exact(8))[0]
                            mask = exact(4)
                            data = exact(size)
                            return first & 15, bytes(v ^ mask[i % 4] for i, v in enumerate(data))
                        _, raw = read_frame()
                        request = json.loads(raw)
                        self.assertEqual(request["params"]["data"], "x" * 70000)
                        conn.sendall(b'\x89\x01p')
                        opcode, data = read_frame()
                        self.assertEqual((opcode, data), (10, b"p"))
                        answer = json.dumps({"id": request["id"], "result": {"accepted": True}}).encode()
                        conn.sendall(bytes([1, 10]) + answer[:10] + bytes([128, len(answer) - 10]) + answer[10:])
                        # Closing after the acknowledged request leaves further delivery ambiguous.
                        f.close()
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=handle)
            thread.start()
            rpc = Rpc(path, Path(temp) / "protocol.log")
            try:
                self.assertEqual(rpc.call("fixture", {"data": "x" * 70000}), {"accepted": True})
                with self.assertRaises((BridgeError, BrokenPipeError)):
                    rpc.call("after-close", {}, timeout=.1)
            finally:
                rpc.close(); thread.join(timeout=2); server.close()
            self.assertEqual(errors, [])

    def test_reject_invalid_and_oversized_frames(self):
        for frame in (b'\x82\x01x', b'\x81\xff' + struct.pack("!Q", LIMIT + 1),
                      b'\x81\x7f' + struct.pack("!Q", LIMIT + 1)):
            left, right = socket.socketpair()
            client = LocalSocket.__new__(LocalSocket)
            client.socket = left; client.writer = threading.Lock()
            try:
                right.sendall(frame)
                with self.assertRaises(BridgeError): client.receive()
            finally:
                client.close(); right.close()

    def test_turn_outcomes_require_valid_final_text_and_matching_turn(self):
        for mode in ("ok", "missing", "failed", "malformed", "timeout"):
            class FakeRpc:
                events = queue.Queue()
                def call(self, method, params, **kwargs):
                    if method == "turn/start":
                        self.assertions = params
                        self.events.put({"method": "item/completed", "params": {"threadId": "other", "turnId": "turn",
                            "item": {"type": "agentMessage", "text": '{"wrong":true}'}}})
                        if mode != "missing":
                            self.events.put({"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn",
                                "item": {"type": "agentMessage", "phase": "final_answer",
                                         "text": "malformed" if mode == "malformed" else '{"fixture":true}'}}})
                        self.events.put({"method": "turn/completed", "params": {"threadId": "thread",
                            "turn": {"id": "turn", "status": "failed" if mode == "failed" else "completed"}}})
                        return {"turn": {"id": "turn"}}
                    return {}
            peer = CodexPeer.__new__(CodexPeer)
            peer.rpc = FakeRpc(); peer.thread_id = "thread"; peer.ui = None; peer.visible = False
            peer.native = False
            peer.directory = Path("/unused-fixture")
            peer.server_identity = None
            peer.server = type("Server", (), {"pid": -1})()
            with patch("codex_peer.atomic"), patch("codex_peer.descendants", return_value=[]):
                if mode == "ok":
                    value, turn = peer.review("data", Path("/tmp"), time.monotonic() + 1, lambda: False)
                    self.assertEqual((value, turn), ({"fixture": True}, "turn"))
                    self.assertEqual(peer.rpc.assertions["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
                else:
                    with self.assertRaises((BridgeError, ValueError, TimeoutError)):
                        peer.review("data", Path("/tmp"), time.monotonic() + (-1 if mode == "timeout" else 1), lambda: False)

    def test_native_tools_do_not_override_permissions_or_tool_configuration(self):
        self.assertEqual(native_config(), {"features": {"hooks": False}, "notify": []})
        peer = CodexPeer.__new__(CodexPeer)
        peer.native = True
        peer.thread_id = "thread"
        class InspectRpc:
            events = queue.Queue()
            def call(_self, method, params, **kwargs):
                self.assertEqual(method, "turn/start")
                self.assertNotIn("approvalPolicy", params)
                self.assertNotIn("sandboxPolicy", params)
                self.assertEqual(params["cwd"], "/project")
                raise RuntimeError("checked")
        peer.rpc = InspectRpc()
        with self.assertRaisesRegex(RuntimeError, "checked"):
            peer.review("data", Path("/project"), time.monotonic() + 1, lambda: False)

    def test_propose_turn_pins_scratch_only_offline_sandbox(self):
        from codex_peer import PROPOSE_SANDBOX
        peer = CodexPeer.__new__(CodexPeer)
        peer.native = True; peer.propose = True; peer.thread_id = "thread"
        class InspectRpc:
            events = queue.Queue()
            def call(_self, method, params, **kwargs):
                self.assertEqual(params["approvalPolicy"], "never")
                self.assertEqual(params["sandboxPolicy"], PROPOSE_SANDBOX)
                self.assertFalse(params["sandboxPolicy"]["networkAccess"])
                self.assertEqual(params["cwd"], "/scratch")
                raise RuntimeError("checked")
        peer.rpc = InspectRpc()
        with self.assertRaisesRegex(RuntimeError, "checked"):
            peer.review("data", Path("/scratch"), time.monotonic() + 1, lambda: False)

    def test_native_rpc_leaves_approvals_and_tool_requests_to_the_tui(self):
        requests = [{"id": i, "method": method, "params": {"threadId": "thread"}}
                    for i, method in enumerate(("item/commandExecution/requestApproval",
                        "item/fileChange/requestApproval", "item/permissions/requestApproval",
                        "item/tool/requestUserInput", "mcpServer/elicitation/request", "item/tool/call"))]
        class Transport:
            def __init__(self, *args): self.pending = iter(requests); self.sent = []
            def receive(self):
                try: return next(self.pending)
                except StopIteration: raise EOFError()
            def send(self, msg): self.sent.append(msg)
            def close(self): pass
        with tempfile.TemporaryDirectory() as temp, patch("codex_peer.LocalSocket", Transport):
            rpc = Rpc(Path("unused"), Path(temp) / "log", delegate_requests=True)
            rpc.reader.join(timeout=2)
            self.assertEqual(rpc.transport.sent, [])
            self.assertEqual([rpc.events.get_nowait() for _ in requests], requests)
            rpc.close()


if __name__ == "__main__":
    unittest.main()
