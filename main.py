import os
import time
import base64
import threading
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, Response
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

# Panel de administración
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "soulcute2024")
NUMEROS_EN_HUMANO = set()  # números donde el bot está pausado, humano atiende

# Groq: transcripción de audios (notas de voz)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
MODELO_AUDIO = "whisper-large-v3-turbo"

# Shopify: credenciales para autenticación automática (Client Credentials Grant)
SHOPIFY_STORE         = os.environ.get("SHOPIFY_STORE", "")
SHOPIFY_CLIENT_ID     = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")

# Telegram opcional: avisos cuando una conversación necesita humano
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MODELO = "claude-sonnet-4-6"

app = Flask(__name__)
cliente_ia = Anthropic(api_key=ANTHROPIC_API_KEY)

conversaciones = {}
MAX_HISTORIAL = 6

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
    "herramienta 'consultar_producto' ANTES de responder. "
    "REGLA CRÍTICA: el parámetro 'handle' es SOLO el identificador base del producto, SIN color ni talla. "
    "Ejemplos correctos: handle='body-figura-ideal', handle='jeans-nova', handle='body-diosa-fit-copia'. "
    "Si la clienta pide un color o talla específico, pásalos en los parámetros 'color' y 'talla', NUNCA los agregues al handle. "
    "INCORRECTO: handle='body-figura-ideal-negro'. CORRECTO: handle='body-figura-ideal', color='Negro'. "
    "Los handles exactos están en la sección LINKS DIRECTOS DE PRODUCTOS de tu información. "
    "Los datos en vivo de Shopify mandan sobre cualquier precio o detalle escrito en tu información. "
    "Nunca inventes precio ni disponibilidad. "
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
    },
    {
        "name": "consultar_pedido",
        "description": (
            "Consulta el estado REAL de un pedido en Shopify: estado de pago, fulfillment, y número de seguimiento. "
            "Úsala cuando la clienta pregunte '¿dónde está mi pedido?', '¿llegó?', '¿cuándo llega?', "
            "'¿tienen mi número de seguimiento?', o similar. Pasa el número de pedido (ej: '1042') o el email/teléfono "
            "de la clienta para buscarlo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero_pedido": {"type": "string", "description": "Número de pedido, ej: '1042' o '#1042'. Opcional si tienes email."},
                "email":        {"type": "string", "description": "Email de la clienta para buscar su pedido. Opcional si tienes número."},
            },
        },
    },
    {
        "name": "validar_descuento",
        "description": (
            "Verifica si un código de descuento existe en Shopify, si está activo, y sus condiciones (mínimo de compra, "
            "fecha de vencimiento, usos restantes). Úsala cuando una clienta diga que un código no le funciona, ANTES "
            "de explicarle posibles causas. Pasa el código exacto que la clienta está intentando usar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "codigo": {"type": "string", "description": "El código de descuento exacto que la clienta está usando, ej: 'SC2610'"},
            },
            "required": ["codigo"],
        },
    },
    {
        "name": "verificar_cliente",
        "description": (
            "Verifica si una persona ya es cliente de Soulcute (si ha comprado antes) buscando por email o teléfono. "
            "Úsala cuando necesites saber si aplica el descuento de primera compra (SC2610), ya que ese código "
            "solo es válido para quien nunca ha comprado. Pasa el email o teléfono de la clienta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email":    {"type": "string", "description": "Email de la clienta. Opcional si tienes teléfono."},
                "telefono": {"type": "string", "description": "Teléfono de la clienta (con o sin código de país). Opcional si tienes email."},
            },
        },
    },
]

# ─── CEREBRO DESDE NOTION + FILES API DE ANTHROPIC ──────────────────────────
_cache = {"texto": None, "file_id": None, "momento": 0}
CACHE_SEGUNDOS = 86400  # relee Notion y actualiza el archivo cada 24h

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
}
FILES_API_HEADERS = {
    "x-api-key": ANTHROPIC_API_KEY,
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "files-api-2025-04-14",
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

def _subir_cerebro_files_api(texto):
    """Sube el cerebro como archivo a Anthropic Files API. Devuelve el file_id."""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/files",
            headers=FILES_API_HEADERS,
            files={"file": ("cerebro_soulcute.txt", texto.encode("utf-8"), "text/plain")},
            timeout=30,
        )
        if r.status_code == 200:
            file_id = r.json().get("id")
            print(f"[FILES API] Cerebro subido → {file_id}")
            return file_id
        print(f"[FILES API] Error subiendo: {r.status_code} {r.text[:200]}")
        return None
    except Exception as e:
        print(f"[FILES API] Excepción subiendo: {e}")
        return None

def _eliminar_archivo_files_api(file_id):
    """Elimina un archivo anterior de Anthropic Files API."""
    try:
        r = requests.delete(
            f"https://api.anthropic.com/v1/files/{file_id}",
            headers=FILES_API_HEADERS,
            timeout=15,
        )
        print(f"[FILES API] Archivo {file_id} eliminado → {r.status_code}")
    except Exception as e:
        print(f"[FILES API] Error eliminando {file_id}: {e}")

