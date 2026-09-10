"""Scripted in-memory ESL peer; no sockets, credentials or server required."""

from collections import deque
import socket


def frame(content_type: str, body: str = "", **headers: str) -> bytes:
    payload = body.encode("utf-8")
    values = {"Content-Type": content_type, **headers}
    if body:
        values["Content-Length"] = str(len(payload))
    return ("".join(f"{k}: {v}\n" for k, v in values.items()) + "\n").encode() + payload


class ScriptedPeer:
    """Release responses only after the exact expected client command.

    ``None`` represents a read timeout. ``fragment_size`` also splits UTF-8 and
    header delimiters, independently of the application's recv buffer size.
    """

    def __init__(self, steps, *, fragment_size=4096):
        self.steps = deque(steps)
        self.pending = deque([frame("auth/request")])
        self.fragment_size = fragment_size
        self.closed = False
        self.sent = []

    def sendall(self, data):
        assert not self.closed
        assert self.steps, f"unexpected command: {data!r}"
        expected, responses = self.steps.popleft()
        assert data == expected
        self.sent.append(data)
        self.pending.extend(responses)

    def recv(self, size):
        assert not self.closed
        if not self.pending:
            return b""
        chunk = self.pending.popleft()
        if chunk is None:
            raise socket.timeout
        count = min(size, self.fragment_size)
        if len(chunk) > count:
            self.pending.appendleft(chunk[count:])
        return chunk[:count]

    def settimeout(self, timeout):
        assert timeout > 0

    def setsockopt(self, *_args):
        pass

    def close(self):
        self.closed = True

    def assert_complete(self):
        assert not self.steps, "expected commands were not sent"


def accepted():
    return frame("command/reply", **{"Reply-Text": "+OK accepted"})
