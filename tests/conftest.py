from contextlib import contextmanager
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import os

os.environ["SNOWFLAKE_DISABLE_PLATFORM_DETECTION"] = "true"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"

import pytest
import snowflake.connector

from snowflake_emulator.testing import running_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def emulator():
    with running_server() as options:
        yield options


@pytest.fixture
def connection(emulator):
    connection = snowflake.connector.connect(**emulator)
    with connection.cursor() as c:
        for sql in ["CREATE DATABASE INSURANCE", "CREATE SCHEMA INSURANCE.CALC", "USE SCHEMA INSURANCE.CALC"]:
            c.execute(sql)
    yield connection
    connection.close()


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def process_server(args, health_url, env=None):
    process = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(process.stdout.read())
            try:
                urllib.request.urlopen(health_url, timeout=0.5).read()
                break
            except Exception:
                time.sleep(0.05)
        else:
            raise RuntimeError("Service did not become ready.")
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
