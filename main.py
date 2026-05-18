import os
import math
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)

TOKEN      = os.environ["BOT_TOKEN"]
SHEET_ID   = os.environ["SHEET_ID"]
SHEET_URL  = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"

ZONAS = ["Centro", "Norte", "Sur", "Este", "Oeste"]

def cargar_parkings():
    r = requests.get(SHEET_URL, timeout=10)
    r.encoding = "utf-8"
    lines = r.text.strip().splitlines()
    parkings = []
    for line in lines[1:]:
        line = line.strip().strip('"')
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 5:
            try:
                parkings.append({
                    "nombre": parts[0],
                    "lat":    float(parts[1]),
                    "lon":    float(parts[2]),
                    "tipo":   parts[3],
                    "zona":   parts[4],
                })
            except ValueError:
                pass
    return parkings

def distancia_km(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def mas_cercano(parkings, lat, lon):
    return min(parkings, key=lambda p: distancia_km(lat, lon, p["lat"], p["lon"]))

def mensaje_parking(p, dist_km=None):
    tipo_txt = "Parada corta (≤15 min)" if "corta" in p["tipo"] else "Larga estancia"
    dist_txt = f"\n📍 Distancia: {dist_km:.1f} km" if dist_km else ""
    gmaps = f"https://www.google.com/maps/dir/?api=1&destination={p['lat']},{p['lon']}&travelmode=driving"
    waze  = f"https://waze.com/ul?ll={p['lat']},{p['lon']}&navigate=yes"
    texto = (
        f"🅿️ *{p['nombre']}*\n"
        f"🚌 Tipo: {tipo_txt}\n"
        f"🗺️ Zona: {p['zona']}"
        f"{dist_txt}"
    )
    teclado = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗺️ Google Maps", url=gmaps),
        InlineKeyboardButton("🔵 Waze",        url=waze),
    ]])
    return texto, teclado

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Enviar mi ubicación GPS", callback_data="pedir_ubicacion")],
        [InlineKeyboardButton("🗺️ Elegir zona de la ciudad", callback_data="elegir_zona")],
    ])
    await update.message.reply_text(
        "👋 Hola. Soy el asistente de parkings para autobuses discrecionales.\n\n"
        "¿Cómo quieres buscar el aparcamiento más cercano?",
        reply_markup=teclado
    )

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "pedir_ubicacion":
        await q.message.reply_text(
            "Pulsa el clip 📎 en Telegram → *Ubicación* → *Enviar mi ubicación actual*.",
            parse_mode="Markdown"
        )
    elif data == "elegir_zona":
        botones = [[InlineKeyboardButton(z, callback_data=f"zona_{z}")] for z in ZONAS]
        await q.message.reply_text("Elige una zona:", reply_markup=InlineKeyboardMarkup(botones))
    elif data.startswith("zona_"):
        zona = data[5:]
        parkings = cargar_parkings()
        filtrados = [p for p in parkings if p["zona"].lower() == zona.lower()]
        if not filtrados:
            await q.message.reply_text(f"No tengo parkings registrados en la zona {zona}.")
            return
        centro_zonas = {
            "Centro": (40.4168, -3.7038),
            "Norte":  (40.4800, -3.6900),
            "Sur":    (40.3700, -3.7000),
            "Este":   (40.4300, -3.6200),
            "Oeste":  (40.4300, -3.7600),
        }
        lat0, lon0 = centro_zonas.get(zona, (40.4168, -3.7038))
        p = mas_cercano(filtrados, lat0, lon0)
        texto, teclado = mensaje_parking(p)
        await q.message.reply_text(
            f"🅿️ Mejor opción en zona *{zona}*:\n\n{texto}",
            parse_mode="Markdown", reply_markup=teclado
        )

async def ubicacion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    parkings = cargar_parkings()
    if not parkings:
        await update.message.reply_text("No pude cargar los parkings. Inténtalo de nuevo.")
        return
    p = mas_cercano(parkings, loc.latitude, loc.longitude)
    dist = distancia_km(loc.latitude, loc.longitude, p["lat"], p["lon"])
    texto, teclado = mensaje_parking(p, dist)
    await update.message.reply_text(
        f"Parking más cercano a tu posición:\n\n{texto}",
        parse_mode="Markdown", reply_markup=teclado
    )

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.LOCATION, ubicacion))
    app.run_polling()
