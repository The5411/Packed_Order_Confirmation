import os
import requests
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
import json
from datetime import datetime, timedelta
load_dotenv()



class MintsoftOrderClient:
    BASE_URL = "https://api.mintsoft.co.uk"

    def __init__(self):
        self.username = os.getenv("MINTSOFT_USERNAME")
        self.password = os.getenv("MINTSOFT_PASSWORD")

        if not all([self.username, self.password]):
            raise RuntimeError(
                "Missing Mintsoft credentials "
                "(MINTSOFT_USERNAME / MINTSOFT_PASSWORD)"
            )

        self.api_key = self._authenticate()

    def _authenticate(self) -> str:
        url = f"{self.BASE_URL}/api/Auth"

        payload = {
            "Username": self.username,
            "Password": self.password,
        }

        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        print(r.json())
        return r.json()

    def headers(self) -> Dict[str, str]:
        return {
            "ms-apikey": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    PAGE_SIZE = 100

    def get_orders(self, since_updated, status_id: Optional[int] = None,
                   warehouse_id: int = 3) -> List[Dict[str, Any]]:
        """Devuelve TODAS las ordenes en una sola lista, juntando las paginas.

        /api/Order/List corta en 100 filas por pagina. Ignora PageNumber,
        PageSize, Limit y Take: el unico parametro que funciona es PageNo,
        que arranca en 1. Se itera hasta que una pagina viene vacia o
        incompleta.
        """
        url = f"{self.BASE_URL}/api/Order/List"

        params: Dict[str, Any] = {"SinceLastUpdated": since_updated}
        if status_id is not None:
            params["OrderStatusId"] = status_id
            params["WarehouseId"] = warehouse_id

        todas: List[Dict[str, Any]] = []
        vistos = set()
        page_no = 1

        while True:
            r = requests.get(
                url,
                headers=self.headers(),
                params={**params, "PageNo": page_no},
                timeout=30,
            )

            r.raise_for_status()
            pagina = r.json()

            if not pagina:
                break

            # Si una orden se actualiza mientras paginamos, el orden de las
            # filas se corre y puede repetirse alguna entre paginas.
            nuevas = [o for o in pagina if o.get("ID") not in vistos]
            vistos.update(o.get("ID") for o in nuevas)
            todas.extend(nuevas)

            print(f"  PageNo={page_no}: {len(pagina)} filas ({len(nuevas)} nuevas)")

            # Una pagina incompleta ya es la ultima
            if len(pagina) < self.PAGE_SIZE:
                break

            page_no += 1

        print(f"  Total: {len(todas)} ordenes en {page_no} pagina(s)")
        return todas

    def get_clients(self) -> List[Dict[str, Any]]:
        r = requests.get(
            f"{self.BASE_URL}/api/Client",
            headers=self.headers(),
            timeout=30
        )

        if not r.ok:
            print(f"Error HTTP {r.status_code}")
            print(f"Respuesta del servidor: {r.text}")
            return []
        
        return r.json()

    def get_client(self, client_id: int) -> Optional[Dict[str, Any]]:
        """Trae un cliente puntual.

        /api/Client se topa en 100 filas e ignora PageNumber, asi que hay
        clientes reales que nunca aparecen en esa lista. Para esos hay que
        pedirlos de a uno.
        """
        r = requests.get(
            f"{self.BASE_URL}/api/Client/{client_id}",
            headers=self.headers(),
            timeout=30,
        )

        if not r.ok:
            print(f"Error HTTP {r.status_code} al buscar el cliente {client_id}")
            return None

        data = r.json()
        return data if isinstance(data, dict) and data.get("Name") else None
