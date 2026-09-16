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
SENT_ORDERS_FILE = os.getenv(
    "SENT_ORDERS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_orders.json"),
)
RETENTION_DAYS = 7


def cargar_enviadas():
    """IDs ya avisados, descartando los mas viejos que RETENTION_DAYS."""
    try:
        with open(SENT_ORDERS_FILE) as f:
            guardadas = json.load(f)
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as e:
        print(f"No se pudo leer {SENT_ORDERS_FILE} ({e}); se arranca vacio")
        return {}

    corte = datetime.now(ZoneInfo("Europe/London")) - timedelta(days=RETENTION_DAYS)
    vigentes = {}
    for order_id, enviado_en in guardadas.items():
        try:
            if datetime.fromisoformat(enviado_en) >= corte:
                vigentes[order_id] = enviado_en
        except (TypeError, ValueError):
            continue
    return vigentes


def guardar_enviadas(enviadas):
    """Escribe el estado de forma atomica para no corromperlo si se corta."""
    carpeta = os.path.dirname(SENT_ORDERS_FILE) or "."
    try:
        fd, tmp = tempfile.mkstemp(dir=carpeta, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(enviadas, f)
        os.replace(tmp, SENT_ORDERS_FILE)
    except OSError as e:
        print(f"No se pudo guardar {SENT_ORDERS_FILE}: {e}")


def es_wholesale(order):
    """True si la orden le corresponde al equipo de wholesale."""
    channel_id = order.get("ChannelId")

    if channel_id in WHOLESALE_CHANNELS:
        return True

    return (order.get("ClientId"), channel_id) in CLIENT_CHANNEL_EXCEPTIONS


enviadas = cargar_enviadas()
print(f"{len(enviadas)} ordenes ya avisadas en los ultimos {RETENTION_DAYS} dias")

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

    if uk_now.minute < 30:
        now = uk_now.replace(minute=0, second=0, microsecond=0)
    else:
        now = uk_now.replace(minute=30, second=0, microsecond=0)

    since_updated = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")

    print(f"Consultando órdenes que hayan sido packeadas desde {since_updated}")
    clientes_por_id = {c.get("ID"): c.get("Name") for c in ms_client.get_clients()}
    new_packed_orders = ms_client.get_orders(since_updated, status_id)

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
                guardar_enviadas(enviadas)
                time.sleep(1)

            except SlackApiError as e:
                print(f"Error de Slack API en la orden {order_number}: {e.response['error']}")

            except Exception as e:
                print(f"Error inesperado en la orden {order_number}: {e}")

except SlackApiError as e:
    print(f"Error de Slack API: {e.response['error']}")

except Exception as e:
    print(f"Error inesperado: {e}")
