import os
import math
import logging
import requests
import csv
import io
from datetime import datetime
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)

TOKEN      = os.environ["BOT_TOKEN"]
SHEET_ID   = os.environ["SHEET_ID"]
ADMIN_ID   = int(os.environ["ADMIN_ID"])
SHEET_URL  = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"

ZONAS = ["Centro", "Norte", "Sur", "Este", "Oeste"]

# ── Mapa de tiempo_maximo a categoría ────────────────────────────────────────
CATEGORIAS_TIEMPO = {
    "5 min":          "5min",
    "15 min":         "15min",
    "120 min":        "2h",
    "larga estancia": "larga",
}

def categoria_tiempo(tiempo_maximo):
    t = tiempo_maximo.strip().lower()
    for clave, cat in CATEGORIAS_TIEMPO.items():
        if clave in t:
            return cat
    return "otros"

# ── Estado en memoria ─────────────────────────────────────────────────────────
reportes     = {}
estadisticas = defaultdict(int)

def reiniciar_reportes_si_toca():
    hora = datetime.now().hour
    if hora >= 20 or hora < 9:
        reportes.clear()

# ── Google Sheets ─────────────────────────────────────────────────────────────
def cargar_parkings():
    try:
        r = requests.get(SHEET_URL, timeout=10)
        r.encoding = "utf-8"
        reader = csv.DictReader(io.StringIO(r.text))
        parkings = []
        for row in reader:
            try:
                parkings.append({
                    "nombre":        row.get("nombre", "Sin nombre").strip(),
                    "lat":           float(row.get("lat", 0)),
                    "lon":           float(row.get("lon", 0)),
                    "tiempo_maximo": row.get("tiempo_maximo", "No disponible").strip(),
                    "plazas":        row.get("plazas", "No disponible").strip(),
                    "horario":       row.get("horario", "No disponible").strip(),
                    "restricciones": row.get("restricciones", "No disponible").strip(),
                    "tipo":          row.get("tipo", "ambas").strip().lower(),
                })
            except ValueError:
                continue
        return parkings
    except Exception as e:
        logging.error(f"Error cargando parkings: {e}")
        return []

# ── Cálculo de distancia ──────────────────────────────────────────────────────
def distancia_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) *
         math.sin(dlon/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def ordenar_por_distancia(parkings, lat, lon):
    return sorted(parkings, key=lambda p: distancia_km(lat, lon, p["lat"], p["lon"]))

def seleccionar_parkings(parkings, lat, lon, tipo_filtro):
    """
    - tipo_filtro == 'todos': 1 más cercano de cada categoría de tiempo
    - tipo_filtro concreto:   2 más cercanos de ese tipo
    """
    if tipo_filtro == "todos":
        ordenados = ordenar_por_distancia(parkings, lat, lon)
        vistos    = set()
        resultado = []
        for p in ordenados:
            cat = categoria_tiempo(p["tiempo_maximo"])
            if cat not in vistos:
                vistos.add(cat)
                resultado.append(p)
            if len(vistos) == 4:
                break
        return resultado
    else:
        filtrados = [
            p for p in parkings
            if p["tipo"] == tipo_filtro or p["tipo"] == "ambas"
        ]
        ordenados = ordenar_por_distancia(filtrados, lat, lon)
        return ordenados[:2]

# ── Textos y teclados ─────────────────────────────────────────────────────────
def estado_parking(nombre):
    estado = reportes.get(nombre)
    if estado == "lleno":
        return "🔴 Reportado como completo"
    elif estado == "libre":
        return "🟢 Reportado como disponible"
    return "⚪ Sin reportes recientes"

def texto_parking(p, dist_km=None, indice=1):
    dist_txt = f"\n📍 Distancia: {dist_km:.1f} km" if dist_km else ""
    return (
        f"*{indice}ª opción — {p['nombre']}*\n"
        f"⏱️ Tiempo máximo: {p['tiempo_maximo']}\n"
        f"🚌 Plazas: {p['plazas']}\n"
        f"🕐 Horario: {p['horario']}\n"
        f"⚠️ Restricciones: {p['restricciones']}\n"
        f"📊 Estado: {estado_parking(p['nombre'])}"
        f"{dist_txt}"
    )

def teclado_parking(p, indice):
    gmaps = f"https://www.google.com/maps/dir/?api=1&destination={p['lat']},{p['lon']}&travelmode=driving"
    waze  = f"https://waze.com/ul?ll={p['lat']},{p['lon']}&navigate=yes"
    nombre_enc = p['nombre'].replace(" ", "_")
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🗺️ Maps ({indice})", url=gmaps),
            InlineKeyboardButton(f"🔵 Waze ({indice})",  url=waze),
        ],
        [
            InlineKeyboardButton("🔴 Completo",   callback_data=f"rep_lleno_{nombre_enc}"),
            InlineKeyboardButton("🟢 Disponible", callback_data=f"rep_libre_{nombre_enc}"),
        ],
    ])

# ── Menú principal ────────────────────────────────────────────────────────────
async def mostrar_menu(chat_id, context):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Enviar mi ubicación GPS",   callback_data="pedir_ubicacion")],
        [InlineKeyboardButton("🗺️ Elegir zona de la ciudad", callback_data="elegir_zona")],
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text="🚌 *DiscrePark — Parkings para autobuses*\n\n¿Cómo quieres buscar el aparcamiento?",
        parse_mode="Markdown",
        reply_markup=teclado
    )

async def mostrar_filtro_tipo(message, origen):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱️ Parada corta (≤15 min)",    callback_data=f"tipo_corta_{origen}")],
        [InlineKeyboardButton("🕐 Media estancia (hasta 2h)", callback_data=f"tipo_media_{origen}")],
        [InlineKeyboardButton("🅿️ Larga estancia",            callback_data=f"tipo_larga_{origen}")],
        [InlineKeyboardButton("🔍 Mostrar todos",             callback_data=f"tipo_todos_{origen}")],
        [InlineKeyboardButton("⬅️ Volver al menú",            callback_data="menu")],
    ])
    await message.reply_text(
        "¿Qué tipo de parking necesitas?",
        reply_markup=teclado
    )

# ── Enviar resultados ─────────────────────────────────────────────────────────
async def enviar_resultados(message, parkings_cercanos):
    reiniciar_reportes_si_toca()
    if not parkings_cercanos:
        await message.reply_text(
            "⚠️ No hay parkings disponibles para ese tipo en esta zona.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu")]
            ])
        )
        return
    for i, p in enumerate(parkings_cercanos, 1):
        estadisticas[p["nombre"]] += 1
        dist = p.get("_dist")
        await message.reply_text(
            texto_parking(p, dist, i),
            parse_mode="Markdown",
            reply_markup=teclado_parking(p, i)
        )
    await message.reply_text(
        "¿Necesitas buscar de nuevo?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu")]
        ])
    )

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mostrar_menu(update.effective_chat.id, context)

async def estadisticas_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ No tienes permiso para usar este comando.")
        return
    if not estadisticas:
        await update.message.reply_text("📊 Aún no hay consultas registradas hoy.")
        return
    total   = sum(estadisticas.values())
    ranking = sorted(estadis
