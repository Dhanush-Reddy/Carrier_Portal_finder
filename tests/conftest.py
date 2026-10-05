import json
from pathlib import Path

import pytest

from portalfinder.db import connect

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())["results"]["bindings"]


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()
