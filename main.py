import os
import time
import base64
import threading
import requests
from datetime import datetime, timezone
from flask import Flask, request
from anthropic import Anthropic

# Zona horaria de Chile (para la fecha en el registro de conversaciones)
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Santiago")
except Exception:
    TZ = timezone.utc

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

# Notion: base de datos donde se guardan las conversaciones
NOTION_DB_CONVERSACIONES = os.environ.get(
    "NOTION_DB_CONVERSACIONES", "bc17e2ba-92d9-40c3-ac37-16abeb09ae34"
)

# Groq: transcripción de audios (notas de voz)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
MODELO_AUDIO = "whisper-large-v3-turbo"

# Shopify: datos en vivo de productos (precio, stock, tallas, descripción)
SHOPIFY_STORE = os.environ.get("SHOPIFY_STORE", "")   # ej: soulcute.myshopify.com
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_TOKEN", "")   # Admin API token con read_products y read_inventory

# Telegram opcional: avisos cuando una conversación necesita humano
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MODELO = "claude-sonnet-4-6"

app = Flask(__name__)
cliente_ia = Anthropic(api_key=ANTHROPIC_API_KEY)

conversaciones = {}
MAX_HISTORIAL = 10

# Evita responder dos veces el mismo mensaje (WhatsApp a veces reenvía el webhook)
mensajes_vistos = set()

# Instrucción fija de seguridad: SIEMPRE aplica, aunque se edite la página de Notion.
INSTRUCCION_FIJA = (
    "Eres la asistente virtual de Soulcute (tienda chilena de moda femenina que moldea la figura), "
    "atendiendo clientas por WhatsApp. Responde SIEMPRE en español, con tono cálido y cercano, tuteando. "
    "Nunca hables de 'bajar de peso' (di 'moldear', 'estilizar', 'realzar la figura'). "
    "Nunca inventes datos que no estén en tu información; si no sabes algo, ofrece amablemente que una "
    "persona del equipo lo confirme. Sé concisa y natural para WhatsApp. "
    "Cuando la clienta pregunte por PRECIO, STOCK, tallas, colores o detalles de un producto, usa la "
    "herramienta 'consultar_producto' (con el handle del producto) ANTES de responder. Los datos en vivo de "
    "la tienda mandan sobre cualquier precio o detalle escrito en tu información. Nunca inventes precio ni "
    "disponibilidad. "
    "A continuación tienes toda tu información y reglas:\n\n"
)

# ─── HERRAMIENTAS QUE PUEDE USAR LA IA ──────────────────────────────────────
HERRAMIENTAS = [
    {
        "name": "consultar_producto",
        "description": (
            "Consulta datos REALES y en vivo de un producto en Shopify: precio, descripción, tallas, colores y "
            "stock. Úsala SIEMPRE que la clienta pregunte por el precio, si hay stock, si una talla/color está "
            "disponible, o por detalles del producto, ANTES de responder. Pasa el 'handle' del producto (la parte "
            "final del link en 'LINKS DIRECTOS DE PRODUCTOS', ej: para soulcute.cl/products/jeans-nova el handle es "
            "'jeans-nova'). Si no sabes el handle, usa 'busqueda' con el nombre del producto. Si la clienta indicó "
            "talla o color, pásalos también para filtrar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "handle":   {"type": "string", "description": "Handle del producto, ej: 'jeans-nova', 'body-figura-ideal'"},
                "busqueda": {"type": "string", "description": "Nombre del producto a buscar si no sabes el handle, ej: 'jeans nova'"},
                "talla":    {"type": "string", "description": "Talla consultada, ej '42', 'M', 'XL'. Opcional."},
                "color":    {"type": "string", "description": "Color consultado, ej 'Gris', 'Negro'. Opcional."},
            },
        },
    }
]

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
            if b.get("has_children") and t not in ("child_page", "child_database"):
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

