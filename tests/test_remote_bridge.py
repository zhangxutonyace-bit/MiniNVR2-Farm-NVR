"""Verify the adapter bridge cannot become a generic LAN/HTTP proxy."""
from email.message import Message
import io
from types import SimpleNamespace
from unittest import mock

from nvr import remote_bridge as bridge
from tests.support import OfflineTestCase


class BridgeTests(OfflineTestCase):
    def handler(self, path="/info", address="100.64.1.2", headers=None):
        handler = bridge._BridgeHandler.__new__(bridge._BridgeHandler)
        handler.path = path
        handler.client_address = (address, 12345)
        handler.server = SimpleNamespace(origin_port=18888)
        handler.headers = Message()
        for key, value in (headers or {}).items():
            handler.headers[key] = value
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        return handler

    def test_non_overlay_adapter_addresses_never_bind(self):
        with mock.patch.object(bridge.socket, "socket") as socket:
            for address in ("0.0.0.0", "127.0.0.1", "192.168.1.10", "172.19.0.1", "8.8.8.8", "::1"):
                with self.assertRaises(ValueError):
                    bridge.PreviewBridge(address, 18888)
            socket.assert_not_called()

    def test_adapter_must_be_assigned_and_listener_only_binds_that_adapter(self):
        with mock.patch.object(bridge.socket, "socket") as probe:
            probe.return_value.__enter__.return_value.bind.side_effect = OSError("fixture")
            with self.assertRaises(ValueError):
                bridge.validate_adapter("100.64.1.1")
        with mock.patch.object(bridge, "validate_adapter", side_effect=lambda x: x), \
                mock.patch.object(bridge, "_BridgeServer") as server, \
                mock.patch.object(bridge.threading, "Thread"):
            server.return_value.server_port = 18889
            viewer = bridge.PreviewBridge("100.64.1.1", 18888)
            self.assertEqual(viewer.start(), 18889)
            server.assert_called_once_with(("100.64.1.1", 0), 18888)
            viewer.stop()
            server.return_value.shutdown.assert_called_once()
            server.return_value.server_close.assert_called_once()

    def test_non_overlay_clients_and_non_viewer_paths_never_reach_origin(self):
        with mock.patch.object(bridge.http.client, "HTTPConnection") as upstream:
            for path, address in (("/api/settings", "100.64.1.2"), ("/", "100.64.1.2"),
                    ("http://other.example/info", "100.64.1.2"), ("/info?token=x", "100.64.1.2"),
                    ("/frame/../admin", "100.64.1.2"), ("/preview.js/extra", "100.64.1.2"),
                    ("/frame/camera%2Fother", "100.64.1.2"), ("/info", "192.168.1.20"),
                    ("/info", "127.0.0.1"), ("/info", "8.8.8.8")):
                handler = self.handler(path, address)
                handler.do_GET()
                self.assertIn(handler.send_response.call_args.args[0], (403, 404))
            upstream.assert_not_called()

    def test_only_authorization_is_forwarded_to_fixed_loopback_origin(self):
        token = "Bearer " + "a" * 43
        handler = self.handler("/frame/farm", headers={"Authorization": token,
            "Cookie": "management-secret", "Host": "other.example", "X-Forwarded-For": "8.8.8.8"})
        with mock.patch.object(bridge.http.client, "HTTPConnection") as upstream:
            response = upstream.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b"synthetic-jpeg"
            response.getheader.side_effect = lambda name: {"Content-Type": "image/jpeg",
                "Cache-Control": "no-store"}.get(name)
            handler.do_GET()
            upstream.assert_called_once_with("127.0.0.1", 18888, timeout=3)
            upstream.return_value.request.assert_called_once_with("GET", "/frame/farm",
                headers={"Connection": "close", "Authorization": token})
            upstream.return_value.close.assert_called_once()
        self.assertEqual(handler.wfile.getvalue(), b"synthetic-jpeg")
        handler.send_header.assert_any_call("Cache-Control", "no-store")

    def test_write_methods_and_request_bodies_cannot_be_proxied(self):
        with mock.patch.object(bridge.http.client, "HTTPConnection") as upstream:
            for method in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH", "do_OPTIONS", "do_HEAD"):
                handler = self.handler()
                getattr(handler, method)()
                handler.send_response.assert_called_once_with(405)
            for headers in ({"Content-Length": "1"}, {"Transfer-Encoding": "chunked"}):
                handler = self.handler(headers=headers)
                handler.do_GET()
                self.assertIn(handler.send_response.call_args.args[0], (400, 404))
            upstream.assert_not_called()

    def test_origin_failure_or_oversized_response_returns_no_image(self):
        for oversize in (False, True):
            handler = self.handler("/frame/farm")
            with mock.patch.object(bridge.http.client, "HTTPConnection") as upstream:
                if oversize:
                    upstream.return_value.getresponse.return_value.read.return_value = b"x" * (bridge._MAX_RESPONSE + 1)
                else:
                    upstream.return_value.request.side_effect = OSError("fixture-only")
                handler.do_GET()
                handler.send_response.assert_called_once_with(502 if oversize else 503)
                self.assertEqual(handler.wfile.getvalue(), b"")
                upstream.return_value.close.assert_called_once()
