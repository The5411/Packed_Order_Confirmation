import json
import os
import tempfile
import redis
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
from datetime import datetime, timedelta
from clients.MintsoftClient import MintsoftOrderClient
import time
from zoneinfo import ZoneInfo

load_dotenv()

CLIENT = WebClient(token= os.getenv("SLACK_TOKEN"))
CHANNEL = os.getenv("ID_CANAL_DE_SLACK", "C0ASH80L065")

# Solo se avisan las ordenes del canal Pickandpack Boutique. El filtro se
# hace en la consulta a Mintsoft (/api/Order/List acepta ChannelId) y no
# por almacen, asi entra cualquier orden de Boutique sin importar desde que
# almacen salga.
BOUTIQUE_CHANNEL_ID = 51
BOUTIQUE_CHANNEL_NAME = "Pickandpack Boutique"

# Nombres de almacen para el mensaje de Slack.
WAREHOUSES = {
    3: "General / Wholesale",
    5: "Ecommerce",
    6: "Majors",
    7: "ATS",
}


# Las ventanas de consulta se pisan (cada corrida pide los ultimos 30-60
# min y la API no acepta cota superior), asi que la misma orden vuelve en
# varias corridas. Para no avisar dos veces guardamos los IDs ya enviados.
# Se usa el ID interno de Mintsoft y no el OrderNumber, porque el
# OrderNumber se repite entre ordenes distintas.
#
# El estado va a Redis (REDIS_URL, en Railway apunta al servicio
# Redis-packed). Si no hay REDIS_URL, por ejemplo corriendo local, se usa
# un archivo JSON.
REDIS_PREFIX = "packed_boutique"
REDIS_KEY_LAST_RUN = f"{REDIS_PREFIX}:last_run"


def _conectar_redis():
    """Devuelve el cliente de Redis, o None si no hay REDIS_URL.

    Si hay URL pero Redis no responde, se corta la corrida: seguir con un
    archivo en un contenedor efimero haria que se repitan los avisos.
    """
    url = os.getenv("REDIS_URL", "")
    if not url.startswith(("redis://", "rediss://")):
        if url:
            print("REDIS_URL no es una URL de Redis valida; se usa el archivo de estado")
        return None

    cliente = redis.Redis.from_url(url, decode_responses=True, socket_timeout=10)
    cliente.ping()
    return cliente


def _clave_orden(order_id):
    return f"{REDIS_PREFIX}:sent:{order_id}"


REDIS = _conectar_redis()

def _ruta_estado():
    """Donde guardar el estado entre corridas.

    En Railway el filesystem del contenedor es efimero: sin volumen se
    pierde la marca de agua en cada deploy y la ventana vuelve a ser un
    lookback fijo. Si hay un volumen montado, Railway expone su ruta en
    RAILWAY_VOLUME_MOUNT_PATH y el estado va ahi automaticamente.
    """
    explicita = os.getenv("SENT_ORDERS_FILE")
    if explicita:
        return explicita

    volumen = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if volumen:
        return os.path.join(volumen, "sent_orders.json")

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_orders.json")


SENT_ORDERS_FILE = _ruta_estado()
RETENTION_DAYS = 7

# La ventana no se calcula contra el reloj sino contra la ultima corrida que
# termino bien. Si una corrida no se ejecuta o falla, la siguiente arranca
# donde quedo la anterior en vez de dejar un hueco: con el reloj fijo cada
# orden tenia una sola oportunidad de ser vista y si esa corrida se perdia,
# la orden no se avisaba nunca.
DEFAULT_LOOKBACK_MINUTES = 30
OVERLAP_MINUTES = 5                  # margen por si una orden cae justo en el borde
MAX_LOOKBACK_DAYS = RETENTION_DAYS   # mas atras que esto ya no se puede deduplicar


def _cargar_estado_redis():
    # Cada orden es una clave propia que vence sola a los RETENTION_DAYS
    claves = list(REDIS.scan_iter(match=_clave_orden("*"), count=500))
    valores = REDIS.mget(claves) if claves else []
    enviadas = {
        clave.rsplit(":", 1)[1]: valor
        for clave, valor in zip(claves, valores) if valor
    }

    last_run = REDIS.get(REDIS_KEY_LAST_RUN)
    try:
        last_run = datetime.fromisoformat(last_run) if last_run else None
    except (TypeError, ValueError):
        last_run = None

    return enviadas, last_run


