# ModBerry Manager

Interface web de gestion de parc **ModBerry X-500 CM5** : découverte LAN, changement d'IP,
et **déploiement de ThingsBoard Edge** sur chaque carte depuis le navigateur.

- URL : `http://<ip-du-serveur>:2310`
- Identifiant par défaut : `admin-ip@modberry.local` / `takieddine`

---

## Objectif : déployer ThingsBoard Edge sur plusieurs cartes

La stratégie visée est le déploiement en masse à partir d'une image maître :

```
Master ModBerry CM5 (10.0.0.26)
  └─ Docker + ThingsBoard Edge configuré depuis cette interface
       └─ Création d'une Master Image (dd / rpi-clone)
            └─ Flash en masse sur les autres CM5
                 └─ First-Boot Provisioning  → identité unique par carte
                      └─ Enregistrement automatique dans le Tenant ThingsBoard
```

Cette interface couvre le cycle complet : scan du LAN → détection de l'état de l'Edge →
création de l'Edge côté serveur si absent → provisioning de la carte avec sa propre
paire **Clé / Secret**.

---

## Étape 1 — Découverte et état de l'Edge

Le scan LAN (`Scanner maintenant`) détecte les hôtes joignables et identifie les ModBerry
(hostname `techbase` / `modberry`, ou serial CM5 lisible).

La colonne **ThingsBoard Edge** du dashboard indique l'état de chaque carte :

| Badge | Signification |
|---|---|
| `Connecte au serveur` | Le conteneur tourne **et** les logs confirment la liaison au serveur |
| `Demarrage / non connecte` | Conteneur démarré, liaison pas encore établie (ou clé refusée) |
| `Edge arrete` | Stack présente mais conteneur arrêté |
| `Non configure` | Docker présent, aucun `.env` Edge |
| `Docker absent` | Docker n'est pas installé sur la carte |
| `Injoignable` | SSH indisponible |

Bouton **Configurer Edge** → page dédiée `/edge/<ip>`.

### Ce que la sonde vérifie réellement (via SSH)

- version de Docker, disponibilité de `docker compose`, service `docker` actif
- présence et contenu de `/opt/tb-edge/.env` (clé Edge, serveur cible, version)
- état des conteneurs `tb-edge` et `tb-edge-postgres`
- logs `tb-edge` filtrés pour détecter `connected to cloud`, `UNAUTHORIZED`,
  `Failed to establish`, etc. → c'est ce qui distingue « démarré » de « réellement connecté »

---

## Étape 2 — Créer l'Edge côté serveur (si la carte n'en a pas)

Sur la page `/edge/<ip>`, section **1. Serveur ThingsBoard** :

1. Renseignez l'URL du serveur (ex. `http://10.0.0.1:8080`) et vos identifiants **tenant**.
   Ces identifiants servent uniquement à l'appel en cours, ils ne sont jamais stockés.
2. Trois actions :
   - **Lister les Edges du tenant** — voir tous les Edges existants et lesquels sont actifs
   - **Verifier la Cle saisie** — savoir si une Clé donnée correspond à un Edge existant
   - **Creer un nouvel Edge** — crée l'Edge dans le tenant et **récupère automatiquement
     sa Clé et son Secret**, reportés directement dans le formulaire de déploiement

Le nom proposé par défaut est dérivé du serial de la carte (ex. `modberry-146ee0b1`),
ce qui garantit l'unicité dans le tenant.

> Le bouton **Utiliser** de chaque ligne recopie la Clé/Secret d'un Edge existant
> vers le formulaire de déploiement.

---

## Étape 3 — Provisionner la carte

Section **2. Identifiants Edge et deploiement** :

| Champ | Exemple |
|---|---|
| Clé Edge (cloud routing key) | `1666b31c-e954-04e6-a19d-01adbeaa6eae` |
| Secret Edge (cloud routing secret) | `yxsjali73u4ypuktq4fb` |
| Hôte du serveur ThingsBoard | `10.0.0.1` |
| Port RPC edge | `7070` |

La Clé est validée comme UUID et le Secret comme chaîne alphanumérique (8–64 car.)
avant tout envoi sur la carte.

**Options avancées** : version de l'image `tb-edge`, répertoire d'installation, ports
HTTP/MQTT locaux, mot de passe Postgres, et les bascules :

- **Purger la base locale** — efface `/var/lib/tb-edge/db`. **Indispensable après clonage
  d'une Master Image** : sans cela la carte réutilise l'identité Edge de la carte maître.
- **Definir un hostname unique depuis le serial** — `modberry-<8 derniers car. du serial>`
- **Installer Docker si absent** — installe Docker via `get.docker.com`
- **Telecharger les images Docker** / **Demarrer l'Edge a la fin**

Le déploiement s'exécute en tâche de fond ; le **Journal** affiche la sortie du script
en direct (polling incrémental), y compris le `docker compose pull` qui peut durer
plusieurs minutes sur ARM64.

