"""Pytest fixtures: mock MQTT client, mock DB connection, temp data dir."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on the path so `from src import ...` works
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
#  Temp data directory (isolates file I/O for today_stats)
# ---------------------------------------------------------------------------
@pytest.fixture
def temp_data_dir(tmp_path):
    """Provide a clean temporary DATA_DIR for file-based persistence tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


# ---------------------------------------------------------------------------
#  MQTT mock
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_mqtt_client():
    """Return a MagicMock that mimics a paho mqtt client."""
    client = MagicMock()
    client.publish = MagicMock(return_value=MagicMock(rc=0))
    client.connect = MagicMock(return_value=0)
    client.loop_start = MagicMock()
    client.loop_stop = MagicMock()
    client.is_connected = MagicMock(return_value=True)
    return client


# ---------------------------------------------------------------------------
#  Database mock
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_db_conn():
    """Return a MagicMock that mimics a psycopg2 connection + cursor."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
#  Sample today_stats payload (used across tests)
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_today_stats():
    """Return a typical today_stats dict."""
    return {
        "total": 10,
        "correct": 7,
        "incorrect": 3,
        "accuracy": 70.0,
        "consecutive_failures": 0,
        "date": date.today().isoformat(),
    }
