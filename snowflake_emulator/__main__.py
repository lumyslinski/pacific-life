import argparse
import uvicorn

from .app import create_app


def main():
    parser = argparse.ArgumentParser(description="Local Snowflake SQL-function emulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8084)
    parser.add_argument("--database", default="data/emulator.duckdb")
    parser.add_argument("--history", default="data/query-history.jsonl")
    parser.add_argument("--max-rows", type=int, default=20000)
    args = parser.parse_args()
    uvicorn.run(create_app(args.database, args.history, args.max_rows),
                host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
