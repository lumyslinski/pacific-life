"""Start an actual TCP server for tests, with its own database and cleanup."""
from contextlib import contextmanager
from threading import Thread
from time import monotonic, sleep
import socket
import os

import uvicorn

from .app import create_app


@contextmanager
def running_server(database_path=":memory:"):
    os.environ["SNOWFLAKE_DISABLE_PLATFORM_DETECTION"] = "true"
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    app = create_app(database_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = monotonic() + 10
    try:
        while not server.started:
            if not thread.is_alive() or monotonic() > deadline:
                raise RuntimeError("Local emulator did not start.")
            sleep(0.01)
        yield {"account": "local", "user": "test", "password": "test",
               "host": "127.0.0.1", "port": port, "protocol": "http",
               "login_timeout": 5, "network_timeout": 10,
               "platform_detection_timeout_seconds": 0,
               "session_parameters": {"CLIENT_OUT_OF_BAND_TELEMETRY_ENABLED": False}}
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Local emulator did not stop.")