À la fin, une sonde automatique est relancée et l'état est mis à jour.

### Ce que le provisioning écrit sur la carte

```
/opt/tb-edge/.env                 identité unique (clé, secret, serveur) — chmod 600
/opt/tb-edge/docker-compose.yml   stack tb-edge + postgres 16
/var/lib/tb-edge/db               données Postgres
/var/lib/tb-edge/logs             logs tb-edge
```

Le script est **idempotent** : il arrête la stack existante, réécrit la configuration,
puis redémarre. Il peut être relancé sans risque.

---

## Étape 4 — Master Image et déploiement en masse

1. Configurez et validez **une** carte maître (ici `10.0.0.26`) jusqu'au badge
   `Connecte au serveur`.
2. Arrêtez la stack (`Arreter`) et créez l'image disque de la carte.
3. Flashez l'image sur les autres CM5.
4. Pour chaque nouvelle carte : scan → **Configurer Edge** → créer son Edge dans le tenant
   → déployer avec **Purger la base locale** et **hostname unique** cochés.

Chaque carte obtient ainsi sa propre identité et s'enregistre séparément dans le tenant.

> Le script `assets/edge_provision.sh` est autonome : il peut aussi être appelé par un
> service systemd `first-boot` sur l'image clonée, avec les variables d'environnement
> `EDGE_KEY`, `EDGE_SECRET`, `CLOUD_RPC_HOST`, `RESET_DATA=1`, `SET_HOSTNAME=1`.

---

## Supervision

Depuis la page `/edge/<ip>` :

- **Verifier maintenant** — relance la sonde SSH
- **Demarrer / Redemarrer / Arreter** — pilote la stack docker compose
- **Voir les logs** — 200 dernières lignes du conteneur `tb-edge`
- Lien vers l'interface Edge locale `http://<ip>:8080`

---

## API

| Méthode | Route | Rôle |
|---|---|---|
| `GET/POST` | `/api/edge/<ip>/probe` | État complet de l'Edge sur la carte |
| `POST` | `/api/edge/<ip>/deploy` | Lance le provisioning (tâche de fond) |
| `GET` | `/api/edge/<ip>/job?offset=N` | Logs incrémentaux du déploiement |
| `POST` | `/api/edge/<ip>/control` | `start` / `stop` / `restart` / `down` |
| `GET` | `/api/edge/<ip>/logs?lines=200` | Logs du conteneur `tb-edge` |
| `GET/POST` | `/api/edge/settings` | Réglages Edge globaux |
| `GET` | `/api/edge/states` | Derniers états connus, par IP |
| `POST` | `/api/tb/edges` | Liste les Edges du tenant |
| `POST` | `/api/tb/edge/lookup` | Cherche un Edge par clé ou par nom |
| `POST` | `/api/tb/edge/create` | Crée un Edge, renvoie clé + secret |
| `GET` | `/api/devices`, `/api/modberries`, `/api/networks` | Inventaire du scan |
| `POST` | `/scan` | Déclenche un scan réseau |

---

## Installation

```bash
sudo ./install.sh
```

Crée le virtualenv, installe les dépendances et le service systemd
`modberry_manager.service` (port 2310).

```bash
systemctl status modberry_manager.service
journalctl -u modberry_manager.service -f
```

### Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `MODBERRY_PORT` | `2310` | Port d'écoute |
| `MODBERRY_SECRET_KEY` | — | Clé de session Flask (**à définir en production**) |
| `MODBERRY_SSH_USER` | `root` | Utilisateur SSH des cartes |
| `MODBERRY_SSH_PASSWORD` | `techbase` | Mot de passe SSH des cartes |
| `MODBERRY_TB_HOST` | — | Hôte ThingsBoard par défaut |
| `MODBERRY_TB_RPC_PORT` | `7070` | Port RPC edge par défaut |
| `MODBERRY_TB_URL` | — | URL web du serveur ThingsBoard |
| `MODBERRY_AUTO_SCAN_INTERVAL_SECONDS` | `7200` | Intervalle du scan automatique |

---

## Structure

```
app.py                      application Flask + routes Edge
tb_edge.py                  sonde SSH et provisioning des cartes
tb_cloud.py                 client REST du serveur ThingsBoard (tenant)
jobs.py                     tâches de fond avec logs incrémentaux
assets/edge_provision.sh    script exécuté sur la carte (idempotent, first-boot)
templates/edge.html         page de configuration ThingsBoard Edge
templates/dashboard.html    inventaire + colonne d'état Edge
config.json                 état persisté (scan, réglages, états Edge)
```

## Prérequis sur les cartes

- SSH accessible avec les identifiants configurés
- `sudo` sans mot de passe si l'utilisateur SSH n'est pas `root`
- accès réseau au serveur ThingsBoard sur le port RPC (7070) et à Docker Hub pour le `pull`