def cargar_estado():
    """Devuelve (ids ya avisados, momento de la ultima corrida OK)."""
    if REDIS:
        return _cargar_estado_redis()

    try:
        with open(SENT_ORDERS_FILE) as f:
            guardado = json.load(f)
    except FileNotFoundError:
        return {}, None
    except (ValueError, OSError) as e:
        print(f"No se pudo leer {SENT_ORDERS_FILE} ({e}); se arranca vacio")
        return {}, None

    # Formato viejo: el archivo era un dict plano de id -> fecha
    if "orders" in guardado:
        guardadas = guardado.get("orders") or {}
        last_run = guardado.get("last_run")
    else:
        guardadas, last_run = guardado, None

    corte = datetime.now(ZoneInfo("Europe/London")) - timedelta(days=RETENTION_DAYS)
    vigentes = {}
    for order_id, enviado_en in guardadas.items():
        try:
            if datetime.fromisoformat(enviado_en) >= corte:
                vigentes[order_id] = enviado_en
        except (TypeError, ValueError):
            continue

    try:
        last_run = datetime.fromisoformat(last_run) if last_run else None
    except (TypeError, ValueError):
        last_run = None

    return vigentes, last_run


def guardar_estado(enviadas, last_run=None):
    """Escribe el estado de forma atomica para no corromperlo si se corta."""
    if REDIS:
        # Las ordenes ya se guardan de a una en marcar_enviada
        if last_run is not None:
            REDIS.set(REDIS_KEY_LAST_RUN, last_run.isoformat())
        return

    carpeta = os.path.dirname(SENT_ORDERS_FILE) or "."
    datos = {"orders": enviadas}
    if last_run is not None:
        datos["last_run"] = last_run.isoformat()
    try:
        fd, tmp = tempfile.mkstemp(dir=carpeta, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(datos, f)
        os.replace(tmp, SENT_ORDERS_FILE)
    except OSError as e:
        print(f"No se pudo guardar {SENT_ORDERS_FILE}: {e}")


def marcar_enviada(enviadas, order_id, last_run):
    """Registra la orden como avisada apenas sale el mensaje."""
    enviado_en = datetime.now(ZoneInfo("Europe/London")).isoformat()
    enviadas[order_id] = enviado_en

    if REDIS:
        REDIS.set(_clave_orden(order_id), enviado_en, ex=RETENTION_DAYS * 24 * 3600)
    else:
        guardar_estado(enviadas, last_run)


def calcular_desde(last_run, ahora):
    """Desde cuando pedir ordenes, retomando donde quedo la corrida anterior."""
    if last_run is None:
        return ahora - timedelta(minutes=DEFAULT_LOOKBACK_MINUTES)

    desde = last_run - timedelta(minutes=OVERLAP_MINUTES)
    piso = ahora - timedelta(days=MAX_LOOKBACK_DAYS)
    return max(desde, piso)


def es_boutique(order):
    """True si la orden es del canal Pickandpack Boutique."""
    return order.get("ChannelId") == BOUTIQUE_CHANNEL_ID


def _num(valor):
    """Formatea pesos y medidas sin decimales de mas (5.00 -> 5, 7.5 -> 7.5)."""
    return f"{float(valor or 0):g}"


def destinatario(order):
    """Nombre del cliente final de la orden: empresa o, si no hay, la persona."""
    empresa = (order.get("CompanyName") or "").strip()
    if empresa:
        return empresa
    persona = f"{order.get('FirstName') or ''} {order.get('LastName') or ''}".strip()
    return persona or "-"


def linea_caja(caja):
    """Una linea por caja: cantidad de items, peso y medidas."""
    unidades = sum(i.get("Quantity") or 0 for i in caja.get("Items") or [])
    skus = len(caja.get("Items") or [])
    linea = f"• Caja {caja.get('BoxNumber')}: {unidades} items ({skus} SKU{'s' if skus != 1 else ''})"

    peso = caja.get("Weight") or 0
    linea += f" | {_num(peso)} kg" if peso else " | peso sin cargar"

    medidas = [caja.get("Length"), caja.get("Height"), caja.get("Depth")]
    if all(medidas):
        linea += f" | {' x '.join(_num(m) for m in medidas)} cm"
    return linea


def armar_mensaje(order, client_name, cajas):
    """Devuelve (texto plano, blocks) para Slack."""
    order_number = order.get("OrderNumber")
    canal = BOUTIQUE_CHANNEL_NAME
    almacen = WAREHOUSES.get(order.get("WarehouseId"), f"Almacen {order.get('WarehouseId')}")

    # El peso de la orden suele venir en 0 aunque las cajas tengan peso, asi
    # que se prioriza la suma de las cajas.
    peso_cajas = sum(c.get("Weight") or 0 for c in cajas)
    peso_total = peso_cajas or order.get("TotalWeight") or 0
    total_cajas = len(cajas) or order.get("NumberOfParcels") or 0

    texto = (
        f"Orden: {order_number} | Cliente: {client_name} | "
        f"Items: {order.get('TotalItems')} | Cajas: {total_cajas} | Peso: {_num(peso_total)} kg"
    )

    campos = [
        f"*Cliente:*\n{client_name}",
        f"*Destinatario:*\n{destinatario(order)}",
        f"*Canal:*\n{canal}",
        f"*Almacen:*\n{almacen}",
        f"*Items:*\n{order.get('TotalItems')}",
        f"*Cajas:*\n{total_cajas}",
        f"*Peso total:*\n{_num(peso_total)} kg" if peso_total else "*Peso total:*\nsin cargar",
        f"*Courier:*\n{order.get('CourierServiceName') or '-'}",
    ]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📦 Orden {order_number} packeada"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": c} for c in campos]},
    ]

    if cajas:
        detalle = "\n".join(linea_caja(c) for c in sorted(cajas, key=lambda c: c.get("BoxNumber") or 0))
        # Slack corta los textos de section en 3000 caracteres
        if len(detalle) > 2900:
            detalle = detalle[:2900].rsplit("\n", 1)[0] + "\n…"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Detalle por caja:*\n{detalle}"}})
    else:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "Sin detalle de cajas en Mintsoft"}
        ]})

    return texto, blocks


