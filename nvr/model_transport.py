"""Guard every HTTPS status/header/body read without weakening standard TLS."""
import http.client
import io
import socket
import time
import urllib.request


class DownloadGuard:
    def __init__(self, cancel, deadline):
        self.cancel, self.deadline = cancel, deadline

    def check(self):
        if self.cancel.is_set():
            raise InterruptedError()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("下载超时，请检查网络后重试")
        return remaining


class _GuardedReader(io.RawIOBase):
    def __init__(self, sock, guard):
        super().__init__()
        self.sock, self.guard = sock, guard
        # Keep the normal socket-file ownership reference. Read recv_into
        # directly: SocketIO cannot be reused after a polling timeout.
        self.owner = sock.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, target):
        idle_deadline = time.monotonic() + 10
        while True:
            remaining = self.guard.check()
            idle_remaining = idle_deadline - time.monotonic()
            if idle_remaining <= 0:
                raise TimeoutError("HTTPS read idle timeout")
            previous_timeout = self.sock.gettimeout()
            self.sock.settimeout(min(.25, remaining, idle_remaining))
            try:
                try:
                    count = self.sock.recv_into(target)
                except socket.timeout:
                    continue
            finally:
                # CONNECT reads happen before TLS negotiation. Polling must
                # not shorten the following standard handshake/write budget.
                self.sock.settimeout(previous_timeout)
            self.guard.check()
            return count

    def close(self):
        try:
            self.owner.close()
        finally:
            super().close()


class _ResponseSocket:
    def __init__(self, sock, guard):
        self.sock, self.guard = sock, guard

    def makefile(self, mode):
        return io.BufferedReader(_GuardedReader(self.sock, self.guard))


def guarded_https_handler(guard):
    class Response(http.client.HTTPResponse):
        def __init__(self, sock, *args, **kwargs):
            super().__init__(_ResponseSocket(sock, guard), *args, **kwargs)

    class Connection(http.client.HTTPSConnection):
        response_class = Response

        def connect(self):
            guard.check()
            super().connect()  # standard certificate/hostname verification and proxy CONNECT
            guard.check()

    class Handler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            guard.check()
            return self.do_open(Connection, request, context=self._context)

    return Handler()
