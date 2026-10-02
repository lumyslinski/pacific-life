"""Settings, read from the environment once at start-up."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_ORIGINS = "http://localhost:5173,http://localhost:3000,http://localhost:4200"


@dataclass(frozen=True)
class Settings:
    database_url: str
    auth_mode: str = "none"                 # "dev" or "none" (see auth.py)
    allowed_origins: tuple[str, ...] = field(default_factory=tuple)
    pool_min_size: int = 1
    pool_max_size: int = 10
    statement_timeout_ms: int = 15_000      # no request may hold a connection longer

    @classmethod
    def from_environment(cls) -> "Settings":
        url = os.environ.get("GEA_DATABASE_URL")
        if not url:
            raise SystemExit("Set GEA_DATABASE_URL, e.g. postgresql://gea_app:secret@localhost:5432/gea")
        origins = os.environ.get("FRONTEND_ORIGINS", DEFAULT_ORIGINS)
        return cls(
            database_url=url,
            auth_mode=os.environ.get("GEA_AUTH_MODE", "none"),
            allowed_origins=tuple(origin.strip() for origin in origins.split(",") if origin.strip()),
            pool_min_size=int(os.environ.get("GEA_POOL_MIN", "1")),
            pool_max_size=int(os.environ.get("GEA_POOL_MAX", "10")),
            statement_timeout_ms=int(os.environ.get("GEA_STATEMENT_TIMEOUT_MS", "15000")),
        )
