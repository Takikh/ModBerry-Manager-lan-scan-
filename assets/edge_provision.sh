#!/bin/bash
# =============================================================================
#  ModBerry Manager - Provisioning ThingsBoard Edge sur ModBerry X-500 CM5
#  Execute SUR la carte cible, via SSH depuis l'interface web.
#
#  Reproduit la stack de la carte de reference "techbase" (10.0.0.26) :
#    - Docker CE depuis le depot officiel Debian (bookworm / arm64)
#    - projet compose "tbedge-mobilis" dans /root/tb-edge-mobilis
#    - image custom thingsboard/tb-edge:4.3.1.3EDGE-mobilis (chargee par docker load)
#    - postgres:16 sur volume nomme (JAMAIS clone : identite/etat propres a la carte)
#
#  Idempotent : relançable sans risque. Utilisable aussi en first-boot.
#
#  Requis :
#    EDGE_KEY / EDGE_SECRET   identite UNIQUE de cette carte (routing key/secret)
#    CLOUD_RPC_HOST           serveur ThingsBoard (ex. 10.0.0.1)
#  Optionnel :
#    CLOUD_RPC_PORT      defaut 7071
#    CLOUD_RPC_SSL       defaut false
#    EDGE_IMAGE          defaut thingsboard/tb-edge:4.3.1.3EDGE-mobilis
#    EDGE_IMAGE_ARCHIVE  archive .tar.gz a charger si l'image est absente
#    EDGE_DIR            defaut /root/tb-edge-mobilis
#    COMPOSE_PROJECT     defaut tbedge-mobilis
#    EDGE_HTTP_PORT      defaut 8082    EDGE_MQTT_PORT  defaut 1884
#    PG_PASSWORD         defaut postgres
#    DOCKER_DATA_ROOT    ex. /mnt/ssd/docker (deplace le stockage Docker sur le SSD)
#    RESET_DATA          1 = supprime les volumes (obligatoire sur image clonee)
#    SET_HOSTNAME        1 = hostname unique derive du serial CM5
#    HOSTNAME_PREFIX     defaut modberry
#    INSTALL_DOCKER      1 = installe Docker CE si absent
#    PULL_IMAGES         1 = docker compose pull (defaut 1)
#    START_EDGE          1 = docker compose up -d (defaut 1)
# =============================================================================
set -uo pipefail

EDGE_DIR="${EDGE_DIR:-/root/tb-edge-mobilis}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-tbedge-mobilis}"
CLOUD_RPC_PORT="${CLOUD_RPC_PORT:-7071}"
CLOUD_RPC_SSL="${CLOUD_RPC_SSL:-false}"
EDGE_IMAGE="${EDGE_IMAGE:-thingsboard/tb-edge:4.3.1.3EDGE-mobilis}"
EDGE_IMAGE_ARCHIVE="${EDGE_IMAGE_ARCHIVE:-}"
EDGE_HTTP_PORT="${EDGE_HTTP_PORT:-8082}"
EDGE_MQTT_PORT="${EDGE_MQTT_PORT:-1884}"
PG_IMAGE="${PG_IMAGE:-postgres:16}"
PG_PASSWORD="${PG_PASSWORD:-postgres}"
DOCKER_DATA_ROOT="${DOCKER_DATA_ROOT:-}"
RESET_DATA="${RESET_DATA:-0}"
SET_HOSTNAME="${SET_HOSTNAME:-0}"
HOSTNAME_PREFIX="${HOSTNAME_PREFIX:-modberry}"
INSTALL_DOCKER="${INSTALL_DOCKER:-0}"
PULL_IMAGES="${PULL_IMAGES:-1}"
START_EDGE="${START_EDGE:-1}"

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[$(date '+%H:%M:%S')] ERREUR: $*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "root requis"
  SUDO="sudo -n"
fi

# ---------------------------------------------------------------- 0. controles
[ -n "${EDGE_KEY:-}" ]       || fail "EDGE_KEY (Cle Edge) manquant"
[ -n "${EDGE_SECRET:-}" ]    || fail "EDGE_SECRET (Secret Edge) manquant"
[ -n "${CLOUD_RPC_HOST:-}" ] || fail "CLOUD_RPC_HOST manquant"

