from __future__ import annotations
import argparse
from app.services.sites_registry import _discover_sites, _get_site

def cmd_sites(_args: argparse.Namespace) -> int:
    sites = _discover_sites()
    print("Sitios disponibles:\n")
    for s in sites:
        caps = ", ".join(s.capabilities) if s.capabilities else "-"
        print(f"  - {s.site_id:<14} | {s.site_name:<22} | {caps:<15} | {s.module_path}")
    return 0

def cmd_site_help(args: argparse.Namespace) -> int:
    site = _get_site(args.site, verbose=getattr(args, 'verbose', False))
    if hasattr(site, 'help') and callable(getattr(site, 'help')):
        print(site.help())
    else:
        caps = ', '.join(getattr(site, 'capabilities', []) or []) or '-'
        mod = getattr(site, 'module_path', '') or getattr(site, 'module', '')
        print(f"Site: {site.site_id}\nModule: {mod}\nCapabilities: {caps}")
    return 0