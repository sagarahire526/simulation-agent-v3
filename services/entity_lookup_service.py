"""
Entity Lookup Service — fetches distinct DB values for GCs, markets, and regions.

Used by the Query Refiner to normalize informal user references
(e.g., "voxline" → "Voxline LLC") to exact database column values.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import psycopg2

import config

logger = logging.getLogger(__name__)

_TABLE = "pwc_macro_staging_schema.stg_ndpd_mbt_tmobile_macro_combined"


def _conn():
    """Open a read-only psycopg2 connection."""
    return psycopg2.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        database=config.PG_DATABASE,
        user=config.PG_USER,
        password=config.PG_PASSWORD,
        connect_timeout=5,
        options="-c default_transaction_read_only=on",
    )


def _fetch_distinct(column: str) -> list[str]:
    """Fetch distinct non-null values for a column, sorted alphabetically."""
    sql = f"SELECT DISTINCT {column} FROM {_TABLE} WHERE {column} IS NOT NULL ORDER BY {column}"
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.warning("Entity lookup failed for %s: %s", column, e)
        return []


def get_gc_names() -> list[str]:
    """Distinct construction_gc values from the staging table."""
    return _fetch_distinct("construction_gc")


def get_market_names() -> list[str]:
    """Distinct market values from the staging table."""
    return _fetch_distinct("m_market")


def get_region_names() -> list[str]:
    """Distinct region values from the staging table."""
    return _fetch_distinct("rgn_region")


def get_all_entity_lookups() -> dict[str, list[str]]:
    """
    Fetch all entity lookups in one call.
    Returns dict with keys: gc_names, markets, regions.
    Gracefully returns empty lists on failure.
    """
    return {
        "gc_names": get_gc_names(),
        "markets": get_market_names(),
        "regions": get_region_names(),
    }
