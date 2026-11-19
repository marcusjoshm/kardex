import urllib.request

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test may reach a real server; Discogs calls go through recorded fixtures."""
    def refuse(*a, **k):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
