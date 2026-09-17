import argparse
import snowflake.connector
import uvicorn

from .app import create_app
from .bootstrap import connection_options, seed


def main():
    parser = argparse.ArgumentParser(description="Frontend-facing calculation simulation API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--init-demo", action="store_true")
    args = parser.parse_args()
    options = connection_options()
    if args.init_demo:
        with snowflake.connector.connect(**options) as conn:
            seed(conn)
    uvicorn.run(create_app(options), host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
