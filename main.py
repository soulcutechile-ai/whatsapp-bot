import os
import json
import requests
from flask import Flask, request
from anthropic import Anthropic
from base_conocimiento import BASE_CONOCIMIENTO, INSTRUCCIONES_SISTEMA

# ─── CONFIGURACIÓN (variables de entorno en Railway) ──────────────
WHATSAPP_TOKEN      = os.environ["WHATSAPP_TOKEN"]
WHATSAPP_PHONE_ID   = os.environ["WHATSAPP_PHONE_ID"]
VERIFY_TOKEN        = os.environ["VERIFY_TOKEN"]        # lo inventas tú, ej: "soulcute2026"
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]

# Telegram opcional: para avisarte cuando una conversación necesita humano
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")

MODELO = "claude-sonnet-4-6"  # cerebro del bot (cambiable en 1 línea)

app = Flask(__name__)
cliente_ia = Anthropic(api_key=ANTHROPIC_API_KEY)

# Memoria simple de conversaciones (por número de teléfono)
# Guarda el historial reciente para mantener contexto en la charla
conversaciones = {}
MAX_HISTORIAL = 10  # cuántos mensajes recordar por clienta


# ─── ENVIAR MENSAJE DE WHATSAPP ───────────────────────────────────
def enviar_whatsapp(numero, texto):
    url = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": texto}
    }
    r = requests.post(url, headers=headers, json=payload)
    if r.status_code != 200:
        print(f"Error enviando WhatsApp: {r.status_code} - {r.text}")
    return r


# ─── AVISAR A TELEGRAM (cuando se necesita humano) ────────────────
def avisar_humano(numero, mensaje_clienta):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    texto = (
        f"🔔 <b>Conversación necesita atención humana</b>\n\n"
        f"📱 Cliente: {numero}\n"
        f"💬 Mensaje: {mensaje_clienta}\n\n"
        f"Entra a responder cuando puedas."
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML"})


# ─── GENERAR RESPUESTA CON IA ─────────────────────────────────────
def generar_respuesta(numero, mensaje):
    # Recuperar historial de esta clienta
    historial = conversaciones.get(numero, [])
    historial.append({"role": "user", "content": mensaje})

    try:
        respuesta = cliente_ia.messages.create(
            model=MODELO,
            max_tokens=500,
            system=INSTRUCCIONES_SISTEMA + "\n\n" + BASE_CONOCIMIENTO,
            messages=historial
        )
        texto = respuesta.content[0].text

        # Guardar la respuesta en el historial
        historial.append({"role": "assistant", "content": texto})
        # Limitar tamaño del historial
        conversaciones[numero] = historial[-MAX_HISTORIAL:]

        # Si la IA indica que deriva a humano, avisar por Telegram
        if "una persona del equipo" in texto.lower() or "déjame confirmarte" in texto.lower():
            avisar_humano(numero, mensaje)

        return texto

    except Exception as e:
        print(f"Error con la IA: {e}")
        return "¡Hola! 💕 En este momento tuve un problemita técnico. Una persona del equipo te responderá en breve."


# ─── WEBHOOK: VERIFICACIÓN (Meta lo pide al conectar) ─────────────
@app.route("/webhook", methods=["GET"])
def verificar():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verificación fallida", 403


# ─── WEBHOOK: RECIBIR MENSAJES ────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def recibir():
    data = request.get_json()
    try:
        entry = data["entry"][0]
        cambios = entry["changes"][0]["value"]

        # Solo procesar si hay mensajes (ignorar notificaciones de estado)
        if "messages" in cambios:
            mensaje = cambios["messages"][0]
            numero = mensaje["from"]

            # Solo procesar mensajes de texto
            if mensaje["type"] == "text":
                texto_clienta = mensaje["text"]["body"]
                print(f"Mensaje de {numero}: {texto_clienta}")

                # Generar y enviar respuesta
                respuesta = generar_respuesta(numero, texto_clienta)
                enviar_whatsapp(numero, respuesta)
            else:
                # Si manda audio, imagen, etc.
                enviar_whatsapp(numero, "¡Hola! 💕 Por ahora solo puedo leer mensajes de texto. ¿En qué te ayudo?")

    except Exception as e:
        print(f"Error procesando mensaje: {e}")

    return "OK", 200


@app.route("/", methods=["GET"])
def inicio():
    return "Bot Soulcute WhatsApp activo ✅", 200


if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=puerto)