def obtener_file_id_cerebro():
    """
    Devuelve el file_id del cerebro en Anthropic Files API.
    Si el caché de 24h expiró: relee Notion, sube nuevo archivo, elimina el viejo.
    """
    if not NOTION_TOKEN or not NOTION_PAGE_ID:
        return None  # fallback: usa texto directo
    ahora = time.time()
    if _cache["file_id"] and (ahora - _cache["momento"] < CACHE_SEGUNDOS):
        return _cache["file_id"]
    try:
        texto = _leer_bloques(NOTION_PAGE_ID)
        if not texto or len(texto) < 50:
            texto = _cache["texto"] or FALLBACK_CEREBRO
        file_id_nuevo = _subir_cerebro_files_api(texto)
        if file_id_nuevo:
            # Eliminar el archivo anterior si existe
            if _cache["file_id"]:
                threading.Thread(
                    target=_eliminar_archivo_files_api,
                    args=(_cache["file_id"],),
                    daemon=True,
                ).start()
            _cache["texto"] = texto
            _cache["file_id"] = file_id_nuevo
            _cache["momento"] = ahora
            return file_id_nuevo
        # Si falla la subida, usar texto directo como fallback
        _cache["texto"] = texto
        _cache["momento"] = ahora
        return None
    except Exception as e:
        print(f"[FILES API] Error actualizando cerebro: {e}")
        return _cache["file_id"]

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

# ─── TOKEN SHOPIFY: RENOVACIÓN AUTOMÁTICA (Client Credentials Grant) ─────────
_shopify_token_cache = {"token": None, "expires_at": 0}