log "=== Provisioning ThingsBoard Edge (ModBerry CM5) ==="
log "Repertoire    : $EDGE_DIR (projet $COMPOSE_PROJECT)"
log "Serveur       : $CLOUD_RPC_HOST:$CLOUD_RPC_PORT (ssl=$CLOUD_RPC_SSL)"
log "Image edge    : $EDGE_IMAGE"
log "Ports hote    : HTTP $EDGE_HTTP_PORT -> 8080 | MQTT $EDGE_MQTT_PORT -> 1883"
log "Cle Edge      : ${EDGE_KEY:0:8}...${EDGE_KEY: -4}"
log "Reset volumes : $RESET_DATA"

log "OS            : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") / $(uname -m)"
log "Modele        : $(tr -d '\000' < /proc/device-tree/model 2>/dev/null || echo inconnu)"

# ------------------------------------------------------- 1. identite unique
SERIAL="$(tr -d '\000' < /proc/device-tree/serial-number 2>/dev/null || true)"
[ -n "$SERIAL" ] || SERIAL="$(awk '/Serial/{print $3}' /proc/cpuinfo 2>/dev/null | tail -1)"
[ -n "$SERIAL" ] || SERIAL="$(cut -c1-16 /etc/machine-id 2>/dev/null)"
SERIAL="${SERIAL:-unknown}"
SHORT_SERIAL="$(echo "$SERIAL" | tail -c 9)"
log "Serial carte  : $SERIAL"

if [ "$SET_HOSTNAME" = "1" ]; then
  NEW_HOSTNAME="${HOSTNAME_PREFIX}-${SHORT_SERIAL}"
  if [ "$(hostname)" != "$NEW_HOSTNAME" ]; then
    log "Hostname      : $(hostname) -> $NEW_HOSTNAME"
    $SUDO hostnamectl set-hostname "$NEW_HOSTNAME" 2>/dev/null \
      || echo "$NEW_HOSTNAME" | $SUDO tee /etc/hostname >/dev/null
    $SUDO sed -i "s/127.0.1.1.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts 2>/dev/null || true
  else
    log "Hostname      : deja $NEW_HOSTNAME"
  fi
fi

# ------------------------------------------- 2. Docker CE (depot officiel)
install_docker_ce() {
  log "Installation de Docker CE (depot officiel Debian, arm64)..."
  local codename arch
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")"
  case "$(dpkg --print-architecture 2>/dev/null || uname -m)" in
    arm64|aarch64) arch="arm64" ;;
    amd64|x86_64)  arch="amd64" ;;
    armhf)         arch="armhf" ;;
    *)             arch="arm64" ;;
  esac
  log "  distribution=$codename architecture=$arch"

  $SUDO apt-get update -qq || fail "apt-get update a echoue"
  $SUDO apt-get install -y -qq ca-certificates curl gnupg || fail "prerequis apt non installes"

  $SUDO install -m 0755 -d /etc/apt/keyrings
  $SUDO curl -fsSL https://download.docker.com/linux/debian/gpg \
    -o /etc/apt/keyrings/docker.asc || fail "cle GPG Docker non recuperee"
  $SUDO chmod a+r /etc/apt/keyrings/docker.asc

  echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $codename stable" \
    | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null

  $SUDO apt-get update -qq || fail "apt-get update (depot docker) a echoue"
  $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin || fail "installation docker-ce echouee"

  $SUDO systemctl enable --now docker || true
}

if ! command -v docker >/dev/null 2>&1; then
  if [ "$INSTALL_DOCKER" = "1" ]; then
    install_docker_ce
  else
    fail "Docker absent. Cochez « Installer Docker CE » pour l'installer automatiquement."
  fi
fi
log "Docker        : $(docker --version 2>/dev/null)"

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  log "Plugin compose absent -> installation"
  $SUDO apt-get install -y -qq docker-compose-plugin || fail "docker compose indisponible"
  DC="docker compose"
fi
log "Compose       : $DC"

$SUDO systemctl is-active docker >/dev/null 2>&1 || $SUDO systemctl start docker || true

