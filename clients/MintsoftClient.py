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

    def get_orders(self, since_updated, status_id: Optional[int] = None) -> List[Dict[str, Any]]:
        url = f"{self.BASE_URL}/api/Order/List"

        if status_id is not None:
            url += f"?OrderStatusId={status_id}&WarehouseId=3&SinceLastUpdated={since_updated}"
            
        r = requests.get(
            url,
            headers=self.headers(),
            timeout=30,
        )

        r.raise_for_status()
        return r.json()
    
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
