from __future__ import annotations

import argparse
import re
import time
import sys
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

# Importa tu parser del sitio (el que ya tenemos en app/sites/el_lector.py)
from app.sites import el_lector
from app.standard import to_standard_row, write_standard_csv

BASE = "https://www.ellector.com.py"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Links de producto: /productos/<slug>-id-XXXX.html
# Evitamos categorías: /productos/categoria-...
RE_PROD = re.compile(r'href=["\'](/productos/(?!categoria-)[^"\']*?-id-[^"\']+?\.html)["\']', re.I)

# Detectar última página desde paginación: productos-pagina-2499.html
RE_PAGE = re.compile(r"productos-pagina-(\d+)\.html", re.I)


def catalog_page_url(n: int) -> str:
    if n <= 1:
        return f"{BASE}/productos.html"
    return f"{BASE}/productos-pagina-{n}.html"


def extract_product_urls(html: str) -> list[str]:
    rels = RE_PROD.findall(html)
    urls = [urljoin(BASE, r) for r in rels]
    # unique keep order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def detect_last_page(html: str) -> int | None:
    nums = [int(x) for x in RE_PAGE.findall(html)]
    return max(nums) if nums else None


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(p.strip() for p in path.read_text(encoding="utf-8").splitlines() if p.strip())


def append_seen(path: Path, url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(url + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--end-page", type=int, default=0, help="0 = autodetect")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--out", type=str, default="data/exports/ellector_catalog.csv")
    ap.add_argument("--seen-file", type=str, default="data/state/ellector_seen_urls.txt")
    ap.add_argument("--only-with-isbn", action="store_true", default=True)
    ap.add_argument("--max-products", type=int, default=0, help="0 = sin límite")
    args = ap.parse_args()

    out_path = Path(args.out)
    seen_path = Path(args.seen_file)
    seen = load_seen(seen_path)

    headers = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}

    collected_std: list[dict] = []

    with httpx.Client(headers=headers, follow_redirects=True) as client:
        # Autodetect end_page desde la primera página que abras
        if args.end_page == 0:
            html1 = client.get(catalog_page_url(args.start_page), timeout=args.timeout).text
            last = detect_last_page(html1)
            if last:
                args.end_page = last
                print(f"[CATALOG] Última página detectada: {args.end_page}")
            else:
                # fallback seguro: 1 sola página
                args.end_page = args.start_page
                print("[CATALOG] No pude detectar última página, usaré solo start-page.")

        total_products = 0

        for page in range(args.start_page, args.end_page + 1):
            url_page = catalog_page_url(page)
            print(f"\n[PAGE {page}] {url_page}")

            r = client.get(url_page, timeout=args.timeout)
            if r.status_code != 200:
                print(f"  [WARN] status={r.status_code}, salto página.")
                continue

            prod_urls = extract_product_urls(r.text)
            print(f"  [FOUND] productos en página: {len(prod_urls)}")

            for u in prod_urls:
                if u in seen:
                    continue

                if args.delay:
                    time.sleep(args.delay)

                try:
                    # bajamos HTML del producto y parseamos con tu parser
                    pr = client.get(u, timeout=args.timeout)
                    if pr.status_code != 200:
                        append_seen(seen_path, u)
                        seen.add(u)
                        continue

                    raw = el_lector._parse_product_page(pr.text, u)  # reutilizamos tu parser
                    # filtro: si no hay ISBN, probablemente no es libro
                    if args.only_with_isbn and not raw.get("isbn"):
                        append_seen(seen_path, u)
                        seen.add(u)
                        continue

                    std = to_standard_row(raw, site_id=el_lector.SITE_ID)
                    collected_std.append(std)

                    append_seen(seen_path, u)
                    seen.add(u)

                    total_products += 1
                    if total_products % 20 == 0:
                        print(f"  [OK] acumulados: {total_products}")

                    if args.max_products and total_products >= args.max_products:
                        print("[STOP] max-products alcanzado.")
                        break

                except Exception as e:
                    # marcamos como visto igual para no buclear
                    append_seen(seen_path, u)
                    seen.add(u)
                    print(f"  [ERR] {u} -> {e}")

            # export incremental por página (para que no pierdas progreso)
            if collected_std:
                write_standard_csv(collected_std, out_path)
                print(f"  [CSV] guardado parcial: {out_path} ({len(collected_std)} filas)")

            if args.max_products and total_products >= args.max_products:
                break

    print(f"\n[DONE] Total filas CSV: {len(collected_std)}")
    print(f"[DONE] CSV: {out_path}")
    print(f"[DONE] Seen: {seen_path}")


if __name__ == "__main__":
    main()