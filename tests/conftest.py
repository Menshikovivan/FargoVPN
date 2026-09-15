import pytest
from starlette.testclient import TestClient
import webapp

@pytest.fixture
def client():
    return TestClient(webapp.app)
