"""pytest fixtures for integration tests"""
import os
import tempfile
import pytest
from db import init_db, get_connection, close_db


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """Initialize Oracle connection pool for all tests"""
    init_db()
    yield
    close_db()


@pytest.fixture
def conn():
    """Get a database connection"""
    conn = get_connection()
    yield conn


@pytest.fixture
def output_dir():
    """Create a temporary output directory"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def app_client():
    """Flask test client"""
    from app import app
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client
