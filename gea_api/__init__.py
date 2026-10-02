"""GEA API: projects, run configuration (the wizard) and data contracts on PostgreSQL.

The HTTP contract is gea/api/openapi.yaml. The package is layered so that only
two modules know a framework:

    routes.py, app.py   Litestar: paths, headers, status codes
    db.py               psycopg: connection pool and transactions
    services.py         one function per operation: preconditions, idempotency, writes
    queries.py          the SQL; every query returns API-shaped JSON built by PostgreSQL
    errors.py           the one error object of the envelope and the SQLSTATE mapping
    http.py, validation.py, auth.py   plain helpers
"""
