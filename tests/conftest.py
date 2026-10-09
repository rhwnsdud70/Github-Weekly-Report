import socket
import pytest


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def blocked(*args,**kwargs):
        raise AssertionError('Tests must use fixtures or MockTransport, not real network calls')
    monkeypatch.setattr(socket.socket,'connect',blocked)
