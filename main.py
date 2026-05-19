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

# ── Estado en memoria ─────────────────────────────────────────────────────────
reportes   = {}   # {nombre_parking: "lleno" | "libre" | "cerrado"}
estadisticas = defaultdict(int)   # {nombre_parking: nº consultas}

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

def dos_mas_cercanos(parkings, lat, lon, tipo_filtro=None):
    if tipo_filtro and tipo_filtro != "todos":
        parkings = [
            p for p in parkings
            if p["tipo"] == tipo_filtro or p["tipo"] == "ambas"
        ]
    ordenados = sorted(parkings, key=lambda p: distancia_km(lat, lon, p["lat"], p["lon"]))
    return ordenados[:2]

# ── Textos y teclados ─────────────────────────────────────────────────────────
def estado_parking(nombre):
    estado = reportes.get(nombre)
    if estado == "lleno":
        return "🔴 Reportado como lleno"
    elif estado == "cerrado":
        return "⛔ Reportado como cerrado"
    elif estado == "libre":
        return "🟢 Reportado con plazas libres"
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
            InlineKeyboardButton("🔴 Lleno",   callback_data=f"rep_lleno_{nombre_enc}"),
            InlineKeyboardButton("🟢 Hay plazas", callback_data=f"rep_libre_{nombre_enc}"),
            InlineKeyboardButton("⛔ Cerrado", callback_data=f"rep_cerrado_{nombre_enc}"),
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

async def mostrar_filtro_tipo(message, origen, extra=""):
    """origen puede ser 'gps' o una zona como 'Centro'"""
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱️ Parada corta (≤15 min)",    callback_data=f"tipo_corta_{origen}{extra}")],
        [InlineKeyboardButton("🕐 Media estancia (hasta 2h)", callback_data=f"tipo_media_{origen}{extra}")],
        [InlineKeyboardButton("🅿️ Larga estancia",            callback_data=f"tipo_larga_{origen}{extra}")],
        [InlineKeyboardButton("🔍 Mostrar todos",             callback_data=f"tipo_todos_{origen}{extra}")],
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
    total = sum(estadisticas.values())
    ranking = sorted(estadisticas.items(), key=lambda x: x[1], reverse=True)
    texto = f"📊 *Estadísticas del día*\n\n🔢 Total consultas: {total}\n\n*Parkings más consultados:*\n"
    for i, (nombre, count) in enumerate(ranking, 1):
        texto += f"{i}. {nombre}: {count} consulta{'s' if count > 1 else ''}\n"
    await update.message.reply_text(texto, parse_mode="Markdown")

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    # ── Menú y navegación ──
    if data == "menu":
        await mostrar_menu(q.message.chat_id, context)

    elif data == "pedir_ubicacion":
        context.user_data["esperando_ubicacion"] = True
        await q.message.reply_text(
            "📍 Pulsa el clip 📎 → *Ubicación* → *Enviar mi ubicación actual*.",
            parse_mode="Markdown"
        )

    elif data == "elegir_zona":
        botones = [[InlineKeyboardButton(z, callback_data=f"zona_{z}")] for z in ZONAS]
        botones.append([InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu")])
        await q.message.reply_text("Elige una zona:", reply_markup=InlineKeyboardMarkup(botones))

    # ── Selección de zona → pide tipo ──
    elif data.startswith("zona_"):
        zona = data[5:]
        context.user_data["zona_seleccionada"] = zona
        await mostrar_filtro_tipo(q.message, zona)

    # ── Selección de tipo ──
    elif data.startswith("tipo_"):
        partes    = data.split("_", 2)
        tipo      = partes[1]
        origen    = partes[2] if len(partes) > 2 else "todos"
        parkings  = cargar_parkings()
        reiniciar_reportes_si_toca()

        if origen == "gps":
            coords = context.user_data.get("ultima_ubicacion")
            if not coords:
                await q.message.reply_text("⚠️ No tengo tu ubicación. Envíala de nuevo.")
                return
            lat, lon = coords
            cercanos = dos_mas_cercanos(parkings, lat, lon, tipo)
            for p in cercanos:
                p["_dist"] = distancia_km(lat, lon, p["lat"], p["lon"])
        else:
            zona = origen
            centro_zonas = {
                "Centro": (40.4168, -3.7038),
                "Norte":  (40.4800, -3.6900),
                "Sur":    (40.3700, -3.7000),
                "Este":   (40.4300, -3.6200),
                "Oeste":  (40.4300, -3.7600),
            }
            lat0, lon0 = centro_zonas.get(zona, (40.4168, -3.7038))
            cercanos = dos_mas_cercanos(parkings, lat0, lon0, tipo)

        tipo_txt = {
            "corta": "parada corta",
            "media": "media estancia",
            "larga": "larga estancia",
            "todos": "todos los tipos"
        }.get(tipo, tipo)

        await q.message.reply_text(
            f"🅿️ *Las 2 mejores opciones — {tipo_txt}:*",
            parse_mode="Markdown"
        )
        await enviar_resultados(q.message, cercanos)

    # ── Reportes ──
    elif data.startswith("rep_"):
        partes  = data.split("_", 2)
        estado  = partes[1]
        nombre  = partes[2].replace("_", " ")
        reportes[nombre] = estado
        estado_txt = {
            "lleno":   "🔴 Gracias. Se ha reportado como lleno.",
            "libre":   "🟢 Gracias. Se ha reportado con plazas libres.",
            "cerrado": "⛔ Gracias. Se ha reportado como cerrado.",
        }.get(estado, "Reporte registrado.")
        await q.message.reply_text(estado_txt)
        if context.bot_data.get("admin_id"):
            conductor = q.from_user.first_name or "Un conductor"
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"📢 *Reporte recibido*\n\n{conductor} ha reportado *{nombre}* como *{estado}*.",
                parse_mode="Markdown"
            )

async def ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    context.user_data["ultima_ubicacion"] = (loc.latitude, loc.longitude)
    await mostrar_filtro_tipo(update.message, "gps")

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start",        "Buscar parking"),
        BotCommand("estadisticas", "Ver estadísticas del día (solo admin)"),
    ])

if __name__ == "__main__":
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",        start))
    app.add_handler(CommandHandler("estadisticas", estadisticas_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.LOCATION, ubicacion))
    app.run_polling()
