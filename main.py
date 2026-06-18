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

# ── Mapa de tiempo_maximo a categoría ────────────────────────────────────────
CATEGORIAS_TIEMPO = {
    "5 min":          "5min",
    "15 min":         "15min",
    "2 h":            "2h",
    "larga estancia": "larga",
}

def categoria_tiempo(tiempo_maximo):
    t = tiempo_maximo.strip().lower()
    for clave, cat in CATEGORIAS_TIEMPO.items():
        if clave == t:
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
                    "zona":          row.get("zona", "").strip(),
                    "ciudad":        row.get("ciudad", "").strip(),
                })
            except ValueError:
                continue
        logging.info(f"Parkings cargados: {len(parkings)}")
        return parkings
    except Exception as e:
        logging.error(f"Error cargando parkings: {e}")
        return []

def obtener_ciudades(parkings):
    ciudades = sorted(set(p["ciudad"] for p in parkings if p["ciudad"]))
    logging.info(f"Ciudades detectadas: {ciudades}")
    return ciudades

def obtener_zonas_de_ciudad(parkings, ciudad):
    zonas = sorted(set(
        p["zona"] for p in parkings
        if p["zona"] and p["ciudad"].lower() == ciudad.lower()
    ))
    logging.info(f"Zonas para {ciudad}: {zonas}")
    return zonas

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

def seleccionar_parkings_ciudad_zona(parkings, ciudad, zona, tipo_filtro):
    filtrados = [
        p for p in parkings
        if p["ciudad"].lower() == ciudad.lower() and p["zona"].lower() == zona.lower()
    ]
    if not filtrados:
        return []
    lat0 = sum(p["lat"] for p in filtrados) / len(filtrados)
    lon0 = sum(p["lon"] for p in filtrados) / len(filtrados)

    if tipo_filtro == "todos":
        ordenados = ordenar_por_distancia(filtrados, lat0, lon0)
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
        por_tipo = [
            p for p in filtrados
            if p["tipo"] == tipo_filtro or p["tipo"] == "ambas"
        ]
        return ordenar_por_distancia(por_tipo, lat0, lon0)[:2]

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
        [InlineKeyboardButton("📍 Enviar mi ubicación GPS", callback_data="pedir_ubicacion")],
        [InlineKeyboardButton("🏙️ Elegir ciudad",          callback_data="elegir_ciudad")],
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
    ranking = sorted(estadisticas.items(), key=lambda x: x[1], reverse=True)
    texto   = f"📊 *Estadísticas del día*\n\n🔢 Total consultas: {total}\n\n*Parkings más consultados:*\n"
    for i, (nombre, count) in enumerate(ranking, 1):
        texto += f"{i}. {nombre}: {count} consulta{'s' if count > 1 else ''}\n"
    await update.message.reply_text(texto, parse_mode="Markdown")

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data

    # ── Navegación principal ──
    if data == "menu":
        await mostrar_menu(q.message.chat_id, context)

    elif data == "pedir_ubicacion":
        context.user_data["esperando_ubicacion"] = True
        await q.message.reply_text(
            "📍 Pulsa el clip 📎 → *Ubicación* → *Enviar mi ubicación actual*.",
            parse_mode="Markdown"
        )

    # ── Paso 1: elegir ciudad ──
    elif data == "elegir_ciudad":
        parkings = cargar_parkings()
        ciudades = obtener_ciudades(parkings)
        if not ciudades:
            await q.message.reply_text(
                "⚠️ No hay ciudades definidas en la base de datos.\n\n"
                "Comprueba que la columna 'ciudad' en Sheets tiene valores rellenos."
            )
            return
        botones = [[InlineKeyboardButton(c, callback_data=f"ciudad_{c}")] for c in ciudades]
        botones.append([InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu")])
        await q.message.reply_text("Elige una ciudad:", reply_markup=InlineKeyboardMarkup(botones))

    # ── Paso 2: elegir zona dentro de la ciudad ──
    elif data.startswith("ciudad_"):
        ciudad   = data[len("ciudad_"):]
        parkings = cargar_parkings()
        zonas    = obtener_zonas_de_ciudad(parkings, ciudad)
        if not zonas:
            await q.message.reply_text(f"⚠️ No hay zonas definidas para {ciudad}.")
            return
        botones = [
            [InlineKeyboardButton(z, callback_data=f"zonacity_{ciudad}::{z}")] for z in zonas
        ]
        botones.append([InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu")])
        await q.message.reply_text(
            f"Elige una zona en *{ciudad}*:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(botones)
        )

    # ── Paso 3: zona elegida → pide tipo ──
    elif data.startswith("zonacity_"):
        resto         = data[len("zonacity_"):]
        ciudad, zona  = resto.split("::", 1)
        await mostrar_filtro_tipo(q.message, f"zonacity_{ciudad}::{zona}")

    # ── Paso 4: tipo elegido → resultados ──
    elif data.startswith("tipo_"):
        partes   = data.split("_", 2)
        tipo     = partes[1]
        origen   = partes[2] if len(partes) > 2 else "todos"
        parkings = cargar_parkings()
        reiniciar_reportes_si_toca()

        if origen == "gps":
            coords = context.user_data.get("ultima_ubicacion")
            if not coords:
                await q.message.reply_text("⚠️ No tengo tu ubicación. Envíala de nuevo.")
                return
            lat, lon = coords
            cercanos = seleccionar_parkings(parkings, lat, lon, tipo)
            for p in cercanos:
                p["_dist"] = distancia_km(lat, lon, p["lat"], p["lon"])

        elif origen.startswith("zonacity_"):
            resto        = origen[len("zonacity_"):]
            ciudad, zona = resto.split("::", 1)
            cercanos     = seleccionar_parkings_ciudad_zona(parkings, ciudad, zona, tipo)

        else:
            await q.message.reply_text("⚠️ Origen no reconocido. Vuelve al menú.")
            return

        tipo_txt = {
            "corta": "parada corta — 2 más cercanos",
            "media": "media estancia — 2 más cercanos",
            "larga": "larga estancia — 2 más cercanos",
            "todos": "uno de cada tiempo máximo",
        }.get(tipo, tipo)

        await q.message.reply_text(
            f"🅿️ *Opciones encontradas — {tipo_txt}:*",
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
            "lleno": "🔴 Gracias. Se ha reportado como completo.",
            "libre": "🟢 Gracias. Se ha reportado como disponible.",
        }.get(estado, "Reporte registrado.")
        await q.message.reply_text(estado_txt)
        conductor = q.from_user.first_name or "Un conductor"
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"📢 *Reporte recibido*\n\n"
                f"👤 Conductor: {conductor}\n"
                f"🅿️ Parking: {nombre}\n"
                f"📊 Estado: {'completo' if estado == 'lleno' else 'disponible'}"
            ),
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
