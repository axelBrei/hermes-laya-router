"""El __init__.py de la raíz debe exponer register con cualquier nombre de módulo que use Hermes."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def clean_modules():
    """Restaura sys.modules y sys.path para que cada carga no contamine al resto de la suite."""
    saved_modules = dict(sys.modules)
    saved_path = list(sys.path)
    yield
    for name in list(sys.modules):
        if name not in saved_modules:
            del sys.modules[name]
    sys.modules.update(saved_modules)
    sys.path[:] = saved_path


def _drop(prefix):
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def _load(name, as_package=True):
    # submodule_search_locations=None fuerza un módulo sin paquete (sin __path__ ni parent).
    kwargs = {"submodule_search_locations": [str(ROOT)] if as_package else None}
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", **kwargs)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", ["laya_router", "laya-router", "hermes-laya-router", "hermes_plugins_laya_router"])
def test_root_init_exposes_register_under_any_name(clean_modules, name):
    _drop(name)
    _drop("laya_router")
    mod = _load(name)
    assert callable(mod.register)
    assert mod.register.__name__ == "register"
    assert mod.register.__module__.endswith("laya_router.plugin")


def test_root_init_falls_back_to_absolute_import_without_package(clean_modules):
    _drop("laya_router")
    sys.path[:] = [p for p in sys.path if Path(p).resolve() != ROOT]
    mod = _load("laya_router_suelto", as_package=False)
    assert callable(mod.register) and mod.register.__module__ == "laya_router.plugin"
