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
| `Base postgres absente` | Image `postgres:16` manquante → `localhost:5432 refused` garanti |
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

## Étape 0 bis — Prérequis et dépendances (obligatoire)

Section **0. Prerequis et dependances** → bouton **Verifier les prerequis**.

Cette couche existe pour une raison précise : un déploiement a échoué **après 6 minutes de
transfert** parce que la carte cible n'avait pas Docker :

```
[16:31:14] Chargement de l'image sur la cible (docker load)...
[16:31:15] bash: line 1: docker: command not found
[16:31:15] docker load a echoue (code 127).
```

L'archive de ~1 Go était déjà poussée. Désormais, **rien de long ne démarre avant que les
dépendances soient vérifiées.**

### Ce qui est contrôlé

| Portée | Vérifications |
|---|---|
| **Carte cible** | SSH, root/`sudo -n`, OS+arch, **Docker CE**, plugin compose, **daemon actif**, image edge présente, outils (`gzip`, `gunzip`, `tar`, `awk`, `sed`, `systemctl`), espace disque (8 Go min, 12 Go conseillés), répertoire d'installation |
| **Carte de référence** | SSH, Docker, **présence de l'image à répliquer**, espace `/tmp` (2 Go), `gzip`/`stat` |
| **Serveur ModBerry** | `assets/edge_provision.sh`, modules `paramiko`/`requests`, espace `/tmp` (2 Go, relais de l'archive) |

Si Docker doit être installé, on vérifie en plus `curl`/`apt-get`/`dpkg` et la joignabilité
de `download.docker.com`.

Chaque contrôle est classé **OK** / **Attention** / **Bloquant**, avec le correctif à
appliquer. Un contrôle bloquant **annule le déploiement avant tout transfert**.

### Installer Docker séparément

Bouton **Installer Docker CE sur la carte** : exécute `edge_provision.sh` en mode
`DOCKER_ONLY=1` — installe `docker-ce`, le plugin compose, démarre le daemon, déplace
éventuellement le data-root sur le SSD, **puis s'arrête** sans toucher à la stack Edge.
Étape indépendante et rejouable.

Le déploiement complet fait la même chose automatiquement : l'ordre des étapes place
l'installation de Docker **avant** le transfert de l'image.

---

## Étape 0 ter — Base locale postgres (dépendance obligatoire)

Section **Base locale postgres** → bouton **Installer / répliquer postgres**.

Deuxième panne rencontrée en production. Le conteneur `tb-edge` démarrait puis mourait :

```
ERROR o.h.e.jdbc.spi.SqlExceptionHelper - Connection to localhost:5432 refused.
Caused by: java.net.ConnectException: Connection refused
```

Deux causes cumulées :

1. **L'image postgres n'était jamais arrivée.** Le script se contentait d'un
   `docker pull postgres:16` avec un simple `AVERTISSEMENT` en cas d'échec — sur un parc
   sans accès internet, l'image manquait silencieusement.
2. **L'Edge avait été lancé à la main** (`docker run`), donc sans la stack compose : pas de
   conteneur `tb-edge-postgres`, pas de `SPRING_DATASOURCE_URL`, d'où `localhost:5432`
   au lieu de `postgres:5432`.

### Ce qui a changé

- **`postgres` est répliquée depuis la carte de référence** (`docker save` → `docker load`),
  exactement comme l'image edge custom. Ordre d'essai : déjà présente → `docker pull` →
  **réplication depuis la carte de référence**.
- **L'absence de postgres est bloquante** : le script s'arrête avec un message explicite
  (`exit 1`) au lieu de continuer vers un échec inévitable.
- Le badge dashboard **`Base postgres absente`** signale le cas avant tout démarrage.

---

## Démarrage automatique

L'Edge démarre désormais **seul**, sans `docker run` manuel.

### À la fin du déploiement

`docker compose up -d`, puis deux attentes actives :

| Attente | Condition | Défaut |
|---|---|---|
| Base prête | `pg_isready -U postgres -d tb_edge` | `WAIT_DB=180` s |
| Edge démarré | `Started ThingsBoardEdge` dans les logs | `WAIT_EDGE=300` s |

Si la base ne répond pas, le script affiche **l'état et les logs de postgres** puis échoue —
plus besoin de lire 300 lignes de stacktrace Java. Si l'edge ne confirme pas son démarrage,
les lignes `refused` / `ERROR` / `FATAL` / `UNAUTHORIZED` sont extraites automatiquement.

### Après un redémarrage de la carte

`restart: always` ne suffit pas si `docker.service` n'est pas activé au boot. Le
déploiement (option **Relancer l'Edge automatiquement au demarrage**, cochée par défaut) :

- active `docker.service` et `containerd`
- installe l'unité **`tb-edge-stack.service`** (oneshot) qui rejoue `compose up -d` au boot
  — utile quand le SSD du data-root est monté après docker

Boutons **Activer la relance au boot** / **Désactiver** pour piloter cela seul.

---

## Diagnostic

Section **Diagnostic — pourquoi l'Edge ne tourne pas** → bouton **Diagnostiquer**.

Rassemble en un appel ce qu'il fallait chercher à la main en SSH, avec le correctif
de chaque problème :

- images edge et **postgres** présentes ou non
- tous les conteneurs, **y compris ceux lancés par `docker run`** (noms générés du type
  `loving_lamport`) — ils tournent sans base ni `.env` et bloquent les ports
- santé de postgres (`pg_isready`) et ses logs
- causes d'erreur reconnues dans les logs de l'edge : `5432 refused`, `UNAUTHORIZED`,
  serveur RPC injoignable, port déjà utilisé, `OutOfMemoryError`, disque plein
- ports hôte occupés par un autre service, état de la relance au boot

Bouton **Nettoyer les conteneurs manuels** : supprime les conteneurs edge hors stack
(`tb-edge` et `tb-edge-postgres` ne sont jamais touchés).

---

## Étape 1 — Carte de référence

Section **1. Carte de reference** : bouton **Inventorier** → liste les images `tb-edge`
disponibles sur la carte maître avec leur taille, l'état du compose et de la gateway.

Le transfert d'image (`docker save | gzip` → SFTP → `docker load`) est déclenché
automatiquement au déploiement si l'image manque sur la cible. Progression affichée en
direct ; comptez plusieurs dizaines de minutes pour ~2,4 Go.

Avant chaque transfert (et une seconde fois juste avant `docker load`), la présence de
Docker, du daemon, de `gunzip` et de l'espace disque est revérifiée sur la cible.

### Gérer l'image sur la carte

Section **Image edge sur cette carte** : le stockage des CM5 ne permet pas de conserver
plusieurs images de ~2,4 Go.

- **Lister les images** — images `tb-edge` présentes, taille, date, espace libre
- **Supprimer** — arrête la stack (`docker compose down`), retire les conteneurs qui
  utilisent l'image, puis `docker rmi -f`. Le `.env` et l'identité Edge **ne sont pas
  touchés**
- **Transferer l'image manquante** — transfert simple, sans reconfigurer la stack
- **Remplacer l'image** — supprime l'image existante puis la recharge depuis la carte de
  référence (équivalent de l'option **Forcer le retransfert** du déploiement)

Sans suppression ni `force`, une image déjà présente n'est jamais retransférée.

---

## Étape 2 — Identité Edge dans le tenant

Section **2. Serveur ThingsBoard** : l'URL et les identifiants **tenant** sont pré-remplis
avec les valeurs du parc Mobilis — `tenant@mobilis.dz` / `tenant`. Le mot de passe est
utilisé pour l'appel en cours uniquement et **n'est jamais stocké** (seul l'identifiant est
mémorisé). Surchargeables par `MODBERRY_TB_USERNAME` / `MODBERRY_TB_PASSWORD`.

Ensuite :

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

Le déploiement enchaîne cinq étapes dans une seule action, avec journal en direct.
L'ordre est important : **Docker est installé avant que l'image ne soit transférée.**

1. **Prérequis** — contrôle complet (serveur, carte de référence, cible). Un point
   bloquant arrête le déploiement **immédiatement**, avant tout transfert
2. **Dépendances Docker** — installe Docker CE / démarre le daemon si nécessaire
   (mode `DOCKER_ONLY`). C'est ce qui évite l'échec `docker load` code 127
3. **Image Docker custom** — `docker save` sur le maître → `docker load` sur la cible
   (ignoré si l'image est déjà présente, sauf **Forcer le retransfert**)
4. **Base locale postgres** — pull, ou **réplication depuis la carte de référence**.
   Bloquant : sans elle, l'edge échoue sur `localhost:5432 refused`
5. **Stack** — écriture de `.env` (chmod 600) et `docker-compose.yml`,
   `docker compose up -d`, puis **attente que postgres soit prête et que l'edge démarre**

Puis, en complément : **Gateway I/O** — copie de `/opt/mobilis-gateway`, installation de
`tb-edge-io.service`. À la fin, une sonde automatique met l'état à jour.

### Options avancées

Répertoire, projet compose, ports hôte, mot de passe Postgres, **stockage Docker sur SSD**
(`/mnt/ssd/docker`, comme sur la carte maître), et les bascules :

- **Verifier les prerequis avant de commencer** — coché par défaut, à ne décocher qu'en
  connaissance de cause
- **Purger les volumes de donnees** — `docker compose down -v` + suppression des volumes.
  **Indispensable** sur une carte issue d'une image clonée.
- **Forcer le retransfert** — supprime l'image existante sur la carte et la recharge
- **Garantir la base postgres** — coché par défaut, ne pas décocher sans raison
- **Relancer l'Edge automatiquement au demarrage** — `docker.service` + `tb-edge-stack.service`
- **Hostname unique derive du serial** — `modberry-<8 derniers car. du serial CM5>`
- **Installer Docker CE si absent** — dépôt officiel Debian, arch. détectée (arm64),
  exécuté **avant** le transfert de l'image
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
| `GET/POST` | `/api/edge/<ip>/preflight` | **Contrôle des prérequis** (cible + référence + serveur) |
| `POST` | `/api/edge/<ip>/docker/install` | **Installe Docker CE seul** (`DOCKER_ONLY`) |
| `GET` | `/api/edge/<ip>/images` | **Liste les images Docker** de la carte |
| `POST` | `/api/edge/<ip>/images/delete` | **Supprime une image** (arrête la stack) |
| `POST` | `/api/edge/<ip>/images/transfer` | **Transfère / remplace l'image** (`force`) |
| `GET` | `/api/preflight/server` | Prérequis du serveur ModBerry Manager |
| `GET/POST` | `/api/edge/<ip>/diagnose` | **Diagnostic complet** avec correctifs |
| `POST` | `/api/edge/<ip>/postgres/ensure` | **Garantit la base** (pull ou réplication) |
| `POST` | `/api/edge/<ip>/cleanup` | **Supprime les conteneurs hors stack** |
| `POST` | `/api/edge/<ip>/boot` | **Relance au boot** (activer / désactiver) |
| `GET` | `/api/edge/<ip>/logs?container=tb-edge-postgres` | Logs de la base |
| `POST` | `/api/edge/<ip>/deploy` | Séquence prérequis → Docker → image → stack → gateway |
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
| `MODBERRY_TB_USERNAME` | `tenant@mobilis.dz` | Identifiant tenant pré-rempli |
| `MODBERRY_TB_PASSWORD` | `tenant` | Mot de passe tenant pré-rempli (jamais persisté) |
| `MODBERRY_AUTO_SCAN_INTERVAL_SECONDS` | `7200` | Intervalle du scan automatique |

---

## Structure

```
app.py                      application Flask + routes Edge / master / gateway
tb_edge.py                  sonde SSH, provisioning, garde-fou d'identité
tb_preflight.py             contrôle des prérequis (Docker, outils, disque, image)
tb_master.py                réplication + gestion des images depuis la référence
tb_cloud.py                 client REST du serveur ThingsBoard (tenant)
jobs.py                     tâches de fond avec logs incrémentaux
assets/edge_provision.sh    script exécuté sur la carte (idempotent, DOCKER_ONLY)
templates/edge.html         page de provisioning (prérequis, image, tenant, déploiement)
templates/dashboard.html    inventaire + colonne d'état Edge
config.json                 état persisté (scan, réglages, états, identités)
```

## Prérequis sur les cartes

- SSH accessible avec les identifiants configurés
- `sudo` sans mot de passe si l'utilisateur SSH n'est pas `root`
- accès réseau au serveur ThingsBoard (port RPC `7071`)
- accès à Docker Hub pour `postgres:16` (l'image Edge, elle, vient de la carte maître)
