# This file is part of Xpra.
# Copyright (C) 2016-2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from xpra.util import envbool
from xpra.net.websocket import make_websocket_accept_hash
from xpra.server.http_handler import HTTPRequestHandler
from xpra.log import Logger

log = Logger("network", "websocket")

WEBSOCKET_ONLY_UPGRADE = envbool("XPRA_WEBSOCKET_ONLY_UPGRADE", False)

# HyBi-07 report version 7
# HyBi-08 - HyBi-12 report version 8
# HyBi-13 reports version 13
SUPPORT_HyBi_PROTOCOLS = ("7", "8", "13")


class WebSocketRequestHandler(HTTPRequestHandler):

    server_version = "Xpra-WebSocket-Server"

    def __init__(self, sock, addr, new_websocket_client, web_root="/usr/share/xpra/www/", http_headers_dir="/usr/share/xpra/http-headers", script_paths={}):
        self.new_websocket_client = new_websocket_client
        self.only_upgrade = WEBSOCKET_ONLY_UPGRADE
        HTTPRequestHandler.__init__(self, sock, addr, web_root, http_headers_dir, script_paths)

    def handle_websocket(self):
        log("handle_websocket() calling %s, request=%s (%s)", self.new_websocket_client, self.request, type(self.request))
        ver = self.headers.get('Sec-WebSocket-Version')
        if ver is None:
            raise Exception("Missing Sec-WebSocket-Version header");

        if ver not in SUPPORT_HyBi_PROTOCOLS:
            raise Exception("Unsupported protocol version %s" % ver)

        protocols = self.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if "binary" not in protocols:
            raise Exception("client does not support 'binary' protocol")

        key = self.headers.get("Sec-WebSocket-Key")
        if key is None:
            raise Exception("Missing Sec-WebSocket-Key header");
        for upgrade_string in (
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Accept: %s" % make_websocket_accept_hash(key),
            "Sec-WebSocket-Protocol: %s" % "binary",
            "",
            ):
            self.wfile.write("%s\r\n" % upgrade_string)
        self.wfile.flush()
        self.wfile.close()
        self.new_websocket_client(self)

    def do_GET(self):
        if self.only_upgrade or (self.headers.get('upgrade') and
            self.headers.get('upgrade').lower() == 'websocket'):
            try:
                self.handle_websocket()
            except Exception as e:
                log("do_GET()", exc_info=True)
                log.error("Error: cannot handle websocket upgrade:")
                log.error(" %s", e)
                self.send_error(403, "failed to handle websocket: %s" % e)
                self.wfile.flush()
                self.wfile.close()
            return
        self.handle_request()

    def handle_request(self):
        if self.only_upgrade:
            self.send_error(405, "Method Not Allowed")
        else:
            content = self.send_head()
            if content:
                self.wfile.write(content)
        self.wfile.flush()
        self.wfile.close()
