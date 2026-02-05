from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Dict, Any

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from app.sites.experimental import amazon_books_experimental as ax


def open_chrome(headful: bool = True) -> webdriver.Chrome:
    opts = Options()
    if not headful:
        opts.add_argument("--headless=new")

    opts.add_argument("--start-maximized")
    driver = webdriver.Chrome(options=opts)
    return driver


def prime_page(driver: webdriver.Chrome, *, max_scroll: int = 9000) -> None:
    """
    Fuerza lazy-load de secciones inferiores (Detalles del producto / portada)
    y espera un poco a que aparezcan en el DOM antes de capturar page_source.
    """
    wait = WebDriverWait(driver, 25)

    # Espera a que el documento esté listo
    try:
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:
        pass

    # Scroll gradual para activar carga de secciones (bullets/imagen)
    for y in range(0, max_scroll, 900):
        driver.execute_script(f"window.scrollTo(0, {y});")
        time.sleep(0.35)

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1.2)

    # Espera a que aparezca alguno de estos elementos (si existen en esa página)
    def _has_any(d):
        return (
            len(d.find_elements(By.ID, "detailBullets_feature_div")) > 0
            or len(d.find_elements(By.ID, "productDetails_techSpec_section_1")) > 0
            or len(d.find_elements(By.ID, "landingImage")) > 0
        )

    try:
        wait.until(_has_any)
    except Exception:
        pass


def assisted_amazon_isbn(isbn: str, output_html: Optional[str] = None) -> Optional[Dict[str, Any]]:
    isbn = ax.normalize_isbn(isbn)
    if len(isbn) not in (10, 13):
        print(f"[WARN] ISBN inválido: {isbn}")
        return None

    url = f"https://www.amazon.com/s?k={isbn}&i=stripbooks"
    driver = open_chrome(headful=True)

    try:
        print(f"[INFO] Abriendo: {url}")
        driver.get(url)

        print("\n=== MODO ASISTIDO ===")
        print("1) Si aparece RobotCheck/Captcha, resolvelo manualmente.")
        print("2) Entrá al producto correcto (página del libro).")
        print("3) Cuando estés en la página del producto, presioná ENTER aquí en la consola.\n")
        input("Listo? ENTER para parsear... ")

        prime_page(driver)

        current_url = driver.current_url
        html = driver.page_source or ""

        # DEBUG útil para confirmar si el HTML capturado trae el bloque
        print(f"[DEBUG] detailBullets_feature_div en HTML: {'detailBullets_feature_div' in html}")

        if output_html:
            Path(output_html).parent.mkdir(parents=True, exist_ok=True)
            Path(output_html).write_text(html, encoding="utf-8")
            print(f"[DEBUG] HTML guardado en: {output_html}")

        row = ax.parse_product_page(html, current_url, isbn_hint=isbn)

        if not (row.get("TITULO") or "").strip():
            print("[INFO] No se pudo extraer título; probablemente no estás en el producto.")
            return None

        # DEBUG extra: cuántos bullets se sacaron
        kv = row.get("_amazon_detail_kv") or {}
        print(f"[DEBUG] bullets extraídos: {len(kv)}")

        return row

    finally:
        try:
            time.sleep(1)
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    isbn = input("ISBN (10/13): ").strip()
    row = assisted_amazon_isbn(isbn, output_html="data/debug/amazon_last.html")
    if row:
        print("\n[OK] Extraído:")
        for k in [
            "ISBN", "TITULO", "AUTOR", "EDITORIAL", "IDIOMA", "PAGINAS",
            "DIMENSIONES", "ENCUADERNACION", "FECHA PUBLICACION",
            "URL PORTADA", "URL"
        ]:
            print(f"- {k}: {row.get(k)}")

        # SINOPSIS: solo primer párrafo (o primera línea si no hay doble salto)
        syn = (row.get("SINOPSIS") or "").strip()
        if syn:
            first_para = syn.split("\n\n")[0].strip()
            if not first_para:
                first_para = syn.split("\n")[0].strip()
            print(f"\n- SINOPSIS (primer párrafo): {first_para}")
        else:
            print("\n- SINOPSIS (primer párrafo): (vacía)")
    else:
        print("\n[FAIL] Sin datos.")