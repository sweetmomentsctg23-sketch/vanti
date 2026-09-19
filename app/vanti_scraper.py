import asyncio
import os
import re
import unicodedata
from playwright.sync_api import sync_playwright

def _consultar_factura_vanti_sync(empresa: str, referencia: str) -> dict:
    if not empresa or not referencia or str(empresa).strip() == "" or str(referencia).strip() == "":
        return {
            "success": False,
            "message": "La empresa y la referencia son obligatorias para realizar la consulta."
        }

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process,SameSiteByDefaultCookies,CookiesWithoutSameSiteMustBeSecure",
                "--allow-third-party-cookies"
            ]
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'cookieEnabled', { get: () => true });
        """)

        try:
            # 1. Entrar a la página principal de Vanti
            page.goto("https://www.grupovanti.com/", wait_until="domcontentloaded", timeout=45000)

            # 2. CONDICIÓN: Si aparece el primer modal/aviso inicial, cerrarlo cuando sea visible
            try:
                btn_cerrar_modal = page.locator('button[aria-label="Cerrar modal"]').first
                if btn_cerrar_modal.is_visible(timeout=4000):
                    btn_cerrar_modal.click(force=True)
            except Exception:
                pass

            # 3. CONDICIÓN: Si aparece el aviso de cookies, hacer clic en "Aceptar" cuando sea visible
            try:
                # Buscamos el botón de aceptar cookies exactamente con la estructura que descubriste
                btn_cookies = page.locator('button.button.button-outline:has-text("Aceptar"), button:has-text("Aceptar")').first
                if btn_cookies.is_visible(timeout=4000):
                    btn_cookies.click(force=True)
            except Exception:
                pass

            # 4. CONDICIÓN: Hacer clic en el enlace/botón que lleva a la pasarela de pagos cuando esté disponible
            try:
                link_pagos = page.locator('a[href*="pagosenlinea.grupovanti.com"]').first
                if link_pagos.is_visible(timeout=5000):
                    link_pagos.click(force=True)
                    # Esperar a que la URL cambie efectivamente a la pasarela
                    page.wait_for_url("**/pagosenlinea.grupovanti.com/**", timeout=20000)
                else:
                    page.goto("https://pagosenlinea.grupovanti.com/", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                page.goto("https://pagosenlinea.grupovanti.com/", wait_until="domcontentloaded", timeout=45000)

            # 5. CONDICIÓN: Esperar a que el selector de empresa esté visible en la pasarela
            select_elem = page.locator('select#empresa')
            select_elem.wait_for(state="visible", timeout=20000)

            val_str = str(empresa).strip()
            try:
                select_elem.select_option(value=val_str, timeout=5000)
            except Exception:
                options = select_elem.locator('option').all()
                selected = False
                for opt in options:
                    opt_val = opt.get_attribute("value")
                    opt_text = opt.inner_text()
                    if opt_val == val_str or val_str.lower() in opt_text.lower():
                        select_elem.select_option(value=opt_val)
                        selected = True
                        break
                if not selected:
                    raise Exception(f"No se encontró la empresa: '{val_str}'")

            # 6. Ingresar Referencia de forma segura
            input_elem = page.locator('input[formcontrolname="reference"], input[name="reference"]').first
            input_elem.wait_for(state="visible", timeout=10000)
            input_elem.click()
            input_elem.fill("")
            input_elem.press_sequentially(str(referencia), delay=30)
            
            input_elem.dispatch_event("input")
            input_elem.dispatch_event("change")
            input_elem.dispatch_event("blur")

            # 7. Seleccionar Bancolombia cuando aparezca y no esté disabled
            label_bancolombia = page.locator('label[for="image2"], img[src*="bancolombia"]').first
            label_bancolombia.wait_for(state="visible", timeout=10000)
            
            try:
                page.wait_for_selector('input#image2[value="122"]:not([disabled])', timeout=5000)
            except Exception:
                pass

            label_bancolombia.click(force=True)

            # 8. Clic en Consultar
            btn = page.locator('button.query-button').first
            btn.click(force=True)

            # 9. CONDICIÓN DE CARGA: Esperar a que aparezca el spinner de carga (si sale) y luego esperar a que desaparezca
            overlay_selector = 'ngx-spinner, .ngx-spinner-overlay, block-ui-spinner, .block-ui-wrapper, div:has-text("Cargando...")'
            try:
                page.wait_for_selector(overlay_selector, state="visible", timeout=2000)
                page.wait_for_selector(overlay_selector, state="hidden", timeout=25000)
            except Exception:
                pass

            # 10. CONDICIÓN DE RESPUESTA: Esperar a que aparezca en pantalla CUALQUIER elemento que indique fin de proceso (Error o Resultado)
            selector_resultado = 'label.disabled, #swal2-html-container, .swal2-popup'
            page.wait_for_selector(selector_resultado, state="visible", timeout=25000)

            # 11. Evaluar si salió un Modal de Error (SweetAlert2)
            swal_text = page.locator('#swal2-html-container').first
            if swal_text.count() > 0 and swal_text.is_visible():
                mensaje_error = swal_text.inner_text().strip()
                if mensaje_error:
                    browser.close()
                    return {
                        "success": False,
                        "message": mensaje_error
                    }

            # 12. Extraer valor a pagar del DOM
            monto = 0.0
            texto_monto = ""
            labels_disabled = page.locator('label.disabled').all()

            for lbl in labels_disabled:
                txt = lbl.inner_text().strip()
                if "$" in txt:
                    texto_monto = txt
                    break

            if texto_monto:
                texto_limpio = unicodedata.normalize("NFKC", texto_monto)
                texto_limpio = texto_limpio.replace(str(referencia), "")
                texto_limpio = texto_limpio.replace(" ", "").strip()
                
                match = re.search(r'\$?([\d\.\,]+)', texto_limpio)
                if match:
                    val_str = match.group(1)
                    if "," in val_str and "." in val_str:
                        val_str = val_str.replace(',', '')
                    elif "," in val_str:
                        partes = val_str.split(",")
                        if len(partes[-1]) == 3:
                            val_str = val_str.replace(',', '')
                        else:
                            val_str = val_str.replace(',', '.')
                    elif "." in val_str:
                        partes = val_str.split(".")
                        if len(partes[-1]) == 3:
                            val_str = val_str.replace('.', '')

                    try:
                        monto = float(val_str)
                    except ValueError:
                        pass

            browser.close()

            if monto > 0:
                return {
                    "success": True,
                    "reference": referencia,
                    "amount": monto,
                    "name": "Usuario Vanti"
                }
            else:
                return {
                    "success": False,
                    "message": "No se pudo extraer el valor a pagar de la pantalla.",
                    "raw_text": texto_monto
                }

        except Exception as e:
            try:
                browser.close()
            except Exception:
                pass
            return {
                "success": False,
                "message": f"Error en la automatización: {str(e)}"
            }

async def consultar_factura_vanti(empresa: str, referencia: str) -> dict:
    return await asyncio.to_thread(_consultar_factura_vanti_sync, empresa, referencia)