def get_shopify_token() -> str:
    """
    Devuelve un token válido de Shopify Admin API.
    Lo renueva automáticamente 1 hora antes de que expire (cada ~23h).
    """
    ahora = time.time()
    margen = 3600  # renovar 1 hora antes de expirar

    if _shopify_token_cache["token"] and ahora < (_shopify_token_cache["expires_at"] - margen):
        return _shopify_token_cache["token"]

    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        print("[SHOPIFY] Faltan SHOPIFY_CLIENT_ID o SHOPIFY_CLIENT_SECRET en Railway.")
        return ""

    try:
        r = requests.post(
            f"https://{SHOPIFY_STORE}/admin/oauth/access_token",
            params={
                "grant_type": "client_credentials",
                "client_id": SHOPIFY_CLIENT_ID,
                "client_secret": SHOPIFY_CLIENT_SECRET,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        _shopify_token_cache["token"] = data["access_token"]
        _shopify_token_cache["expires_at"] = ahora + data.get("expires_in", 86400)
        print(f"[SHOPIFY] Token renovado automáticamente. Expira en {data.get('expires_in', 86400) // 3600}h")
        return _shopify_token_cache["token"]
    except Exception as e:
        print(f"[SHOPIFY] Error renovando token: {e}")
        return _shopify_token_cache["token"] or ""  # fallback al token anterior si existe

# ─── CONSULTAR PRODUCTO EN VIVO (Shopify) ───────────────────────────────────
def _fmt_clp(amount):
    try:
        n = int(round(float(amount)))
        return "$" + f"{n:,}".replace(",", ".")
    except Exception:
        return f"${amount}"

def consultar_producto(handle=None, busqueda=None, talla=None, color=None):
    if not SHOPIFY_STORE:
        return "No tengo acceso al catálogo en vivo ahora; deriva a una persona del equipo."
    token = get_shopify_token()
    if not token:
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
            f"https://{SHOPIFY_STORE}/admin/api/2026-04/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={"query": query, "variables": {"q": q}},
            timeout=20,
        )
        print(f"[SHOPIFY] STATUS={r.status_code} consultar_producto handle={handle}")
        if r.status_code == 401:
            print("[SHOPIFY] 401 en consultar_producto — limpiando caché de token")
            _shopify_token_cache["token"] = None
            _shopify_token_cache["expires_at"] = 0
        if r.status_code != 200:
            print(f"[SHOPIFY] ERROR BODY={r.text[:300]}")
        data = r.json()
        errors = data.get("errors")
        if errors:
            print(f"[SHOPIFY] GRAPHQL ERRORS={errors}")
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

# ─── CONSULTAR PEDIDO EN SHOPIFY ────────────────────────────────────────────
def consultar_pedido(numero_pedido=None, email=None):
    if not SHOPIFY_STORE:
        return "No tengo acceso a los pedidos ahora; deriva a una persona del equipo."
    token = get_shopify_token()
    if not token:
        return "No tengo acceso a los pedidos ahora; deriva a una persona del equipo."
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    try:
        if numero_pedido:
            num = numero_pedido.strip().lstrip("#")
            query = (
                "query($q:String!){ orders(first:1, query:$q){ edges{ node{ "
                "name displayFinancialStatus displayFulfillmentStatus "
                "fulfillments{ trackingInfo{ number url } } "
                "lineItems(first:5){ edges{ node{ title quantity } } } "
                "} } } }"
            )
            variables = {"q": f"name:#{num}"}
        elif email:
            query = (
                "query($q:String!){ orders(first:1, query:$q, sortKey:CREATED_AT, reverse:true){ edges{ node{ "
                "name displayFinancialStatus displayFulfillmentStatus "
                "fulfillments{ trackingInfo{ number url } } "
                "lineItems(first:5){ edges{ node{ title quantity } } } "
                "} } } }"
            )
            variables = {"q": f"email:{email.strip()}"}
        else:
            return "No se indicó número de pedido ni email para buscar."
        r = requests.post(
            f"https://{SHOPIFY_STORE}/admin/api/2026-04/graphql.json",
            headers=headers, json={"query": query, "variables": variables}, timeout=20,
        )
        edges = r.json().get("data", {}).get("orders", {}).get("edges", [])
        if not edges:
            return "No encontré ningún pedido con esos datos."
        o = edges[0]["node"]
        nombre = o.get("name", "")
        pago = o.get("displayFinancialStatus", "")
        fulfillment = o.get("displayFulfillmentStatus", "")
        productos = ", ".join(
            f"{e['node']['title']} x{e['node']['quantity']}"
            for e in o.get("lineItems", {}).get("edges", [])
        )
        tracking_info = []
        for f in (o.get("fulfillments") or []):
            for t in (f.get("trackingInfo") or []):
                if t.get("number"):
                    tracking_info.append(f"N° {t['number']}" + (f" — {t['url']}" if t.get("url") else ""))
        partes = [f"Pedido {nombre}.", f"Pago: {pago}.", f"Estado de envío: {fulfillment}."]
        if productos:
            partes.append(f"Productos: {productos}.")
        if tracking_info:
            partes.append("Seguimiento: " + "; ".join(tracking_info) + ".")
        else:
            partes.append("Aún no hay número de seguimiento disponible.")
        return " ".join(partes)
    except Exception as e:
        print(f"Error consultando pedido: {e}")
        return "No pude consultar el pedido ahora; deriva a una persona del equipo."

# ─── VALIDAR CÓDIGO DE DESCUENTO EN SHOPIFY ─────────────────────────────────
def validar_descuento(codigo):
    if not SHOPIFY_STORE:
        return "No tengo acceso a los descuentos ahora; deriva a una persona del equipo."
    token = get_shopify_token()
    if not token:
        return "No tengo acceso a los descuentos ahora; deriva a una persona del equipo."
    try:
        query = (
            "query($q:String!){ codeDiscountNodes(first:1, query:$q){ edges{ node{ "
            "codeDiscount { ... on DiscountCodeBasic { "
            "title status usageLimit usedCount "
            "startsAt endsAt "
            "minimumRequirement { ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount } } } "
            "customerEligibility { ... on DiscountCustomers { customers { edges { node { id } } } } } "
            "} } } } } }"
        )
        codigo_upper = codigo.strip().upper()
        r = requests.post(
            f"https://{SHOPIFY_STORE}/admin/api/2026-04/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={"query": query, "variables": {"q": codigo_upper}},
            timeout=20,
        )
        all_edges = r.json().get("data", {}).get("codeDiscountNodes", {}).get("edges", [])
        # Filtrar exactamente el código solicitado
        edges = [
            e for e in all_edges
            if any(
                c["node"]["code"].upper() == codigo_upper
                for c in e["node"].get("codeDiscount", {}).get("codes", {}).get("edges", [])
            )
        ]
        if not edges:
            return f"El código '{codigo}' no existe en la tienda o está mal escrito."
        d = edges[0]["node"].get("codeDiscount", {})
        status = d.get("status", "")
        if status == "EXPIRED":
            return f"El código '{codigo}' existe pero está VENCIDO."
        if status == "SCHEDULED":
            return f"El código '{codigo}' aún no está activo (programado para el futuro)."
        limit = d.get("usageLimit")
        used = d.get("usedCount", 0)
        if limit and used >= limit:
            return f"El código '{codigo}' es válido pero ya agotó todos sus usos ({used}/{limit})."
        minimo = None
        req = d.get("minimumRequirement") or {}
        sub = req.get("greaterThanOrEqualToSubtotal") or {}
        if sub.get("amount"):
            minimo = _fmt_clp(sub["amount"])
        partes = [f"El código '{codigo}' está ACTIVO."]
        if minimo:
            partes.append(f"Requiere mínimo de compra de {minimo}.")
        if limit:
            partes.append(f"Usos: {used}/{limit}.")
        ends = d.get("endsAt")
        if ends:
            partes.append(f"Vence: {ends[:10]}.")
        return " ".join(partes)
    except Exception as e:
        print(f"Error validando descuento: {e}")
        return "No pude verificar el código ahora; deriva a una persona del equipo."

# ─── VERIFICAR SI CLIENTA YA HA COMPRADO (para descuento primera compra) ────
def verificar_cliente(email=None, telefono=None):
    if not SHOPIFY_STORE:
        return "No tengo acceso a los clientes ahora; deriva a una persona del equipo."
    token = get_shopify_token()
    if not token:
        return "No tengo acceso a los clientes ahora; deriva a una persona del equipo."
    try:
        if email:
            q = f"email:{email.strip()}"
        elif telefono:
            tel = telefono.strip().replace(" ", "").replace("+", "")
            q = f"phone:{tel}"
        else:
            return "No se indicó email ni teléfono para verificar."
        query = (
            "query($q:String!){ customers(first:1, query:$q){ edges{ node{ "
            "numberOfOrders ordersCount { count } "
            "} } } }"
        )
        r = requests.post(
            f"https://{SHOPIFY_STORE}/admin/api/2026-04/graphql.json",
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            json={"query": query, "variables": {"q": q}},
            timeout=20,
        )
        edges = r.json().get("data", {}).get("customers", {}).get("edges", [])
        if not edges:
            return "No encontré esta persona en los clientes de la tienda. Podría ser su primera compra."
        c = edges[0]["node"]
        count = c.get("numberOfOrders") or (c.get("ordersCount") or {}).get("count") or 0
        if count == 0:
            return "Esta persona está registrada pero nunca ha completado una compra. Aplica el descuento de primera compra."
        return f"Esta persona ya tiene {count} compra(s) registrada(s). El código SC2610 (primera compra) NO le aplica."
    except Exception as e:
        print(f"Error verificando cliente: {e}")
        return "No pude verificar el historial de compras ahora; deriva a una persona del equipo."

def ejecutar_herramienta(nombre, args):
    if nombre == "consultar_producto":
        return consultar_producto(args.get("handle"), args.get("busqueda"), args.get("talla"), args.get("color"))
    if nombre == "consultar_pedido":
        return consultar_pedido(args.get("numero_pedido"), args.get("email"))
    if nombre == "validar_descuento":
        return validar_descuento(args.get("codigo", ""))
    if nombre == "verificar_cliente":
        return verificar_cliente(args.get("email"), args.get("telefono"))
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

def enviar_dividido(numero, texto):
    """Divide la respuesta por ||| y envía cada parte como mensaje separado con delay."""
    partes = [p.strip() for p in texto.split("|||") if p.strip()]
    if not partes:
        partes = [texto]
    for i, parte in enumerate(partes):
        enviar_whatsapp(numero, parte)
        if i < len(partes) - 1:
            time.sleep(0.8)  # pequeño delay entre mensajes para que lleguen en orden

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
    historial = conversaciones.get(numero, [])
    try:
        # System prompt: solo INSTRUCCION_FIJA con Prompt Caching (texto pequeño y estático)
        system = [{"type": "text", "text": INSTRUCCION_FIJA, "cache_control": {"type": "ephemeral"}}]

        # Cerebro: se adjunta como documento via Files API (costo fijo, no crece con el cerebro)
        file_id = obtener_file_id_cerebro()
        if file_id:
            # Cerebro como archivo → casi 0 tokens por mensaje
            if isinstance(contenido_api, str):
                contenido_con_cerebro = [
                    {"type": "document", "source": {"type": "file", "file_id": file_id}},
                    {"type": "text", "text": contenido_api},
                ]
            else:
                contenido_con_cerebro = [
                    {"type": "document", "source": {"type": "file", "file_id": file_id}},
                ] + (contenido_api if isinstance(contenido_api, list) else [contenido_api])
        else:
            # Fallback: usa texto directo si Files API falla
            texto_cerebro = _cache.get("texto") or FALLBACK_CEREBRO
            if isinstance(contenido_api, str):
                contenido_con_cerebro = [{"type": "text", "text": f"[Contexto del negocio]\n{texto_cerebro}\n\n[Mensaje]\n{contenido_api}"}]
            else:
                contenido_con_cerebro = contenido_api

        mensajes = historial + [{"role": "user", "content": contenido_con_cerebro}]
        texto = ""
        for _ in range(4):
            respuesta = cliente_ia.beta.messages.create(
                model=MODELO,
                max_tokens=600,
                system=system,
                messages=mensajes,
                tools=HERRAMIENTAS,
                betas=["prompt-caching-2024-07-31", "files-api-2025-04-14"],
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


# ─── REPORTE DIARIO A TELEGRAM ──────────────────────────────────────────────
def _leer_metricas_bot(fecha_inicio_iso, fecha_fin_iso):
    if not NOTION_TOKEN or not NOTION_DB_CONVERSACIONES:
        return []
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_CONVERSACIONES}/query"
    payload = {
        "filter": {
            "and": [
                {"property": "Fecha", "date": {"on_or_after": fecha_inicio_iso}},
                {"property": "Fecha", "date": {"on_or_before": fecha_fin_iso}},
            ]
        },
        "page_size": 100,
    }
    todos, has_more, cursor = [], True, None
    while has_more:
        if cursor:
            payload["start_cursor"] = cursor
        try:
            r = requests.post(
                url,
                headers={**NOTION_HEADERS, "Content-Type": "application/json"},
                json=payload,
                timeout=15,
            )
            if r.status_code != 200:
                break
            data = r.json()
            todos.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            cursor = data.get("next_cursor")
        except Exception as e:
            print(f"[REPORTE] Error leyendo Notion: {e}")
            break
    return todos

def generar_reporte_diario():
    try:
        ahora_chile = datetime.now(TZ)
        ayer       = (ahora_chile - timedelta(days=1)).replace(hour=0,  minute=0,  second=0,  microsecond=0)
        ayer_fin   = (ahora_chile - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)

        registros = _leer_metricas_bot(ayer.isoformat(), ayer_fin.isoformat())
        if not registros:
            return None

        mensajes_cliente, mensajes_bot, numeros = [], [], set()
        derivaciones, categorias = 0, {}

        for reg in registros:
            props   = reg.get("properties", {})
            quien   = props.get("Quién", {}).get("select", {}).get("name", "")
            mensaje = (props.get("Mensaje", {}).get("title") or [{}])[0].get("plain_text", "")
            numero  = props.get("Número", {}).get("phone_number", "")
            if numero:
                numeros.add(numero)
            if quien == "Cliente":
                mensajes_cliente.append(mensaje)
            elif quien == "Bot":
                mensajes_bot.append(mensaje)
                if "persona del equipo" in mensaje.lower():
                    derivaciones += 1
                    idx = len(mensajes_bot) - 1
                    msg_cli = mensajes_cliente[idx] if idx < len(mensajes_cliente) else ""
                    cat = clasificar_caso(msg_cli, mensaje)
                    categorias[cat] = categorias.get(cat, 0) + 1

        fecha_str = ayer.strftime("%d/%m/%Y")
        lineas = [
            f"📊 <b>Reporte Bot WhatsApp — {fecha_str}</b>",
            "",
            f"💬 Conversaciones únicas: <b>{len(numeros)}</b>",
            f"📨 Mensajes recibidos: <b>{len(mensajes_cliente)}</b>",
            f"🤖 Respuestas del bot: <b>{len(mensajes_bot)}</b>",
            f"🔔 Derivaciones al equipo: <b>{derivaciones}</b>",
        ]
        if categorias:
            lineas += ["", "📂 <b>Derivaciones por categoría:</b>"]
            for cat, cnt in sorted(categorias.items(), key=lambda x: -x[1]):
                lineas.append(f"  {cat}: {cnt}")
        if not numeros:
            lineas += ["", "😴 Sin actividad ayer."]

        return "\n".join(lineas)
    except Exception as e:
        print(f"[REPORTE] Error generando: {e}")
        return None

def _enviar_reporte_diario():
    reporte = generar_reporte_diario()
    if not reporte or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": reporte, "parse_mode": "HTML"},
            timeout=15,
        )
        print("[REPORTE] Enviado a Telegram ✅")
    except Exception as e:
        print(f"[REPORTE] Error enviando: {e}")

