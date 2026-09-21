import json
import os
import tempfile
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
from datetime import datetime, timedelta
from clients.MintsoftClient import MintsoftOrderClient
import time
from zoneinfo import ZoneInfo

load_dotenv()

CLIENT = WebClient(token= os.getenv("SLACK_TOKEN"))
CHANNEL = "C0ASH80L065"

# Canales de wholesale. Todo lo que entre por aca va al canal de Slack,
# sea el cliente que sea. El objetivo del canal es avisarle al equipo de
# wholesale cuando termino el packing, asi que los canales de ecommerce
# (Shopify, Amazon, Manual Input Ecommerce, ShipStation Ecommerce, etc.)
# quedan afuera a proposito.
WHOLESALE_CHANNELS = {
    51: "Boutique",
    52: "Major",
    55: "Xorosoft",
    56: "Manual Input Wholesale",
    59: "ShipStation Wholesale",
}

# Almacenes de wholesale. La API acepta un WarehouseId por consulta, asi
# que se consultan de a uno y se juntan. Ecommerce (5) queda afuera.
WHOLESALE_WAREHOUSES = {
    3: "General / Wholesale",
    6: "Majors",
    7: "ATS",
}

# Excepciones por cliente: pares (ClientId, ChannelId) que son wholesale
# aunque el canal no sea de wholesale para el resto de los clientes.
CLIENT_CHANNEL_EXCEPTIONS = {
    (68, 48): "ByTimo - wholesale por ShipStation",
}


# Las ventanas de consulta se pisan (cada corrida pide los ultimos 30-60
# min y la API no acepta cota superior), asi que la misma orden vuelve en
# varias corridas. Para no avisar dos veces guardamos en disco los IDs ya
# enviados. Se usa el ID interno de Mintsoft y no el OrderNumber, porque el
# OrderNumber se repite entre ordenes distintas.
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


def cargar_estado():
    """Devuelve (ids ya avisados, momento de la ultima corrida OK)."""
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


def calcular_desde(last_run, ahora):
    """Desde cuando pedir ordenes, retomando donde quedo la corrida anterior."""
    if last_run is None:
        return ahora - timedelta(minutes=DEFAULT_LOOKBACK_MINUTES)

    desde = last_run - timedelta(minutes=OVERLAP_MINUTES)
    piso = ahora - timedelta(days=MAX_LOOKBACK_DAYS)
    return max(desde, piso)


def es_wholesale(order):
    """True si la orden le corresponde al equipo de wholesale."""
    channel_id = order.get("ChannelId")

    if channel_id in WHOLESALE_CHANNELS:
        return True

    return (order.get("ClientId"), channel_id) in CLIENT_CHANNEL_EXCEPTIONS


enviadas, last_run = cargar_estado()
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
        since_updated, status_id, warehouse_ids=list(WHOLESALE_WAREHOUSES)
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
                if not es_wholesale(order):
                    print(
                        f"Salteada {order_number}: ChannelId={order.get('ChannelId')} "
                        f"no es de wholesale (ClientId={order.get('ClientId')})"
                    )
                    continue

                order_id = str(order.get("ID"))
                order_client_id = order.get("ClientId")
                items = order.get("TotalItems")

                #Si ya se envio un mensaje al canal para esa orden:
                if order_id in enviadas:
                    print(f"Mensaje ya enviado para la orden {order_number} (ID {order_id})")
                    continue

                client_name = nombre_cliente(order_client_id)

                CLIENT.chat_postMessage(
                    channel = CHANNEL,
                    text = f"Orden: {order_number} | Cliente: {client_name} | Items: {items}"
                )
                print(f"Mensaje enviado con exito para la orden {order_number}")

                # Se guarda en el momento: si la corrida se corta a la mitad,
                # lo ya avisado no se repite en la proxima.
                enviadas[order_id] = datetime.now(ZoneInfo("Europe/London")).isoformat()
                guardar_estado(enviadas, last_run)
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