# -------------------------------- 2b. stockage Docker sur SSD (optionnel)
if [ -n "$DOCKER_DATA_ROOT" ]; then
  CURRENT_ROOT="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo '')"
  if [ "$CURRENT_ROOT" != "$DOCKER_DATA_ROOT" ]; then
    MOUNT_POINT="$(dirname "$DOCKER_DATA_ROOT")"
    if mountpoint -q "$MOUNT_POINT" 2>/dev/null || [ -d "$MOUNT_POINT" ]; then
      log "Deplacement du stockage Docker vers $DOCKER_DATA_ROOT"
      $SUDO mkdir -p "$DOCKER_DATA_ROOT" /etc/docker
      if [ -f /etc/docker/daemon.json ]; then
        $SUDO cp /etc/docker/daemon.json "/etc/docker/daemon.json.bak.$(date +%s)"
      fi
      printf '{\n  "data-root": "%s"\n}\n' "$DOCKER_DATA_ROOT" \
        | $SUDO tee /etc/docker/daemon.json >/dev/null
      $SUDO systemctl restart docker || log "AVERTISSEMENT: redemarrage docker echoue"
      sleep 3
      log "Stockage Docker : $(docker info -f '{{.DockerRootDir}}' 2>/dev/null)"
    else
      log "AVERTISSEMENT: $MOUNT_POINT absent, stockage Docker laisse par defaut"
    fi
  else
    log "Stockage Docker : deja $DOCKER_DATA_ROOT"
  fi
fi

# ------------------------------------- 3. arret de la stack precedente
$SUDO mkdir -p "$EDGE_DIR"

if [ -f "$EDGE_DIR/docker-compose.yml" ]; then
  log "Stack existante -> arret avant reconfiguration"
  if [ "$RESET_DATA" = "1" ]; then
    # -v supprime les volumes nommes : indispensable si l'image a ete clonee,
    # sinon la carte repart avec la base (et donc l'identite) de la carte maitre.
    log "RESET_DATA=1 -> suppression des volumes de donnees"
    (cd "$EDGE_DIR" && $SUDO $DC -p "$COMPOSE_PROJECT" down -v --remove-orphans 2>&1 | sed 's/^/    /') || true
  else
    (cd "$EDGE_DIR" && $SUDO $DC -p "$COMPOSE_PROJECT" down --remove-orphans 2>&1 | sed 's/^/    /') || true
  fi
fi

for container in tb-edge tb-edge-postgres; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$container"; then
    log "Suppression du conteneur orphelin $container"
    $SUDO docker rm -f "$container" >/dev/null 2>&1 || true
  fi
done

if [ "$RESET_DATA" = "1" ]; then
  for volume in "${COMPOSE_PROJECT}_tb-edge-postgres-data" "${COMPOSE_PROJECT}_tb-edge-data" "${COMPOSE_PROJECT}_tb-edge-logs"; do
    if docker volume ls -q | grep -qx "$volume"; then
      log "Suppression du volume residuel $volume"
      $SUDO docker volume rm -f "$volume" >/dev/null 2>&1 || true
    fi
  done
fi

# --------------------------- 4. image custom : chargement si necessaire
if docker image inspect "$EDGE_IMAGE" >/dev/null 2>&1; then
  log "Image $EDGE_IMAGE : deja presente localement"
elif [ -n "$EDGE_IMAGE_ARCHIVE" ] && [ -f "$EDGE_IMAGE_ARCHIVE" ]; then
  log "Chargement de l'image depuis $EDGE_IMAGE_ARCHIVE (docker load)..."
  case "$EDGE_IMAGE_ARCHIVE" in
    *.gz) gunzip -c "$EDGE_IMAGE_ARCHIVE" | $SUDO docker load 2>&1 | sed 's/^/    /' ;;
    *)    $SUDO docker load -i "$EDGE_IMAGE_ARCHIVE" 2>&1 | sed 's/^/    /' ;;
  esac
  docker image inspect "$EDGE_IMAGE" >/dev/null 2>&1 \
    || fail "l'archive ne contient pas l'image $EDGE_IMAGE"
  log "Image chargee avec succes"
  $SUDO rm -f "$EDGE_IMAGE_ARCHIVE"
else
  # Tag custom : tres probablement absent de tout registre public.
  log "Image $EDGE_IMAGE absente localement, tentative de pull..."
  if ! $SUDO docker pull "$EDGE_IMAGE" 2>&1 | sed 's/^/    /'; then
    fail "Image $EDGE_IMAGE introuvable (registre inaccessible). Utilisez la replication depuis la carte de reference pour la transferer (docker save/load)."
  fi
fi

