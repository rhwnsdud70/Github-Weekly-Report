from importlib import import_module


def test_package_imports():
    for name in ('config', 'collectors', 'models', 'database', 'services', 'telegram'):
        assert import_module(name)
