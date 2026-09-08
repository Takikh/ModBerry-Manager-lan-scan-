#!/usr/bin/env python3
"""
tb_cloud.py - Client REST minimal pour le serveur ThingsBoard (cote Tenant).

Permet depuis l'interface web :
  - de se connecter au serveur ThingsBoard (login tenant),
  - de lister les Edge instances du tenant,
  - de retrouver un Edge par sa Cle (routing key) et de savoir s'il est actif,
  - de creer un nouvel Edge et de recuperer sa Cle / son Secret,
  - de recuperer la Cle/Secret d'un Edge existant.

Ainsi, si une carte n'a pas d'Edge, on peut le creer depuis le web puis
provisionner la carte avec les identifiants generes.
"""

import requests

REQUEST_TIMEOUT = 15


class ThingsBoardError(Exception):
    pass


class ThingsBoardClient:
    def __init__(self, base_url, username=None, password=None, verify_ssl=True, token=None):
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise ThingsBoardError("URL du serveur ThingsBoard manquante.")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = f"http://{self.base_url}"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.token = token
        self.refresh_token = None
        self.session = requests.Session()

    # ------------------------------------------------------------- helpers
    def _headers(self):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["X-Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                headers=self._headers(),
                timeout=kwargs.pop("timeout", REQUEST_TIMEOUT),
                verify=self.verify_ssl,
                **kwargs,
            )
        except requests.exceptions.SSLError as exception:
            raise ThingsBoardError(f"Erreur SSL: {exception}") from exception
        except requests.exceptions.ConnectionError:
            raise ThingsBoardError(f"Serveur ThingsBoard injoignable ({url}).")
        except requests.exceptions.Timeout:
            raise ThingsBoardError("Delai depasse en interrogeant le serveur ThingsBoard.")

        if response.status_code == 401:
            raise ThingsBoardError("Identifiants ThingsBoard refuses ou session expiree (401).")
        if response.status_code == 403:
            raise ThingsBoardError("Acces refuse par ThingsBoard (403) - droits tenant requis.")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("message", "")
            except Exception:
                detail = response.text[:200]
            raise ThingsBoardError(f"ThingsBoard a repondu {response.status_code}: {detail}")

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    # --------------------------------------------------------------- auth
    def login(self):
        payload = {"username": self.username, "password": self.password}
        data = self._request("POST", "/api/auth/login", json=payload)
        if not data or "token" not in data:
            raise ThingsBoardError("Connexion ThingsBoard echouee: token absent.")
        self.token = data["token"]
        self.refresh_token = data.get("refreshToken")
        return self.token

    def me(self):
        return self._request("GET", "/api/auth/user") or {}

    # -------------------------------------------------------------- edges
    def list_edges(self, page_size=200, page=0, text_search=None):
        path = f"/api/tenant/edges?pageSize={page_size}&page={page}&sortProperty=name&sortOrder=ASC"
        if text_search:
            path += f"&textSearch={requests.utils.quote(text_search)}"
        data = self._request("GET", path) or {}
        return data.get("data", [])

    def get_edge(self, edge_id):
        return self._request("GET", f"/api/edge/{edge_id}")

    def get_edge_credentials(self, edge_id):
        """Retourne {'routingKey': ..., 'secret': ...} pour un Edge existant."""
        edge = self.get_edge(edge_id)
        if not edge:
            return None
        return {
            "routingKey": edge.get("routingKey"),
            "secret": edge.get("secret"),
            "name": edge.get("name"),
            "type": edge.get("type"),
        }

    def find_edge_by_routing_key(self, routing_key):
        routing_key = (routing_key or "").strip().lower()
        if not routing_key:
            return None
        for edge in self.list_edges():
            if (edge.get("routingKey") or "").strip().lower() == routing_key:
                return edge
        return None

    def find_edge_by_name(self, name):
        name = (name or "").strip().lower()
        if not name:
            return None
        for edge in self.list_edges(text_search=name):
            if (edge.get("name") or "").strip().lower() == name:
                return edge
        return None

    def create_edge(self, name, edge_type="default", label=None, routing_key=None, secret=None):
        """
        Cree un Edge dans le tenant. Si routing_key/secret ne sont pas fournis,
        ThingsBoard les genere automatiquement et les renvoie dans la reponse.
        """
        payload = {"name": name, "type": edge_type or "default"}
        if label:
            payload["label"] = label
        if routing_key:
            payload["routingKey"] = routing_key
        if secret:
            payload["secret"] = secret

        data = self._request("POST", "/api/edge", json=payload)
        if not data:
            raise ThingsBoardError("Creation de l'Edge echouee (reponse vide).")
        return data

    def delete_edge(self, edge_id):
        self._request("DELETE", f"/api/edge/{edge_id}")
        return True

    def is_edge_active(self, edge_id):
        """Lit l'attribut serveur 'active' de l'Edge."""
        path = f"/api/plugins/telemetry/EDGE/{edge_id}/values/attributes/SERVER_SCOPE"
        data = self._request("GET", path) or []
        for attribute in data:
            if attribute.get("key") == "active":
                return bool(attribute.get("value"))
        return False

    def edge_summary(self, edge):
        """Normalise un objet Edge ThingsBoard pour l'interface."""
        edge_id = (edge.get("id") or {}).get("id")
        active = None
        try:
            active = self.is_edge_active(edge_id) if edge_id else None
        except ThingsBoardError:
            active = None
        return {
            "id": edge_id,
            "name": edge.get("name"),
            "type": edge.get("type"),
            "label": edge.get("label"),
            "routing_key": edge.get("routingKey"),
            "secret": edge.get("secret"),
            "active": active,
            "created_time": edge.get("createdTime"),
        }


def connect(base_url, username, password, verify_ssl=True):
    """Raccourci : cree un client et se connecte."""
    client = ThingsBoardClient(base_url, username, password, verify_ssl=verify_ssl)
    client.login()
    return client
