# ModBerry Manager

Interface web de gestion de parc **ModBerry X-500 CM5** : découverte LAN, changement d'IP,
et **provisioning de ThingsBoard Edge** sur chaque carte depuis le navigateur.

- URL : `http://<ip-du-serveur>:2310`
- Identifiant par défaut : `admin-ip@modberry.local` / `takieddine`

Cet outil automatise la procédure de réplication décrite dans le rapport de provisioning :
transfert de la stack applicative depuis la carte de référence, génération d'une identité
Edge unique par carte, et supervision — l'objectif étant le déploiement sur 99+ cartes.

---

## Architecture de référence

Carte maître **techbase** (`10.0.0.26`), pleinement fonctionnelle :

| Élément | Valeur |
|---|---|
| OS | Debian 12 bookworm, aarch64 |
| Matériel | Raspberry Pi Compute Module 5 Rev 1.0 |
| Docker | Docker CE (dépôt `download.docker.com`) |
| Projet compose | `tbedge-mobilis` dans `/root/tb-edge-mobilis` |
| Image Edge | `thingsboard/tb-edge:4.3.1.3EDGE-mobilis` (**custom**, ~2,4 Go) |
| Base | `postgres:16`, non exposée sur l'hôte |
| Ports hôte | HTTP `8082` → 8080, MQTT `1884` → 1883, CoAP `5683-5688/udp` |
| Serveur ThingsBoard | `10.0.0.1`, port RPC **`7071`** |
| Gateway I/O | `/opt/mobilis-gateway` + `tb-edge-io.service` (natif, hors Docker) |

Ces valeurs sont les **défauts** de l'interface — inutile de les ressaisir.

> Le port MQTT hôte est `1884` car `1883` est déjà occupé par le broker mosquitto
> de la stack ChirpStack présente sur la carte maître. Le port RPC est `7071`, pas 7070.

---

## Deux contraintes structurantes

### L'image Edge est customisée

Le tag `4.3.1.3EDGE-mobilis` n'existe dans aucun registre public : un `docker pull` sur une
carte neuve échoue. L'image doit être transférée depuis la carte maître par
`docker save` → `docker load`. L'interface le fait automatiquement (§ Étape 1).

### Les données ne doivent jamais être répliquées

Le clonage disque complet (`dd`) a été **abandonné** : il duplique la base PostgreSQL de
l'Edge, donc son identité et son historique. Deux Edges partageant la même base entrent en
conflit direct sur le serveur (mêmes UUIDs internes).

L'outil ne réplique donc que la **configuration** ; les volumes de données sont recréés
vides sur chaque carte. Un garde-fou refuse en `409` toute tentative de réutiliser :

- la routing key de la carte maître (`b534512b-…`)
- une routing key déjà affectée à une autre carte du parc

---

## Étape 0 — Découverte et état

Le scan LAN (`Scanner maintenant`) détecte les hôtes joignables et identifie les ModBerry.
La colonne **ThingsBoard Edge** du dashboard donne l'état de chaque carte :

| Badge | Signification |
|---|---|
| `Connecte au serveur` | Conteneur actif **et** logs confirmant la liaison |
| `Demarrage / non connecte` | Conteneur démarré, liaison pas établie (ou clé refusée) |
| `Edge arrete` | Stack présente, conteneur arrêté |
| `Non configure` | Docker et image présents, pas de `.env` |
| `Image edge absente` | Docker présent, image custom manquante → réplication requise |
| `Docker absent` | Docker CE non installé |
| `Injoignable` | SSH indisponible |

Bouton **Configurer Edge** → page `/edge/<ip>`. Une carte absente du dernier scan reste
accessible en saisie directe par son IP (utile pour une carte neuve).

### Ce que la sonde vérifie (via SSH)

OS et modèle · version Docker · **emplacement du stockage Docker** · **présence de l'image
custom** · conteneurs `tb-edge` / `tb-edge-postgres` · contenu de `.env` · logs `tb-edge`
(`connected to cloud`, `UNAUTHORIZED`, `Failed to establish`) · état de `tb-edge-io.service`
· **comptage des erreurs GPIO** dans `/var/log/mobilis-gateway.log`.

