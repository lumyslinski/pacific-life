"""Run integration tests against the official DynamoDB Local JAR.

Usage: python tests/run_with_dynamodb_local.py /path/to/extracted/dynamodb [pytest arguments]
The Java service and pytest share the same execution/network environment.
"""
from pathlib import Path
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    home = Path(sys.argv[1]).resolve()
    if not (home / "DynamoDBLocal.jar").is_file():
        raise SystemExit("DynamoDBLocal.jar is missing from the supplied directory.")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = ["java", "-Djava.library.path=./DynamoDBLocal_lib", "-jar", "DynamoDBLocal.jar",
               "-port", str(port), "-sharedDb", "-inMemory", "-disableTelemetry"]
    server = subprocess.Popen(command, cwd=home)
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while True:
            if server.poll() is not None:
                raise RuntimeError("DynamoDB Local exited before becoming ready.")
            try:
                urllib.request.urlopen(endpoint, timeout=1).close()
                break
            except urllib.error.HTTPError:
                break  # An unsigned request returns 400; the server is ready.
            except urllib.error.URLError:
                if time.monotonic() > deadline:
                    raise RuntimeError("DynamoDB Local readiness timeout.")
                time.sleep(0.1)
        env = {**os.environ, "DYNAMODB_TEST_ENDPOINT": endpoint,
               "SNOWFLAKE_DISABLE_PLATFORM_DETECTION": "true", "AWS_EC2_METADATA_DISABLED": "true"}
        return subprocess.call([sys.executable, "-m", "pytest", *(sys.argv[2:] or ["-q"])],
                               env=env, cwd=Path(__file__).resolve().parents[1])
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