enviadas, last_run = cargar_estado()
print(f"Estado en {'Redis' if REDIS else SENT_ORDERS_FILE}")
print(f"{len(enviadas)} ordenes ya avisadas en los ultimos {RETENTION_DAYS} dias")
print(f"Ultima corrida OK: {last_run.strftime('%Y-%m-%d %H:%M:%S') if last_run else 'sin registro'}")

ms_client = MintsoftOrderClient()

# Cache de nombres de clientes. Arranca con /api/Client y se completa de a
# uno para los clientes que esa lista no devuelve (se topa en 100).
clientes_por_id = {}
clientes_faltantes = set()


def nombre_cliente(client_id):
    if client_id in clientes_por_id:
        return clientes_por_id[client_id]

    if client_id not in clientes_faltantes:
        info = ms_client.get_client(client_id)
        if info:
            clientes_por_id[client_id] = info.get("Name")
            return clientes_por_id[client_id]
        clientes_faltantes.add(client_id)

    return f"Cliente {client_id}"


try:

    status_id = 20

    uk_now = datetime.now(ZoneInfo("Europe/London"))
    desde = calcular_desde(last_run, uk_now)
    since_updated = desde.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"Consultando órdenes que hayan sido packeadas desde {since_updated}")
    clientes_por_id = {c.get("ID"): c.get("Name") for c in ms_client.get_clients()}
    new_packed_orders = ms_client.get_orders(
        since_updated, status_id, channel_id=BOUTIQUE_CHANNEL_ID
    )

    # Si esta vacio, significa no hay nuevas ordenes PACKED
    if not new_packed_orders:
        print("No se han pasado ordenes a Status PACKED")

    #Si tiene algo, es que hay nuevas ordenes en PACKED
    else:
        print(f"{len(new_packed_orders)} ordenes en PACKED")

        for order in new_packed_orders:
            order_number = order.get("OrderNumber")

            # Una orden que falla no puede cortar el resto del lote
            try:
                # La API ya filtra por canal; esto es por las dudas.
                if not es_boutique(order):
                    print(
                        f"Salteada {order_number}: ChannelId={order.get('ChannelId')} "
                        f"no es {BOUTIQUE_CHANNEL_NAME}"
                    )
                    continue

                order_id = str(order.get("ID"))
                order_client_id = order.get("ClientId")

                #Si ya se envio un mensaje al canal para esa orden:
                if order_id in enviadas:
                    print(f"Mensaje ya enviado para la orden {order_number} (ID {order_id})")
                    continue

                client_name = nombre_cliente(order_client_id)
                cajas = ms_client.get_box_packing_list(order.get("ID"))
                texto, blocks = armar_mensaje(order, client_name, cajas)

                CLIENT.chat_postMessage(
                    channel = CHANNEL,
                    text = texto,
                    blocks = blocks,
                )
                print(f"Mensaje enviado con exito para la orden {order_number}")

                # Se guarda en el momento: si la corrida se corta a la mitad,
                # lo ya avisado no se repite en la proxima.
                marcar_enviada(enviadas, order_id, last_run)
                time.sleep(1)

            except SlackApiError as e:
                print(f"Error de Slack API en la orden {order_number}: {e.response['error']}")

            except Exception as e:
                print(f"Error inesperado en la orden {order_number}: {e}")

    # Recien aca se mueve la marca de agua: si la corrida se corta antes,
    # la proxima vuelve a pedir desde el mismo punto y no se pierde nada.
    guardar_estado(enviadas, uk_now)
    print(f"Corrida OK, marca de agua en {uk_now:%Y-%m-%d %H:%M:%S}")

except SlackApiError as e:
    print(f"Error de Slack API: {e.response['error']}")

except Exception as e:
    print(f"Error inesperado: {e}")
