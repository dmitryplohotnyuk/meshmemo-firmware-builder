"""Block Python network access during the offline install/build stages."""
import os
import sys

if os.environ.get("MESHMEMO_OFFLINE_BUILD") == "1":
    def deny_network(event, args):
        if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"):
            raise RuntimeError("MeshMemo offline build: network access is disabled")
    sys.addaudithook(deny_network)
