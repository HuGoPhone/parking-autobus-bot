import os
import math
import logging
import requests
import csv
import io
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)

TOKEN     = os.environ["BOT_TOKEN"]
SHEET_ID  = os.environ["SHEET_ID"]
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"

ZONAS = ["Centro", "Norte", "Sur", "Este", "Oeste"]

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
                })
            except ValueError:
                continue
        return parkings
    except Exception as e:
        logging.error(f"Error cargando parkings: {e}")
        return []

def distancia_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) *
         math.cos(math.radians(lat2)) *
         math.sin(dlon/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def dos_mas_cercanos(parkings, lat, lon):
    ordenados = sorted(parkings, key=lambda p: distancia_km(lat, lon, p["lat"], p["lon"]))
    return ordenados[:2]

def construir_teclado_parking(lat, lon, indice):
    gmaps = f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}&travelmode=driving"
    waze  = f"https://waze.com/ul?ll={lat},{lon}&navigate=yes"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🗺️ Maps ({indice})", url=gmaps),
            InlineKeyboardButton(f"🔵 Waze ({indice})",  url=waze),
        ],
    ])

def texto_parking(p, dist_km=None, indice=1):
    dist_txt = f"\n📍 Distancia: {dist_km:.1f} km" if dist_km else ""
    return (
        f"*{indice}º opción — {p['nombre']}*\n"
        f"⏱️ Tiempo máximo: {p['tiempo_maximo']}\n"
        f"🚌 Plazas: {p['plazas']}\n"
        f"🕐 Horario: {p['horario']}\n"
        f"⚠️ Restricciones: {p['restricciones']}"
        f"{dist_txt}"
    )

async def mostrar_menu(chat_id, context):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Enviar mi ubicación GPS",   callback_data="pedir_ubicacion")],
        [InlineKeyboardButton("🗺️ Elegir zona de la ciudad", callback_data="elegir_zona")],
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🚌 *DiscrePark — Parkings para autobuses*\n\n"
            "¿Cómo quieres buscar el aparcamiento más cercano?"
        ),
        parse_mode="Markdown",
        reply_markup=teclado
    )

async def enviar_dos_parkings(message, parkings_cercanos):
    for i, p in enumerate(parkings_cercanos, 1):
        dist = p.get("_dist")
        await message.reply_text(
            texto_parking(p, dist, i),
            parse_mode="Markdown",
            reply_markup=construir_teclado_parking(p["lat"], p["lon"], i)
        )
    await message.reply_text(
        "¿Necesitas buscar de nuevo?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu")]
        ])
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await mostrar_menu(update.effective_chat.id, context)

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "pedir_ubicacion":
        await q.message.reply_text(
            "📍 Pulsa el clip 📎 → *Ubicación* → *Enviar mi ubicación actual*.",
            parse_mode="Markdown"
        )

    elif data == "elegir_zona":
        botones = [
            [InlineKeyboardButton(z, callback_data=f"zona_{z}")] for z in ZONAS
        ]
        botones.append([InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu")])
        await q.message.reply_text(
            "Elige una zona:",
            reply_markup=InlineKeyboardMarkup(botones)
        )

    elif data == "menu":
        await mostrar_menu(q.message.chat_id, context)

    elif data.startswith("zona_"):
        zona = data[5:]
        parkings = cargar_parkings()
        if not parkings:
            await q.message.reply_text("⚠️ No pude cargar los parkings. Inténtalo de nuevo.")
            return
        centro_zonas = {
            "Centro": (40.4168, -3.7038),
            "Norte":  (40.4800, -3.6900),
            "Sur":    (40.3700, -3.7000),
            "Este":   (40.4300, -3.6200),
            "Oeste":  (40.4300, -3.7600),
        }
        lat0, lon0 = centro_zonas.get(zona, (40.4168, -3.7038))
        cercanos = dos_mas_cercanos(parkings, lat0, lon0)
        await q.message.reply_text(
            f"🅿️ Las 2 mejores opciones en zona *{zona}*:",
            parse_mode="Markdown"
        )
        await enviar_dos_parkings(q.message, cercanos)

async def ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    parkings = cargar_parkings()
    if not parkings:
        await update.message.reply_text("⚠️ No pude cargar los parkings. Inténtalo de nuevo.")
        return
    cercanos = dos_mas_cercanos(parkings, loc.latitude, loc.longitude)
    for p in cercanos:
        p["_dist"] = distancia_km(loc.latitude, loc.longitude, p["lat"], p["lon"])
    await update.message.reply_text("🅿️ *Los 2 parkings más cercanos a tu posición:*", parse_mode="Markdown")
    await enviar_dos_parkings(update.message, cercanos)

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Buscar parking")
    ])

if __name__ == "__main__":
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.LOCATION, ubicacion))
    app.run_polling()