# ─── GUARDAR CONVERSACIÓN EN NOTION (en segundo plano) ──────────────────────
def _guardar_conversacion(numero, quien, mensaje):
    if not NOTION_TOKEN or not NOTION_DB_CONVERSACIONES:
        return
    try:
        titulo = (mensaje or "(vacío)")[:1900]
        payload = {
            "parent": {"database_id": NOTION_DB_CONVERSACIONES},
            "properties": {
                "Mensaje": {"title": [{"text": {"content": titulo}}]},
                "Número": {"phone_number": numero},
                "Quién": {"select": {"name": quien}},
                "Fecha": {"date": {"start": datetime.now(TZ).isoformat()}},
            },
        }
        requests.post(
            "https://api.notion.com/v1/pages",
            headers={**NOTION_HEADERS, "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
    except Exception as e:
        print(f"Error guardando conversación en Notion: {e}")

def guardar_conversacion(numero, quien, mensaje):
    threading.Thread(target=_guardar_conversacion, args=(numero, quien, mensaje), daemon=True).start()

# ─── CONSULTAR PRODUCTO EN VIVO (Shopify) ───────────────────────────────────
def _fmt_clp(amount):
    try:
        n = int(round(float(amount)))
        return "$" + f"{n:,}".replace(",", ".")
    except Exception:
        return f"${amount}"

def consultar_producto(handle=None, busqueda=None, talla=None, color=None):
    if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
        return "No tengo acceso al catálogo en vivo ahora; deriva a una persona del equipo."
    if handle:
        h = handle.strip().lower().lstrip("/")
        if h.startswith("products/"):
            h = h.split("products/", 1)[1]
        q = f"handle:{h}"
    elif busqueda:
        q = busqueda.strip()
    else:
        return "No se indicó qué producto consultar."
    query = (
        "query($q:String!){ products(first:1, query:$q){ edges{ node{ "
        "title handle description onlineStoreUrl "
        "priceRangeV2{ minVariantPrice{ amount } maxVariantPrice{ amount } } "
        "variants(first:100){ edges{ node{ price inventoryQuantity availableForSale "
        "inventoryItem{ tracked } selectedOptions{ name value } } } } } } } }"
    )
    try:
        r = requests.post(
            f"https://{SHOPIFY_STORE}/admin/api/2024-01/graphql.json",
            headers={"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"},
            json={"query": query, "variables": {"q": q}},
            timeout=20,
        )
        data = r.json()
        edges = data.get("data", {}).get("products", {}).get("edges", [])
        if not edges:
            return "No encontré ese producto en la tienda."
        node = edges[0]["node"]
        titulo = node.get("title", "")
        url = node.get("onlineStoreUrl") or f"https://soulcute.cl/products/{node.get('handle','')}"
        desc = (node.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 500:
            desc = desc[:500] + "…"
        pr = node.get("priceRangeV2", {}) or {}
        mn = (pr.get("minVariantPrice") or {}).get("amount")
        mx = (pr.get("maxVariantPrice") or {}).get("amount")
        if mn and mx and float(mn) != float(mx):
            precio = f"{_fmt_clp(mn)} a {_fmt_clp(mx)}"
        else:
            precio = _fmt_clp(mn)
        disponibles, agotadas = [], []
        for ve in node.get("variants", {}).get("edges", []):
            v = ve["node"]
            opts = v.get("selectedOptions", []) or []
            etiqueta = " ".join(o["value"] for o in opts) if opts else "única"
            if talla and not any((talla or "").strip().lower() == o["value"].lower() for o in opts):
                continue
            if color and not any((color or "").strip().lower() == o["value"].lower() for o in opts):
                continue
            if v.get("availableForSale"):
                qn = v.get("inventoryQuantity")
                tracked = (v.get("inventoryItem") or {}).get("tracked", True)
                if tracked and isinstance(qn, int) and qn > 0:
                    disponibles.append(f"{etiqueta} (quedan {qn})")
                else:
                    disponibles.append(f"{etiqueta} (disponible)")
            else:
                agotadas.append(etiqueta)
        partes = [f"Producto: {titulo}.", f"Precio: {precio}.", f"Link: {url}."]
        if desc:
            partes.append(f"Descripción: {desc}")
        partes.append("Disponibles: " + (", ".join(disponibles) if disponibles else "ninguna que coincida") + ".")
        if agotadas:
            partes.append("Agotadas: " + ", ".join(agotadas) + ".")
        return " ".join(partes)
    except Exception as e:
        print(f"Error consultando producto: {e}")
        return "No pude consultar el producto ahora mismo; deriva a una persona del equipo."

def ejecutar_herramienta(nombre, args):
    if nombre == "consultar_producto":
        return consultar_producto(args.get("handle"), args.get("busqueda"), args.get("talla"), args.get("color"))
    return "Herramienta desconocida."

# ─── DESCARGAR ARCHIVO DE WHATSAPP (imágenes / audios) ──────────────────────
def descargar_media(media_id):
    try:
        r = requests.get(
            f"https://graph.facebook.com/v21.0/{media_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            timeout=20,
        )
        if r.status_code != 200:
            print(f"Error obteniendo URL de media: {r.status_code} - {r.text[:200]}")
            return None, None
        info = r.json()
        media_url = info.get("url")
        mime = info.get("mime_type", "")
        if not media_url:
            return None, None
        r2 = requests.get(
            media_url,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
            timeout=30,
        )
        if r2.status_code != 200:
            print(f"Error descargando media: {r2.status_code}")
            return None, None
        return r2.content, mime
    except Exception as e:
        print(f"Excepción descargando media: {e}")
        return None, None

# ─── TRANSCRIBIR AUDIO CON GROQ (Whisper) ───────────────────────────────────
def transcribir_audio(media_id):
    if not GROQ_API_KEY:
        print("No hay GROQ_API_KEY configurada; no se transcriben audios.")
        return None
    audio_bytes, mime = descargar_media(media_id)
    if not audio_bytes:
        return None
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("audio.ogg", audio_bytes, mime or "audio/ogg")},
            data={"model": MODELO_AUDIO, "language": "es", "response_format": "json"},
            timeout=60,
        )
        if r.status_code != 200:
            print(f"Error Groq: {r.status_code} - {r.text[:200]}")
            return None
        return (r.json().get("text") or "").strip()
    except Exception as e:
        print(f"Excepción transcribiendo audio: {e}")
        return None

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
def clasificar_caso(texto_cliente, texto_bot):
    t = f"{texto_cliente} {texto_bot}".lower()
    if any(p in t for p in ["descuento", "cupón", "cupon", "código", "codigo", "promoción", "promocion", "sc2610"]):
        return "🏷️ Descuento / cupón"
    if any(p in t for p in ["cambio", "devolución", "devolucion", "reclamo", "defecto", "falla", "fallado", "roto"]):
        return "🔄 Cambio / reclamo"
    if any(p in t for p in ["transferencia", "transferí", "transferi", "comprobante", "pagué", "pague", "depósito", "deposito"]):
        return "💸 Transferencia / pago"
    if any(p in t for p in ["mayorista", "por mayor", "al por mayor"]):
        return "📦 Mayorista"
    return "💬 Consulta general"

def _avisar_humano(numero, mensaje_clienta, categoria):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    texto = (
        f"🔔 <b>Atención humana</b>  ·  {categoria}\n\n"
        f"📱 Cliente: {numero}\n"
        f"💬 Mensaje: {mensaje_clienta}\n\n"
        f"Entra a responder cuando puedas."
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print(f"Error avisando a Telegram: {e}")

def avisar_humano(numero, mensaje_clienta, categoria="💬 Consulta general"):
    threading.Thread(target=_avisar_humano, args=(numero, mensaje_clienta, categoria), daemon=True).start()

# ─── GENERAR RESPUESTA CON IA (con uso de herramientas) ─────────────────────
def generar_respuesta(numero, contenido_api, texto_plano):
    """contenido_api: string (texto) o lista de bloques (para imágenes).
       texto_plano: versión en texto que se guarda en el historial y se usa para avisos."""
    historial = conversaciones.get(numero, [])
    mensajes = historial + [{"role": "user", "content": contenido_api}]
    try:
        system = INSTRUCCION_FIJA + obtener_cerebro()
        texto = ""
        for _ in range(4):  # permite hasta unas pocas consultas de herramientas
            respuesta = cliente_ia.messages.create(
                model=MODELO,
                max_tokens=600,
                system=system,
                messages=mensajes,
                tools=HERRAMIENTAS,
            )
            if respuesta.stop_reason == "tool_use":
                assistant_blocks, tool_results = [], []
                for b in respuesta.content:
                    if b.type == "text":
                        assistant_blocks.append({"type": "text", "text": b.text})
                    elif b.type == "tool_use":
                        assistant_blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                        resultado = ejecutar_herramienta(b.name, b.input)
                        tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": resultado})
                mensajes.append({"role": "assistant", "content": assistant_blocks})
                mensajes.append({"role": "user", "content": tool_results})
                continue
            texto = "".join(b.text for b in respuesta.content if b.type == "text").strip()
            break
        if not texto:
            texto = "¡Hola! 💕 Dame un segundito, una persona del equipo te ayuda con esto enseguida."
        historial.append({"role": "user", "content": texto_plano})
        historial.append({"role": "assistant", "content": texto})
        conversaciones[numero] = historial[-MAX_HISTORIAL:]
        if "una persona del equipo" in texto.lower() or "déjame confirmarte" in texto.lower():
            categoria = clasificar_caso(texto_plano, texto)
            avisar_humano(numero, texto_plano, categoria)
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
    data = request.get_json(silent=True) or {}
    try:
        value = data["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return "OK", 200

    # Candado de llamadas: si llega un evento de llamada, no se contesta.
    if "calls" in value:
        print("[info] Evento de llamada ignorado (el bot no contesta llamadas).")
        return "OK", 200

    # Ignorar estados de entrega y cualquier cosa que no sea un mensaje entrante.
    if "messages" not in value:
        return "OK", 200

    mensaje = value["messages"][0]
    numero = mensaje["from"]
    tipo = mensaje.get("type")

    # Evitar procesar dos veces el mismo mensaje (reintentos de WhatsApp)
    mid = mensaje.get("id")
    if mid:
        if mid in mensajes_vistos:
            return "OK", 200
        mensajes_vistos.add(mid)
        if len(mensajes_vistos) > 2000:
            mensajes_vistos.clear()

    try:
        if tipo == "text":
            texto_clienta = mensaje["text"]["body"]
            if texto_clienta.strip().lower() in ("reiniciar", "reset"):
                conversaciones.pop(numero, None)
                enviar_whatsapp(numero, "Listo, empezamos de nuevo 💕 ¿En qué te puedo ayudar?")
                return "OK", 200
            print(f"Mensaje de {numero}: {texto_clienta}")
            guardar_conversacion(numero, "Cliente", texto_clienta)
            respuesta = generar_respuesta(numero, texto_clienta, texto_clienta)
            enviar_whatsapp(numero, respuesta)
            guardar_conversacion(numero, "Bot", respuesta)

        elif tipo == "image":
            media_id = mensaje["image"]["id"]
            caption = (mensaje["image"].get("caption") or "").strip()
            guardar_conversacion(numero, "Cliente", "📷 Foto. " + caption)
            img_bytes, mime = descargar_media(media_id)
            if not img_bytes:
                enviar_whatsapp(numero, "Uy, no pude abrir bien tu foto 😅 ¿me la reenvías o me cuentas cómo es la prenda?")
                return "OK", 200
            if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                mime = "image/jpeg"
            b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
            instruccion = caption if caption else (
                "La clienta envió esta foto. Identifica el producto de Soulcute más parecido usando las fichas "
                "visuales de tu información y responde con calidez. Si lo reconoces, dale el link directo; si no es "
                "de Soulcute, ofrécele lo más parecido del catálogo."
            )
            contenido = [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": instruccion},
            ]
            texto_plano = "📷 La clienta envió una foto. " + caption
            respuesta = generar_respuesta(numero, contenido, texto_plano)
            enviar_whatsapp(numero, respuesta)
            guardar_conversacion(numero, "Bot", respuesta)

        elif tipo == "audio":
            media_id = mensaje["audio"]["id"]
            transcripcion = transcribir_audio(media_id)
            if not transcripcion:
                guardar_conversacion(numero, "Cliente", "🎙️ (audio no entendido)")
                enviar_whatsapp(numero, "Uy, no pude escuchar bien tu audio 😅 ¿me lo escribes porfa?")
                return "OK", 200
            print(f"Audio de {numero} transcrito: {transcripcion}")
            guardar_conversacion(numero, "Cliente", "🎙️ " + transcripcion)
            respuesta = generar_respuesta(numero, transcripcion, transcripcion)
            enviar_whatsapp(numero, respuesta)
            guardar_conversacion(numero, "Bot", respuesta)

        else:
            enviar_whatsapp(numero, "¡Hola! 💕 Cuéntame en qué te ayudo. Puedes escribirme, mandarme una foto 📷 o un audio 🎙️")
    except Exception as e:
        print(f"Error procesando mensaje: {e}")
    return "OK", 200

@app.route("/", methods=["GET"])
def inicio():
    return "Bot Soulcute WhatsApp activo ✅", 200

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=puerto)
