from __future__ import annotations

import inspect
import importlib
import pkgutil
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from app.utils.common import _stderr

@dataclass 
class SiteEntry:
    site_id: str
    site_name: str
    module_path: str
    capabilities: Tuple[str, ...]
    run_single: Optional[Any] = None
    run_from_file: Optional[Any] = None

    def help(self) -> str:
        doc = ''
        try:
            mod = importlib.import_module(self.module_path)
            doc = (getattr(mod, 'HELP', '') or getattr(mod, '__doc__', '') or '').strip()
        except Exception:
            pass

        parts: List[str] = []
        parts.append(f"== {self.site_id} :: {self.site_name} ==")
        parts.append('')
        if doc:
            parts.append(doc)
            parts.append('')

        try:
            if callable(self.run_single):
                parts.append('run_single' + str(inspect.signature(self.run_single)))
            if callable(self.run_from_file):
                parts.append('run_from_file' + str(inspect.signature(self.run_from_file)))
        except Exception:
            pass

        caps = ', '.join(self.capabilities) if self.capabilities else '-'
        parts.append('')
        parts.append('capabilities: ' + caps)
        parts.append('module: ' + self.module_path)
        return '\n'.join(parts)

def _discover_sites_inline(verbose: bool = False) -> List[SiteEntry]:
    entries: List[SiteEntry] = []

    pkg = importlib.import_module('app.sites')
    for m in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + '.'):
        modname = m.name

        if modname.endswith('.__init__'):
            continue
        if modname.endswith('.manager_sites'):
            continue
        if modname.endswith('.experimental') or '.experimental.' in modname:
            continue

        try:
            mod = importlib.import_module(modname)
        except Exception as e:
            if verbose:
                _stderr(f"[DISCOVER] No se pudo importar {modname}: {e}")
            continue

        site_id = getattr(mod, 'SITE_ID', None)
        if not site_id:
            continue

        site_name = getattr(mod, 'SITE_NAME', None) or str(site_id)
        run_single = getattr(mod, 'run_single', None)
        run_from_file = getattr(mod, 'run_from_file', None)

        caps: List[str] = []
        if callable(run_single):
            caps.append('query')
        if callable(run_from_file):
            caps.append('query-file')
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

    entries.sort(key=lambda x: x.site_id)
    return entries

def _discover_sites(verbose: bool = False):
    try:
        from app.sites.manager_sites import discover_sites
        return discover_sites()
    except Exception as e:
        if verbose:
            _stderr(f"[DISCOVER] manager_sites no disponible ({e}); usando discovery inline")
        return _discover_sites_inline(verbose=verbose)

def _get_site(site_id: str, verbose: bool = False):
    sites = {s.site_id: s for s in _discover_sites(verbose=verbose)}
    if site_id not in sites:
        raise SystemExit(f"[ERROR] Site '{site_id}' no existe. Usá: python -m app sites")
    return sites[site_id]

def _safe_call(fn, *args, **kwargs):
    if not callable(fn):
        return None
    sig = inspect.signature(fn)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in params and v is not None}
    return fn(*args, **filtered)

def _safe_run_single(site_entry, query, **kwargs):
      
    fn = site_entry.run_single
    if not callable(fn):
        raise RuntimeError(f"Site '{site_entry.site_id}' no tiene run_single")

    sig = inspect.signature(fn)
    params = sig.parameters

    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(query, **kwargs)

    filtered = {k: v for k, v in kwargs.items() if k in params and v is not None}
    return fn(query, **filtered)

def _safe_run_from_file(site_entry, query_file, **kwargs):
    fn = site_entry.run_from_file
    if not callable(fn):
        raise RuntimeError(f"Site '{site_entry.site_id}' no tiene run_from_file")

    sig = inspect.signature(fn)
    params = sig.parameters

    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(query_file, **kwargs)

    filtered = {k: v for k, v in kwargs.items() if k in params and v is not None}
    return fn(query_file, **filtered)