def _scheduler_reporte():
    """Corre en background y envía el reporte todos los días a las 8:00 AM Chile."""
    while True:
        ahora   = datetime.now(TZ)
        obj     = ahora.replace(hour=8, minute=0, second=0, microsecond=0)
        if ahora >= obj:
            obj += timedelta(days=1)
        segundos = (obj - ahora).total_seconds()
        print(f"[REPORTE] Próximo reporte en {segundos/3600:.1f}h")
        time.sleep(segundos)
        _enviar_reporte_diario()

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

    if "calls" in value:
        print("[info] Evento de llamada ignorado (el bot no contesta llamadas).")
        return "OK", 200

    if "messages" not in value:
        return "OK", 200

    mensaje = value["messages"][0]
    numero = mensaje["from"]
    tipo = mensaje.get("type")

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
            if numero in NUMEROS_EN_HUMANO:
                guardar_conversacion(numero, "Cliente", texto_clienta)
                return "OK", 200
            print(f"Mensaje de {numero}: {texto_clienta}")
            guardar_conversacion(numero, "Cliente", texto_clienta)
            respuesta = generar_respuesta(numero, texto_clienta, texto_clienta)
            enviar_dividido(numero, respuesta)
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
            enviar_dividido(numero, respuesta)
            guardar_conversacion(numero, "Bot", respuesta)

        elif tipo == "audio":
            if numero in NUMEROS_EN_HUMANO:
                return "OK", 200
            media_id = mensaje["audio"]["id"]
            transcripcion = transcribir_audio(media_id)
            if not transcripcion:
                guardar_conversacion(numero, "Cliente", "🎙️ (audio no entendido)")
                enviar_whatsapp(numero, "Uy, no pude escuchar bien tu audio 😅 ¿me lo escribes porfa?")
                return "OK", 200
            print(f"Audio de {numero} transcrito: {transcripcion}")
            guardar_conversacion(numero, "Cliente", "🎙️ " + transcripcion)
            respuesta = generar_respuesta(numero, transcripcion, transcripcion)
            enviar_dividido(numero, respuesta)
            guardar_conversacion(numero, "Bot", respuesta)

        else:
            enviar_whatsapp(numero, "¡Hola! 💕 Cuéntame en qué te ayudo. Puedes escribirme, mandarme una foto 📷 o un audio 🎙️")
    except Exception as e:
        print(f"Error procesando mensaje: {e}")
    return "OK", 200

