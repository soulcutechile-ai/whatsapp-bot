import os
import time
import requests
from flask import Flask, request
from anthropic import Anthropic

# ─── FALLBACK LOCAL (por si Notion falla, el bot sigue funcionando) ──────────
try:
    from base_conocimiento import BASE_CONOCIMIENTO, INSTRUCCIONES_SISTEMA
    FALLBACK_CEREBRO = INSTRUCCIONES_SISTEMA + "\n\n" + BASE_CONOCIMIENTO
except Exception:
    FALLBACK_CEREBRO = "Eres la asistente virtual de Soulcute. Atiende con calidez. Si no sabes algo, deriva a una persona del equipo."

# ─── CONFIGURACIÓN (variables de entorno en Railway) ────────────────────────
WHATSAPP_TOKEN    = os.environ["WHATSAPP_TOKEN"]
WHATSAPP_PHONE_ID = os.environ["WHATSAPP_PHONE_ID"]
VERIFY_TOKEN      = os.environ["VERIFY_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Notion: el "cerebro" editable del bot
NOTION_TOKEN   = os.environ.get("NOTION_TOKEN", "")
NOTION_PAGE_ID = os.environ.get("NOTION_PAGE_ID", "")

# Telegram opcional: avisos cuando una conversación necesita humano
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MODELO = "claude-sonnet-4-6"

app = Flask(__name__)
cliente_ia = Anthropic(api_key=ANTHROPIC_API_KEY)

conversaciones = {}
MAX_HISTORIAL = 10

# Instrucción fija de seguridad: SIEMPRE aplica, aunque se edite la página de Notion.
INSTRUCCION_FIJA = (
    "Eres la asistente virtual de Soulcute (tienda chilena de moda femenina que moldea la figura), "
    "atendiendo clientas por WhatsApp. Responde SIEMPRE en español, con tono cálido y cercano, tuteando. "
    "Nunca hables de 'bajar de peso' (di 'moldear', 'estilizar', 'realzar la figura'). "
    "Nunca inventes datos que no estén en tu información; si no sabes algo, ofrece amablemente que una "
    "persona del equipo lo confirme. Sé concisa y natural para WhatsApp. "
    "A continuación tienes toda tu información y reglas:\n\n"
)

# ─── CEREBRO DESDE NOTION (con caché para velocidad) ────────────────────────
_cache = {"texto": None, "momento": 0}
CACHE_SEGUNDOS = 120  # relee Notion cada 2 minutos como máximo

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
}

def _rich(arr):
    return "".join(x.get("plain_text", "") for x in (arr or []))

def _leer_bloques(page_id, prof=0):
    if prof > 3:
        return ""
    lineas, cursor = [], None
    while True:
        url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        r = requests.get(url, headers=NOTION_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"Notion error {r.status_code}: {r.text[:200]}")
            return ""
        data = r.json()
        for b in data.get("results", []):
            t = b.get("type", "")
            obj = b.get(t, {})
            if t in ("heading_1", "heading_2", "heading_3"):
                lineas.append("\n" + _rich(obj.get("rich_text")))
            elif t in ("paragraph", "quote", "callout", "toggle", "code"):
                lineas.append(_rich(obj.get("rich_text")))
            elif t in ("bulleted_list_item", "numbered_list_item", "to_do"):
                lineas.append("- " + _rich(obj.get("rich_text")))
            elif t == "table_row":
                lineas.append(" | ".join(_rich(c) for c in obj.get("cells", [])))
            if b.get("has_children") and t != "child_page":
                hijo = _leer_bloques(b["id"], prof + 1)
                if hijo:
                    lineas.append(hijo)
        if data.get("has_more"):
            cursor = data.get("next_cursor")
        else:
            break
    return "\n".join(l for l in lineas if l)

def obtener_cerebro():
    if not NOTION_TOKEN or not NOTION_PAGE_ID:
        return FALLBACK_CEREBRO
    ahora = time.time()
    if _cache["texto"] and (ahora - _cache["momento"] < CACHE_SEGUNDOS):
        return _cache["texto"]
    try:
        texto = _leer_bloques(NOTION_PAGE_ID)
        if texto and len(texto) > 50:
            _cache["texto"] = texto
            _cache["momento"] = ahora
            return texto
        return _cache["texto"] or FALLBACK_CEREBRO
    except Exception as e:
        print(f"Error leyendo Notion: {e}")
        return _cache["texto"] or FALLBACK_CEREBRO

# ─── ENVIAR MENSAJE DE WHATSAPP ─────────────────────────────────────────────
def enviar_whatsapp(numero, texto):
    url = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": numero, "type": "text", "text": {"body": texto}}
    r = requests.post(url, headers=headers, json=payload)
    if r.status_code != 200:
        print(f"Error enviando WhatsApp: {r.status_code} - {r.text}")
    return r

# ─── AVISAR A TELEGRAM (cuando se necesita humano) ──────────────────────────
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

# ─── GENERAR RESPUESTA CON IA ───────────────────────────────────────────────
def generar_respuesta(numero, mensaje):
    historial = conversaciones.get(numero, [])
    historial.append({"role": "user", "content": mensaje})
    try:
        system = INSTRUCCION_FIJA + obtener_cerebro()
        respuesta = cliente_ia.messages.create(
            model=MODELO,
            max_tokens=500,
            system=system,
            messages=historial,
        )
        texto = respuesta.content[0].text
        historial.append({"role": "assistant", "content": texto})
        conversaciones[numero] = historial[-MAX_HISTORIAL:]
        if "una persona del equipo" in texto.lower() or "déjame confirmarte" in texto.lower():
            avisar_humano(numero, mensaje)
        return texto
    except Exception as e:
        print(f"Error con la IA: {e}")
        return "¡Hola! 💕 En este momento tuve un problemita técnico. Una persona del equipo te responderá en breve."

# ─── WEBHOOK: VERIFICACIÓN ──────────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verificar():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verificación fallida", 403

# ─── WEBHOOK: RECIBIR MENSAJES ──────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def recibir():
    data = request.get_json()
    try:
        cambios = data["entry"][0]["changes"][0]["value"]
        if "messages" in cambios:
            mensaje = cambios["messages"][0]
            numero = mensaje["from"]
            if mensaje["type"] == "text":
                texto_clienta = mensaje["text"]["body"]
                print(f"Mensaje de {numero}: {texto_clienta}")
                respuesta = generar_respuesta(numero, texto_clienta)
                enviar_whatsapp(numero, respuesta)
            else:
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