# --------------------------------------------------------- 5. fichier .env
log "Ecriture de $EDGE_DIR/.env"
$SUDO tee "$EDGE_DIR/.env" >/dev/null <<EOF
# Genere par ModBerry Manager le $(date '+%Y-%m-%d %H:%M:%S')
# IDENTITE UNIQUE DE CETTE CARTE - ne jamais copier sur une autre carte
EDGE_NAME=${EDGE_NAME:-modberry-$SHORT_SERIAL}
BOARD_SERIAL=$SERIAL
COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT
CLOUD_ROUTING_KEY=$EDGE_KEY
CLOUD_ROUTING_SECRET=$EDGE_SECRET
CLOUD_RPC_HOST=$CLOUD_RPC_HOST
CLOUD_RPC_PORT=$CLOUD_RPC_PORT
CLOUD_RPC_SSL_ENABLED=$CLOUD_RPC_SSL
EDGE_IMAGE=$EDGE_IMAGE
EDGE_HTTP_PORT=$EDGE_HTTP_PORT
EDGE_MQTT_PORT=$EDGE_MQTT_PORT
PG_IMAGE=$PG_IMAGE
PG_PASSWORD=$PG_PASSWORD
EOF
$SUDO chmod 600 "$EDGE_DIR/.env"

# ------------------------------------------------- 6. docker-compose.yml
log "Ecriture de $EDGE_DIR/docker-compose.yml"
$SUDO tee "$EDGE_DIR/docker-compose.yml" >/dev/null <<'EOF'
# Genere par ModBerry Manager - stack calquee sur techbase (10.0.0.26)
# Les donnees vivent dans des volumes nommes, propres a cette carte.
services:
  postgres:
    container_name: tb-edge-postgres
    image: ${PG_IMAGE}
    restart: always
    environment:
      POSTGRES_DB: tb_edge
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${PG_PASSWORD}
    volumes:
      - tb-edge-postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d tb_edge"]
      interval: 10s
      timeout: 5s
      retries: 12

  tb-edge:
    container_name: tb-edge
    image: ${EDGE_IMAGE}
    restart: always
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "${EDGE_HTTP_PORT}:8080"
      - "${EDGE_MQTT_PORT}:1883"
      - "5683-5688:5683-5688/udp"
    environment:
      SPRING_DATASOURCE_URL: jdbc:postgresql://postgres:5432/tb_edge
      SPRING_DATASOURCE_USERNAME: postgres
      SPRING_DATASOURCE_PASSWORD: ${PG_PASSWORD}
      CLOUD_ROUTING_KEY: ${CLOUD_ROUTING_KEY}
      CLOUD_ROUTING_SECRET: ${CLOUD_ROUTING_SECRET}
      CLOUD_RPC_HOST: ${CLOUD_RPC_HOST}
      CLOUD_RPC_PORT: ${CLOUD_RPC_PORT}
      CLOUD_RPC_SSL_ENABLED: ${CLOUD_RPC_SSL_ENABLED}
      JAVA_OPTS: "-Xms256m -Xmx1024m"
    volumes:
      - tb-edge-data:/data
      - tb-edge-logs:/var/log/tb-edge

volumes:
  tb-edge-postgres-data:
  tb-edge-data:
  tb-edge-logs:
EOF

# ------------------------------------------------------------ 7. demarrage
if [ "$PULL_IMAGES" = "1" ]; then
  # L'image edge est deja locale (etape 4) : on ne recupere que postgres.
  log "Recuperation de $PG_IMAGE..."
  $SUDO docker pull "$PG_IMAGE" 2>&1 | sed 's/^/    /' \
    || log "AVERTISSEMENT: pull de $PG_IMAGE echoue, image locale utilisee si presente"
fi

if [ "$START_EDGE" = "1" ]; then
  log "Demarrage de la stack..."
  (cd "$EDGE_DIR" && $SUDO $DC -p "$COMPOSE_PROJECT" up -d 2>&1 | sed 's/^/    /') \
    || fail "docker compose up a echoue"
  sleep 8
  log "Etat des conteneurs :"
  (cd "$EDGE_DIR" && $SUDO $DC -p "$COMPOSE_PROJECT" ps 2>&1 | sed 's/^/    /') || true
else
  log "START_EDGE=0 -> stack configuree mais non demarree"
fi

log "=== Provisioning termine ==="
log "Interface Edge locale : http://$(hostname -I 2>/dev/null | awk '{print $1}'):$EDGE_HTTP_PORT"
log "Verifier l'etat 'Active' de cet Edge dans ThingsBoard (Edge instances)"
exit 0
