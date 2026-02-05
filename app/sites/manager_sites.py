# app/sites/manager_sites.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple
import importlib
import inspect
import pkgutil


@dataclass
class SiteEntry:
    # Campos que app/main.py espera mostrar
    site_id: str
    site_name: str
    module_path: str
    capabilities: Tuple[str, ...]

    # Callables del sitio
    run_single: Optional[Callable[..., Any]] = None
    run_from_file: Optional[Callable[..., Any]] = None

    # Compat: si en algún lado usás "name/module"
    @property
    def name(self) -> str:
        return self.site_name

    @property
    def module(self) -> str:
        return self.module_path

    def help(self) -> str:
        """Ayuda simple del sitio (doc + firmas)."""
        try:
            mod = importlib.import_module(self.module_path)
            doc = (getattr(mod, "__doc__", "") or "").strip()
        except Exception:
            doc = ""

        parts = [f"== {self.site_id} :: {self.site_name} ==", ""]
        if doc:
            parts.append(doc)
            parts.append("")

        if callable(self.run_single):
            parts.append("run_single" + str(inspect.signature(self.run_single)))
        if callable(self.run_from_file):
            parts.append("run_from_file" + str(inspect.signature(self.run_from_file)))

        parts.append("")
        parts.append("capabilities: " + (", ".join(self.capabilities) if self.capabilities else "-"))
        parts.append("module: " + self.module_path)
        return "\n".join(parts)


def discover_sites() -> List[SiteEntry]:
    """
    Descubre sitios dentro de app.sites.* (excluye app.sites.experimental.*)
    Un sitio válido debe exponer:
      - SITE_ID (str)
      - SITE_NAME (str) opcional
      - run_single(query, ...) opcional
      - run_from_file(query_file, ...) opcional
    """
    entries: List[SiteEntry] = []

    pkg = importlib.import_module("app.sites")

    for m in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
        modname = m.name

        # excluir este manager y cosas internas
        if modname.endswith(".manager_sites") or modname.endswith(".__init__"):
            continue
        # excluir experimentales (en BookSearchV2 tu carpeta es "experimental/")
        if ".experimental." in modname or modname.endswith(".experimental"):
            continue

        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue

        site_id = getattr(mod, "SITE_ID", None)
        if not site_id:
            continue

        site_name = getattr(mod, "SITE_NAME", None) or str(site_id)

        run_single = getattr(mod, "run_single", None)
        run_from_file = getattr(mod, "run_from_file", None)

        caps: List[str] = []
        if callable(run_single):
            caps.append("query")
        if callable(run_from_file):
            caps.append("query-file")
        if not caps:
            continue

        entries.append(
            SiteEntry(
                site_id=str(site_id),
                site_name=str(site_name),
                module_path=modname,
                capabilities=tuple(caps),
                run_single=run_single if callable(run_single) else None,
                run_from_file=run_from_file if callable(run_from_file) else None,
            )
        )

    entries.sort(key=lambda e: e.site_id)
    return entries