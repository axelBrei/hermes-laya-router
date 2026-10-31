"""Punto de entrada del plugin laya-router para Hermes Agent."""
try:
    # Cargado como paquete (cualquier nombre, incluido "laya_router"): import relativo.
    from .laya_router.plugin import register  # noqa: F401
except ImportError:
    # Cargado sin paquete padre: se agrega la raíz al sys.path y se importa absoluto.
    import sys
    from pathlib import Path

    _ROOT = str(Path(__file__).resolve().parent)
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

    from laya_router.plugin import register  # noqa: E402,F401