@app.route("/", methods=["GET"])
def inicio():
    return "Bot Soulcute WhatsApp activo ✅", 200

# ─── PANEL DE ADMINISTRACIÓN ─────────────────────────────────────────────────

def _auth_admin(req):
    pwd = req.args.get("pwd") or req.headers.get("X-Admin-Password", "")
    return pwd == ADMIN_PASSWORD

def _notion_conversaciones():
    """Lee últimas 50 entradas de la BD de conversaciones en Notion."""
    try:
        url = f"https://api.notion.com/v1/databases/{NOTION_DB_CONVERSACIONES}/query"
        body = {"page_size": 50, "sorts": [{"timestamp": "created_time", "direction": "descending"}]}
        r = requests.post(url, headers=NOTION_HEADERS, json=body, timeout=15)
        if r.status_code != 200:
            return []
        rows = []
        for p in r.json().get("results", []):
            props = p.get("properties", {})
            # Número es phone_number
            numero = props.get("Número", {}).get("phone_number") or ""
            # Quién es select
            quien_obj = props.get("Quién", {}).get("select") or {}
            quien = quien_obj.get("name", "")
            # Mensaje es title
            mensaje_arr = props.get("Mensaje", {}).get("title") or []
            mensaje = "".join(x.get("plain_text", "") for x in mensaje_arr)
            rows.append({
                "numero": numero,
                "quien": quien,
                "mensaje": mensaje,
                "fecha": p.get("created_time", "")[:16].replace("T", " "),
            })
        return rows
    except Exception as e:
        print(f"Error leyendo conversaciones Notion: {e}")
        return []

