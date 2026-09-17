FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY snowflake_emulator ./snowflake_emulator
COPY calculation_api ./calculation_api
COPY fixtures ./fixtures
COPY api ./api
COPY examples ./examples
COPY pyproject.toml ./

ENV PYTHONUNBUFFERED=1
ENV SNOWFLAKE_DISABLE_PLATFORM_DETECTION=true
ENV AWS_EC2_METADATA_DISABLED=true
EXPOSE 8084 8000

CMD ["python", "-m", "snowflake_emulator", "--host", "0.0.0.0", "--port", "8084", "--database", "/data/emulator.duckdb", "--history", "/data/query-history.jsonl"]
