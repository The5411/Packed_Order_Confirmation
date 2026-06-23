import os
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv
from datetime import datetime, timedelta
from clients.MintsoftClient import MintsoftOrderClient
import time

load_dotenv()

CLIENT = WebClient(token= os.getenv("SLACK_TOKEN"))
CHANNEL = "C0ASH80L065"

todays_orders = []
no_orders_notice = False

ms_client = MintsoftOrderClient()

try:
    
    status_id = 20
    if datetime.now().minute < 30:
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
    else:
        now = datetime.now().replace(minute=30, second=0, microsecond=0)

    since_updated = now + timedelta(minutes = 60) # Agrego 1h para que se ajuste al horario de Mintsoft
    
    print(f"Consultando órdenes que hayan sido packeadas desde {since_updated}")
    clients = ms_client.get_clients()
    new_packed_orders = ms_client.get_orders(since_updated, status_id)

    # Si esta vacio, significa no hay nuevas ordenes PACKED
    if not new_packed_orders:
        print("No se han pasado ordenes a Status PACKED")

    #Si tiene algo, es que hay nuevas ordenes en PACKED
    else:
        for order in new_packed_orders:
            order_id = order.get("ID")
            order_number = order.get("OrderNumber")
            order_client_id = order.get("ClientId")
            items = order.get("TotalItems")

            client_info = next((c for c in clients if c.get("ID") == order_client_id), None)
            client_name = client_info.get("Name")

            #Si hoy no se envio un mensaje al canal para esa orden:
            if order_number not in todays_orders:
                CLIENT.chat_postMessage(
                    channel = CHANNEL,
                    text = f"Orden: {order_number} | Cliente: {client_name} | Items: {items}"
                )
                print("Mensaje enviado con exito")
                #Almacenar el numero de orden
                todays_orders.append(order_number)
                time.sleep(1)

            else:
                print(f"Mensaje ya enviado para la orden {order_number}")

except SlackApiError as e:
    print(f"Error de Slack API: {e.response['error']}")

except Exception as e:
    print(f"Error inesperado: {e}")
