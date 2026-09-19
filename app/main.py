import sys
import asyncio
import os
import random
import shutil
from typing import List

# 1. Configurar política de Event Loop para Windows ANTES de iniciar cualquier tarea asíncrona
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import (
    init_db, guardar_transaccion, actualizar_estado_transaccion,
    obtener_transaccion, obtener_todas_transacciones, obtener_metricas,
    bloquear_ip, es_ip_bloqueada, guardar_otp_admin, verificar_otp_admin,
    actualizar_actividad_transaccion
)

from app.vanti_scraper import consultar_factura_vanti
from app.telegram_utils import enviar_mensaje_telegram

# 2. Única instancia de la aplicación FastAPI
app = FastAPI(title="Sistema Vanti & Panel Admin")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Monta la carpeta static ubicada afuera de app
# Inicializar BD al arrancar
init_db()

# Archivos estáticos y plantillas
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Gestor de conexiones WebSockets para el Panel Admin
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

# Helper para la IP del cliente
def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0]
    return request.client.host or "127.0.0.1"

# Middleware para bloqueo de IP
@app.middleware("http")
async def check_ip_blocking(request: Request, call_next):
    ip = get_client_ip(request)
    if es_ip_bloqueada(ip) and not request.url.path.startswith("/static"):
        return HTMLResponse("<h1>403 Acceso Denegado - Su IP ha sido bloqueada.</h1>", status_code=403)
    response = await call_next(request)
    return response

# --- RUTAS PÚBLICAS (CLIENTE) ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"error": None})

@app.post("/consultar", response_class=HTMLResponse)
async def consultar(request: Request, empresa: str = Form(...), referencia: str = Form(...)):
    ip = get_client_ip(request)
    
    # 1. Ejecutar Playwright en segundo plano
    resultado = await consultar_factura_vanti(empresa, referencia)
    
    if not resultado.get("success"):
        return templates.TemplateResponse(request, "index.html", {
            "error": resultado.get("message", "Error al consultar la referencia.")
        })
    
    # 2. Guardar en Base de Datos
    monto = float(resultado.get("amount", 0))
    tx_id = guardar_transaccion(empresa, referencia, monto, ip)

    # OBTENER LA TRANSACCIÓN RECIÉN CREADA Y LAS MÉTRICAS
    tx = obtener_transaccion(tx_id)
    metricas = obtener_metricas()

    # 3. 🔔 NOTIFICAR AL PANEL ADMIN EN TIEMPO REAL QUE ENTRÓ UN CLIENTE NUEVO
    await manager.broadcast({
        "event": "NUEVA_CONSULTA",
        "tx": tx,
        "metricas": metricas
    })

    # 4. Renderizar Checkout
    return templates.TemplateResponse(request, "checkout.html", {
        "tx_id": tx_id,
        "referencia": referencia,
        "monto": int(monto),
        "empresa": empresa
    })

@app.post("/notificar_pago", response_class=HTMLResponse)
async def notificar_pago(request: Request, tx_id: int = Form(...)):
    ip = get_client_ip(request)
    tx = obtener_transaccion(tx_id)
    
    if tx:
        # 1. Actualizar el estado en la BD usando la función correcta de database.py
        actualizar_estado_transaccion(tx_id, "por_verificar")
        
        # Actualizamos el diccionario local para enviar los datos correctos
        tx["estado"] = "por_verificar"

        # 2. Notificar al Panel vía WebSocket con el estado ya actualizado
        await manager.broadcast({
            "event": "NUEVO_PAGO",
            "tx": tx,
            "metricas": obtener_metricas()
        })

        # 3. Notificar a Telegram
        enviar_mensaje_telegram(
            f"🔔 <b>¡Nuevo pago recibido para verificar!</b>\n"
            f"• <b>Empresa:</b> {tx['empresa']}\n"
            f"• <b>Referencia:</b> {tx['referencia']}\n"
            f"• <b>Monto:</b> ${tx['monto']:,.0f}\n"
            f"• <b>IP:</b> {ip}"
        )

    return templates.TemplateResponse(request, "esperando.html", {"tx_id": tx_id})

