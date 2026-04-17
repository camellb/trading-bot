"""Shared SQLAlchemy engine singleton — every module that needs DB access imports from here."""

import os
from sqlalchemy import create_engine as _sa_create_engine

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        _engine = _sa_create_engine(url, pool_pre_ping=True)
    return _engine