@app.route("/admin")
def panel_admin():
    pwd = request.args.get("pwd", "")
    if pwd != ADMIN_PASSWORD:
        return Response("""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><title>Soulcute Admin</title>
<style>*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,sans-serif}
body{background:#f5f5f5;display:flex;align-items:center;justify-content:center;height:100vh}
.card{background:#fff;border-radius:12px;padding:32px;width:320px;border:1px solid #e5e5e5}
h2{font-size:16px;font-weight:500;margin-bottom:4px}
p{font-size:13px;color:#888;margin-bottom:20px}
input{width:100%;padding:9px 12px;border:1px solid #ddd;border-radius:8px;font-size:13px;margin-bottom:12px;outline:none}
input:focus{border-color:#7B3F6E}
button{width:100%;padding:10px;background:#7B3F6E;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:500;cursor:pointer}
button:hover{background:#6B3560}
.logo{width:40px;height:40px;background:#7B3F6E;border-radius:10px;display:flex;align-items:center;justify-content:center;margin-bottom:16px}
</style></head>
<body><div class="card">
<div class="logo"><svg width="22" height="22" fill="none" viewBox="0 0 24 24" stroke="#fff" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></div>
<h2>Soulcute — Panel WhatsApp</h2>
<p>Ingresa la contraseña para continuar</p>
<form onsubmit="login(event)">
<input type="password" id="pwd" placeholder="Contraseña" autofocus>
<button type="submit">Entrar</button>
</form>
</div>
<script>function login(e){e.preventDefault();const p=document.getElementById('pwd').value;if(p)window.location.href='/admin?pwd='+encodeURIComponent(p);}</script>
</body></html>""", mimetype="text/html")
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Soulcute — Panel WhatsApp</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@latest/dist/tabler-icons.min.css">
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
body{{background:#f5f5f5;height:100vh;display:flex;flex-direction:column}}
.topbar{{background:#7B3F6E;color:#fff;padding:10px 20px;display:flex;align-items:center;gap:12px;flex-shrink:0}}
.topbar h1{{font-size:15px;font-weight:500}}
.topbar span{{font-size:12px;opacity:0.75;margin-left:auto}}
.layout{{display:flex;flex:1;overflow:hidden}}
.sidebar{{width:280px;background:#fff;border-right:1px solid #e5e5e5;display:flex;flex-direction:column;flex-shrink:0}}
.sidebar-header{{padding:12px 16px;border-bottom:1px solid #e5e5e5;display:flex;justify-content:space-between;align-items:center}}
.sidebar-header h2{{font-size:13px;font-weight:500;color:#333}}
.refresh-btn{{background:none;border:1px solid #ddd;border-radius:6px;padding:4px 8px;cursor:pointer;font-size:12px;color:#666;display:flex;align-items:center;gap:4px}}
.refresh-btn:hover{{background:#f5f5f5}}
.conv-list{{flex:1;overflow-y:auto}}
.conv-item{{padding:12px 16px;cursor:pointer;border-bottom:1px solid #f0f0f0;transition:background 0.1s}}
.conv-item:hover{{background:#faf5f9}}
.conv-item.active{{background:#f9f0f7;border-left:3px solid #7B3F6E}}
.conv-top{{display:flex;justify-content:space-between;margin-bottom:3px}}
.conv-num{{font-size:12px;font-weight:500;color:#333}}
.conv-time{{font-size:11px;color:#999}}
.conv-last{{font-size:11px;color:#777;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.badge-humano{{display:inline-block;background:#fee2e2;color:#b91c1c;font-size:10px;padding:1px 6px;border-radius:99px;margin-top:3px}}
.badge-bot{{display:inline-block;background:#dcfce7;color:#15803d;font-size:10px;padding:1px 6px;border-radius:99px;margin-top:3px}}
.chat-area{{flex:1;display:flex;flex-direction:column;overflow:hidden}}
.chat-header{{padding:12px 20px;background:#fff;border-bottom:1px solid #e5e5e5;display:flex;align-items:center;gap:12px}}
.chat-header h2{{font-size:14px;font-weight:500;flex:1}}
.btn-tomar{{padding:6px 14px;border-radius:6px;border:1px solid #7B3F6E;color:#7B3F6E;background:#fff;cursor:pointer;font-size:12px;font-weight:500}}
.btn-tomar:hover{{background:#7B3F6E;color:#fff}}
.btn-liberar{{padding:6px 14px;border-radius:6px;border:1px solid #15803d;color:#15803d;background:#fff;cursor:pointer;font-size:12px;font-weight:500}}
.btn-liberar:hover{{background:#15803d;color:#fff}}
.messages{{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px;background:#f5f5f5}}
.msg{{max-width:70%}}
.msg-cliente{{align-self:flex-start}}
.msg-bot{{align-self:flex-end}}
.msg-humano{{align-self:flex-end}}
.bubble{{padding:9px 13px;border-radius:12px;font-size:13px;line-height:1.5}}
.bubble-cliente{{background:#fff;border:1px solid #e5e5e5;color:#333}}
.bubble-bot{{background:#eef2ff;color:#3730a3}}
.bubble-humano{{background:#7B3F6E;color:#fff}}
.msg-meta{{font-size:10px;color:#999;margin-top:2px}}
.msg-bot .msg-meta,.msg-humano .msg-meta{{text-align:right}}
.reply-area{{padding:12px 16px;background:#fff;border-top:1px solid #e5e5e5}}
.reply-status{{font-size:11px;color:#666;margin-bottom:8px;display:flex;align-items:center;gap:6px}}
.reply-row{{display:flex;gap:8px}}
textarea{{flex:1;padding:9px 12px;border:1px solid #ddd;border-radius:8px;font-size:13px;resize:none;height:60px;font-family:inherit;outline:none}}
textarea:focus{{border-color:#7B3F6E}}
.send-btn{{background:#7B3F6E;color:#fff;border:none;border-radius:8px;padding:0 20px;cursor:pointer;font-size:13px;font-weight:500;height:60px}}
.send-btn:hover{{background:#6B3560}}
.empty{{flex:1;display:flex;align-items:center;justify-content:center;color:#999;font-size:14px}}
.back-btn{{display:none;background:none;border:none;color:#fff;font-size:20px;cursor:pointer;padding:0 4px}}

@media (max-width: 768px) {{
  .topbar h1{{font-size:13px}}
  .topbar span{{display:none}}
  .layout{{position:relative}}
  .sidebar{{width:100%;position:absolute;inset:0;z-index:1;transition:transform 0.2s}}
  .sidebar.hide-mobile{{transform:translateX(-100%)}}
  .chat-area{{width:100%;position:absolute;inset:0;z-index:2;background:#f5f5f5;transform:translateX(100%);transition:transform 0.2s}}
  .chat-area.show-mobile{{transform:translateX(0)}}
  .back-btn{{display:inline-block}}
  .chat-header h2{{font-size:13px}}
  .msg{{max-width:85%}}
  .reply-row{{flex-direction:column}}
  .send-btn{{height:40px}}
  textarea{{height:50px}}
}}
</style>
</head>
<body>
<div class="topbar">
  <i class="ti ti-message-circle" style="font-size:20px"></i>
  <h1>Soulcute — Panel WhatsApp</h1>
  <span>+56 9 8260 1800</span>
</div>
<div class="layout">
  <div class="sidebar">
    <div class="sidebar-header">
      <h2>Conversaciones</h2>
      <button class="refresh-btn" onclick="cargarConversaciones()">
        <i class="ti ti-refresh" style="font-size:12px"></i> Actualizar
      </button>
    </div>
    <div class="conv-list" id="conv-list">
      <div style="padding:20px;text-align:center;color:#999;font-size:13px">Cargando...</div>
    </div>
  </div>
  <div class="chat-area" id="chat-area">
    <div class="empty"><div style="text-align:center"><i class="ti ti-message-2" style="font-size:40px;display:block;margin-bottom:8px;opacity:0.3"></i>Selecciona una conversación</div></div>
  </div>
</div>
<script>
const PWD = "{pwd}";
const API = (path) => path + "?pwd=" + PWD;
let numeroActivo = null;
let numerosHumano = new Set();
let convData = {{}};

async function cargarConversaciones() {{
  const r = await fetch(API("/admin/api/conversaciones"));
  const data = await r.json();
  convData = {{}};
  data.forEach(m => {{
    if (!convData[m.numero]) convData[m.numero] = [];
    convData[m.numero].push(m);
  }});
  const numeros = Object.keys(convData);
  const list = document.getElementById("conv-list");
  if (!numeros.length) {{ list.innerHTML = '<div style="padding:20px;text-align:center;color:#999;font-size:13px">Sin conversaciones</div>'; return; }}
  list.innerHTML = numeros.map(n => {{
    const msgs = convData[n];
    const ultimo = msgs[0];
    const enHumano = numerosHumano.has(n);
    return `<div class="conv-item${{n === numeroActivo ? " active" : ""}}" onclick="abrirChat('${{n}}')">
      <div class="conv-top"><span class="conv-num">${{n}}</span><span class="conv-time">${{ultimo.fecha.slice(11)}}</span></div>
      <div class="conv-last">${{ultimo.quien}}: ${{ultimo.mensaje.slice(0,40)}}</div>
      <span class="${{enHumano ? "badge-humano" : "badge-bot"}}">${{enHumano ? "🟠 Tú atiendes" : "🟢 Bot"}}</span>
    </div>`;
  }}).join("");
}}

function abrirChat(numero) {{
  numeroActivo = numero;
  cargarConversaciones();
  document.querySelector(".sidebar").classList.add("hide-mobile");
  document.getElementById("chat-area").classList.add("show-mobile");
  const msgs = convData[numero] || [];
  const enHumano = numerosHumano.has(numero);
  const area = document.getElementById("chat-area");
  area.innerHTML = `
    <div class="chat-header">
      <button class="back-btn" onclick="volverLista()"><i class="ti ti-arrow-left"></i></button>
      <i class="ti ti-user-circle" style="font-size:28px;color:#7B3F6E"></i>
      <h2>${{numero}}</h2>
      ${{enHumano
        ? `<button class="btn-liberar" onclick="liberarBot('${{numero}}')"><i class="ti ti-robot"></i> Devolver al bot</button>`
        : `<button class="btn-tomar" onclick="tomarConversacion('${{numero}}')"><i class="ti ti-user-check"></i> Tomar conversación</button>`
      }}
    </div>
    <div class="messages" id="msgs">
      ${{[...msgs].reverse().map(m => {{
        const cls = m.quien === "Cliente" ? "msg-cliente" : m.quien === "Bot" ? "msg-bot" : "msg-humano";
        const bcls = m.quien === "Cliente" ? "bubble-cliente" : m.quien === "Bot" ? "bubble-bot" : "bubble-humano";
        return `<div class="msg ${{cls}}"><div class="bubble ${{bcls}}">${{m.mensaje}}</div><div class="msg-meta">${{m.quien}} · ${{m.fecha}}</div></div>`;
      }}).join("")}}
    </div>
    <div class="reply-area">
      <div class="reply-status">${{enHumano
        ? `<span style="color:#b45309">⚠️ Bot pausado — tú estás atendiendo este chat</span>`
        : `<span style="color:#6b7280">ℹ️ El bot está respondiendo. Haz clic en "Tomar conversación" para responder tú.</span>`
      }}</div>
      <div class="reply-row">
        <textarea id="msg-input" placeholder="${{enHumano ? "Escribe tu respuesta..." : "Toma la conversación primero para responder"}}" ${{enHumano ? "" : "disabled"}}></textarea>
        <button class="send-btn" onclick="enviarMensaje('${{numero}}')" ${{enHumano ? "" : "disabled"}}>Enviar</button>
      </div>
    </div>`;
  const msgsEl = document.getElementById("msgs");
  if (msgsEl) msgsEl.scrollTop = msgsEl.scrollHeight;
}}

function volverLista() {{
  numeroActivo = null;
  document.querySelector(".sidebar").classList.remove("hide-mobile");
  document.getElementById("chat-area").classList.remove("show-mobile");
  cargarConversaciones();
}}

async function tomarConversacion(numero) {{
  await fetch(API("/admin/api/tomar/" + numero), {{method: "POST"}});
  numerosHumano.add(numero);
  abrirChat(numero);
}}

async function liberarBot(numero) {{
  await fetch(API("/admin/api/liberar/" + numero), {{method: "POST"}});
  numerosHumano.delete(numero);
  abrirChat(numero);
}}

async function enviarMensaje(numero) {{
  const input = document.getElementById("msg-input");
  const texto = input.value.trim();
  if (!texto) return;
  input.value = "";
  await fetch(API("/admin/api/enviar"), {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{numero, texto}})
  }});
  await cargarConversaciones();
  abrirChat(numero);
}}

cargarConversaciones();
setInterval(cargarConversaciones, 30000);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")

@app.route("/admin/api/conversaciones")
def api_conversaciones():
    if not _auth_admin(request):
        return jsonify({"error": "no autorizado"}), 401
    return jsonify(_notion_conversaciones())

@app.route("/admin/api/tomar/<numero>", methods=["POST"])
def api_tomar(numero):
    if not _auth_admin(request):
        return jsonify({"error": "no autorizado"}), 401
    NUMEROS_EN_HUMANO.add(numero)
    print(f"[ADMIN] {numero} tomado por humano")
    return jsonify({"ok": True})

@app.route("/admin/api/liberar/<numero>", methods=["POST"])
def api_liberar(numero):
    if not _auth_admin(request):
        return jsonify({"error": "no autorizado"}), 401
    NUMEROS_EN_HUMANO.discard(numero)
    print(f"[ADMIN] {numero} devuelto al bot")
    return jsonify({"ok": True})

@app.route("/admin/api/enviar", methods=["POST"])
def api_enviar():
    if not _auth_admin(request):
        return jsonify({"error": "no autorizado"}), 401
    data = request.get_json()
    numero = data.get("numero", "")
    texto = data.get("texto", "")
    if not numero or not texto:
        return jsonify({"error": "faltan datos"}), 400
    enviar_whatsapp(numero, texto)
    guardar_conversacion(numero, "Humano", texto)
    return jsonify({"ok": True})

if __name__ == "__main__":
    threading.Thread(target=_scheduler_reporte, daemon=True).start()
    puerto = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=puerto)