@app.get("/estado_pago/{tx_id}")
async def estado_pago(tx_id: int):
    tx = obtener_transaccion(tx_id)
    if tx:
        actualizar_actividad_transaccion(tx_id)
        return {"estado": tx["estado"]}
    return {"estado": "no_encontrado"}

@app.post("/actividad/{tx_id}")
async def registrar_actividad(tx_id: int):
    tx = obtener_transaccion(tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Transacción no encontrada.")
    actualizar_actividad_transaccion(tx_id)
    return {"status": "ok"}

@app.get("/resultado/{tx_id}", response_class=HTMLResponse)
async def resultado_final(request: Request, tx_id: int):
    tx = obtener_transaccion(tx_id)
    return templates.TemplateResponse(request, "estado.html", {"tx": tx})

# --- PANEL DE ADMINISTRACIÓN Y SEGURIDAD TELEGRAM ---

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    # Generar OTP de 6 dígitos
    otp = str(random.randint(100000, 999999))
    guardar_otp_admin(otp)
    enviar_mensaje_telegram(f"🔐 <b>Código de Seguridad para Admin:</b> <code>{otp}</code>")
    return templates.TemplateResponse(request, "admin_login.html", {"error": None})

@app.post("/admin/login")
async def admin_login_post(request: Request, otp: str = Form(...)):
    if verificar_otp_admin(otp):
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(key="admin_session", value="authenticated_vanti", httponly=True)
        return response
    return templates.TemplateResponse(request, "admin_login.html", {"error": "Código OTP inválido o expirado."})

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    if request.cookies.get("admin_session") != "authenticated_vanti":
        return RedirectResponse(url="/admin/login")

    transacciones = obtener_todas_transacciones()
    metricas = obtener_metricas()
    return templates.TemplateResponse(request, "admin.html", {
        "transacciones": transacciones,
        "metricas": metricas
    })

@app.get("/admin/datos")
async def admin_datos(request: Request):
    if request.cookies.get("admin_session") != "authenticated_vanti":
        raise HTTPException(status_code=401, detail="Sesión de administrador requerida.")

    return {
        "transacciones": obtener_todas_transacciones(),
        "metricas": obtener_metricas()
    }

# Cargar nuevo Código QR globalmente desde la parte superior del Admin
@app.post("/admin/actualizar_qr")
async def actualizar_qr(file: UploadFile = File(...)):
    os.makedirs("static/uploads", exist_ok=True)
    file_path = "static/uploads/qr_actual.png"
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    # Notificar a la app que el QR se actualizó
    await manager.broadcast({"event": "QR_ACTUALIZADO"})
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/cambiar_estado")
async def cambiar_estado(tx_id: int = Form(...), nuevo_estado: str = Form(...)):
    # 1. Actualizar el estado en la Base de Datos usando la función correcta importada
    actualizar_estado_transaccion(tx_id, nuevo_estado)
    
    # 2. Obtener la transacción actualizada y las métricas nuevas
    tx = obtener_transaccion(tx_id)
    metricas = obtener_metricas()

    # 3. Hacer broadcast al WebSocket para que el panel se actualice en vivo
    await manager.broadcast({
        "event": "CAMBIO_ESTADO",
        "tx_id": tx_id,
        "nuevo_estado": nuevo_estado,  # Aquí viaja 'pagado' o 'no_pagado'
        "tx": tx,
        "metricas": metricas
    })
    
    return {"status": "success"}

@app.post("/admin/bloquear_ip")
async def api_bloquear_ip(ip: str = Form(...)):
    bloquear_ip(ip)
    return {"status": "ok", "message": f"IP {ip} bloqueada con éxito."}
# WebSocket Endpoint para el Admin
@app.websocket("/ws/admin")
async def websocket_admin(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Usamos un timeout o un try/except ligero para que el socket 
            # escuche latidos sin bloquear los mensajes que mandamos desde el servidor
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # El timeout de 30s evita que se quede congelado esperando indefinidamente
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)