---

## Étape 1 — Carte de référence

Section **1. Carte de reference** : bouton **Inventorier** → liste les images `tb-edge`
disponibles sur la carte maître avec leur taille, l'état du compose et de la gateway.

Le transfert d'image (`docker save | gzip` → SFTP → `docker load`) est déclenché
automatiquement au déploiement si l'image manque sur la cible. Progression affichée en
direct ; comptez plusieurs dizaines de minutes pour ~2,4 Go.

---

## Étape 2 — Identité Edge dans le tenant

Section **2. Serveur ThingsBoard** : renseignez l'URL et vos identifiants **tenant**
(utilisés pour l'appel en cours uniquement, jamais stockés), puis :

- **Lister les Edges du tenant** — voir les Edges existants et lesquels sont actifs
- **Verifier la Cle saisie** — savoir si une Clé correspond à un Edge existant
- **Creer un nouvel Edge** — crée l'Edge et **récupère automatiquement sa Clé et son
  Secret**, reportés dans le formulaire de déploiement

Le nom proposé est dérivé du serial de la carte, garantissant l'unicité dans le tenant.
Cela remplace la création manuelle via l'interface web, non tenable sur 99+ cartes.

> En cas d'erreur `Invalid username or password`, vérifiez les identifiants tenant du
> serveur `10.0.0.1` — c'est le blocage rencontré lors de la session manuelle.

---

## Étape 3 — Déploiement

| Champ | Exemple |
|---|---|
| Clé Edge (cloud routing key) | `1666b31c-e954-04e6-a19d-01adbeaa6eae` |
| Secret Edge (cloud routing secret) | `yxsjali73u4ypuktq4fb` |
| Hôte du serveur ThingsBoard | `10.0.0.1` |
| Port RPC edge | `7071` |

Le déploiement enchaîne trois étapes dans une seule action, avec journal en direct :

1. **Image Docker custom** — `docker save` sur le maître → `docker load` sur la cible
   (ignoré si l'image est déjà présente)
2. **Stack** — installation de Docker CE si absent, écriture de `.env` (chmod 600) et
   `docker-compose.yml`, récupération de `postgres:16`, `docker compose up -d`
3. **Gateway I/O** — copie de `/opt/mobilis-gateway`, installation de `tb-edge-io.service`

À la fin, une sonde automatique met l'état à jour.

### Options avancées

Répertoire, projet compose, ports hôte, mot de passe Postgres, **stockage Docker sur SSD**
(`/mnt/ssd/docker`, comme sur la carte maître), et les bascules :

- **Purger les volumes de donnees** — `docker compose down -v` + suppression des volumes.
  **Indispensable** sur une carte issue d'une image clonée.
- **Hostname unique derive du serial** — `modberry-<8 derniers car. du serial CM5>`
- **Installer Docker CE si absent** — dépôt officiel Debian, arch. détectée (arm64)
- **Transferer la gateway I/O** / **Installer l'unite tb-edge-io.service**
- **Demarrer la gateway** — **décoché par défaut**, voir ci-dessous

### Installation Docker CE

Reproduit exactement la procédure de la carte maître : `ca-certificates curl gnupg`, clé
GPG dans `/etc/apt/keyrings/docker.asc`, dépôt
`deb [arch=arm64 signed-by=…] …/debian bookworm stable`, puis `docker-ce docker-ce-cli
containerd.io docker-buildx-plugin docker-compose-plugin`.

---

## Bug GPIO de la gateway — non corrigé

La gateway génère des erreurs répétées `Device or resource busy` et
`This port is not configure yet`, vraisemblablement un conflit d'ouverture de port GPIO
dans `edge_gateway.py`.

En conséquence :

- le service est **installé mais pas démarré** par défaut après transfert
- la sonde **compte ces erreurs** et la page affiche un avertissement dédié
- ce bug doit être corrigé **avant** toute containerisation de la gateway
  (ne pas le masquer avec `privileged: true`)

Boutons dédiés sur la page : démarrer / redémarrer / arrêter la gateway et consulter ses logs.

---

## Déploiement en masse

1. Validez la carte maître jusqu'au badge `Connecte au serveur`.
2. Préparez une image système **avec Docker CE pré-installé mais sans identité Edge**
   (pas de `.env`, pas de volumes de données), puis flashez-la sur les autres CM5.
3. Pour chaque carte : **Configurer Edge** → créer son Edge dans le tenant → déployer
   avec **Purger les volumes** et **Hostname unique** cochés.

Le registre `/api/edge/assignments` liste les identités déjà affectées et empêche tout doublon.

> `assets/edge_provision.sh` est autonome et idempotent : il peut servir de service
> systemd `first-boot` sur l'image clonée, via `EDGE_KEY`, `EDGE_SECRET`,
> `CLOUD_RPC_HOST`, `RESET_DATA=1`, `SET_HOSTNAME=1`.

---

## Ce qui n'est pas géré

- La stack **ChirpStack** de la carte maître (chirpstack, gateway-bridge, mosquitto,
  postgres:14, redis:7) n'est pas répliquée — hors périmètre.
- La **containerisation de la gateway** est reportée jusqu'à correction du bug GPIO.

---

## API

| Méthode | Route | Rôle |
|---|---|---|
| `GET/POST` | `/api/edge/<ip>/probe` | État complet de l'Edge |
| `POST` | `/api/edge/<ip>/deploy` | Séquence image → stack → gateway |
| `GET` | `/api/edge/<ip>/job?offset=N` | Logs incrémentaux |
| `POST` | `/api/edge/<ip>/control` | `start` / `stop` / `restart` / `down` |
| `GET` | `/api/edge/<ip>/logs` | Logs du conteneur `tb-edge` |
| `GET/POST` | `/api/master/inspect` | Inventaire de la carte de référence |
| `GET` | `/api/edge/<ip>/gateway` | État de la gateway I/O |
| `POST` | `/api/edge/<ip>/gateway/transfer` | Transfert de la gateway seule |
| `POST` | `/api/edge/<ip>/gateway/control` | `start`/`stop`/`restart`/`enable`/`status` |
| `GET` | `/api/edge/<ip>/gateway/logs` | Logs de la gateway |
| `GET` | `/api/edge/assignments` | Registre des identités affectées |
| `GET/POST` | `/api/edge/settings` | Réglages globaux |
| `GET` | `/api/edge/states` | Derniers états connus |
| `POST` | `/api/tb/edges` | Liste les Edges du tenant |
| `POST` | `/api/tb/edge/lookup` | Recherche par clé ou nom |
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
| `MODBERRY_MASTER_IP` | `10.0.0.26` | Carte de référence |
| `MODBERRY_TB_HOST` | `10.0.0.1` | Serveur ThingsBoard |
| `MODBERRY_TB_RPC_PORT` | `7071` | Port RPC edge |
| `MODBERRY_TB_URL` | — | URL web du serveur ThingsBoard |
| `MODBERRY_AUTO_SCAN_INTERVAL_SECONDS` | `7200` | Intervalle du scan automatique |

---

## Structure

```
app.py                      application Flask + routes Edge / master / gateway
tb_edge.py                  sonde SSH, provisioning, garde-fou d'identité
tb_master.py                réplication depuis la carte de référence
tb_cloud.py                 client REST du serveur ThingsBoard (tenant)
jobs.py                     tâches de fond avec logs incrémentaux
assets/edge_provision.sh    script exécuté sur la carte (idempotent, first-boot)
templates/edge.html         page de provisioning en 3 étapes
templates/dashboard.html    inventaire + colonne d'état Edge
config.json                 état persisté (scan, réglages, états, identités)
```

## Prérequis sur les cartes

- SSH accessible avec les identifiants configurés
- `sudo` sans mot de passe si l'utilisateur SSH n'est pas `root`
- accès réseau au serveur ThingsBoard (port RPC `7071`)
- accès à Docker Hub pour `postgres:16` (l'image Edge, elle, vient de la carte maître)
