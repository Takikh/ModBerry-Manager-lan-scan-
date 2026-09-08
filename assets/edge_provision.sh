#!/bin/bash
# =============================================================================
#  ModBerry Manager - ThingsBoard Edge provisioning script
#  Executed ON the ModBerry (CM5) board over SSH by the web interface.
#
#  Idempotent: can be re-run safely. Also usable as a first-boot script
#  after cloning a Master Image (see RESET_DATA=1).
#
#  Required env:
#    EDGE_KEY        CLOUD_ROUTING_KEY   (Cle Edge)
#    EDGE_SECRET     CLOUD_ROUTING_SECRET(Secret Edge)
#    CLOUD_RPC_HOST  ThingsBoard server host/IP
#  Optional env:
#    CLOUD_RPC_PORT      default 7070
#    CLOUD_RPC_SSL       default false
#    EDGE_VERSION        default 4.0.0EDGE
#    EDGE_DIR            default /opt/tb-edge
#    EDGE_HTTP_PORT      default 8080
#    EDGE_MQTT_PORT      default 1883
#    EDGE_NAME           informative label stored in .env
#    PG_PASSWORD         default postgres
#    RESET_DATA          1 = wipe local edge DB (mandatory on cloned images)
#    SET_HOSTNAME        1 = set unique hostname from board serial
#    HOSTNAME_PREFIX     default modberry
#    INSTALL_DOCKER      1 = install docker if missing
#    PULL_IMAGES         1 = docker compose pull before up (default 1)
#    START_EDGE          1 = docker compose up -d (default 1)
# =============================================================================
set -uo pipefail

EDGE_DIR="${EDGE_DIR:-/opt/tb-edge}"
CLOUD_RPC_PORT="${CLOUD_RPC_PORT:-7070}"
CLOUD_RPC_SSL="${CLOUD_RPC_SSL:-false}"
EDGE_VERSION="${EDGE_VERSION:-4.0.0EDGE}"
EDGE_HTTP_PORT="${EDGE_HTTP_PORT:-8080}"
EDGE_MQTT_PORT="${EDGE_MQTT_PORT:-1883}"
PG_PASSWORD="${PG_PASSWORD:-postgres}"
PG_VERSION="${PG_VERSION:-16}"
RESET_DATA="${RESET_DATA:-0}"
SET_HOSTNAME="${SET_HOSTNAME:-0}"
HOSTNAME_PREFIX="${HOSTNAME_PREFIX:-modberry}"
INSTALL_DOCKER="${INSTALL_DOCKER:-0}"
PULL_IMAGES="${PULL_IMAGES:-1}"
START_EDGE="${START_EDGE:-1}"
DATA_DIR="${DATA_DIR:-/var/lib/tb-edge}"

log()  { echo "[$(date '+%H:%M:%S')] $*"; }
fail() { echo "[$(date '+%H:%M:%S')] ERREUR: $*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo -n"; else fail "root requis"; fi
fi

# ---------------------------------------------------------------- 0. checks
[ -n "${EDGE_KEY:-}" ]       || fail "EDGE_KEY (Cle Edge) manquant"
[ -n "${EDGE_SECRET:-}" ]    || fail "EDGE_SECRET (Secret Edge) manquant"
[ -n "${CLOUD_RPC_HOST:-}" ] || fail "CLOUD_RPC_HOST manquant"

log "=== Provisioning ThingsBoard Edge ==="
log "Repertoire      : $EDGE_DIR"
log "Serveur cloud   : $CLOUD_RPC_HOST:$CLOUD_RPC_PORT (ssl=$CLOUD_RPC_SSL)"
log "Version edge    : $EDGE_VERSION"
log "Cle Edge        : ${EDGE_KEY:0:8}...${EDGE_KEY: -4}"
log "Reset data      : $RESET_DATA"

# ------------------------------------------------------- 1. identite unique
SERIAL="$(tr -d '\000' < /proc/device-tree/serial-number 2>/dev/null || true)"
[ -n "$SERIAL" ] || SERIAL="$(awk '/Serial/{print $3}' /proc/cpuinfo 2>/dev/null | tail -1)"
[ -n "$SERIAL" ] || SERIAL="$(cat /etc/machine-id 2>/dev/null | cut -c1-16)"
SERIAL="${SERIAL:-unknown}"
SHORT_SERIAL="$(echo "$SERIAL" | tail -c 9)"
log "Serial carte    : $SERIAL"

if [ "$SET_HOSTNAME" = "1" ]; then
  NEW_HOSTNAME="${HOSTNAME_PREFIX}-${SHORT_SERIAL}"
  CURRENT_HOSTNAME="$(hostname)"
  if [ "$CURRENT_HOSTNAME" != "$NEW_HOSTNAME" ]; then
    log "Hostname unique : $CURRENT_HOSTNAME -> $NEW_HOSTNAME"
    $SUDO hostnamectl set-hostname "$NEW_HOSTNAME" 2>/dev/null \
      || echo "$NEW_HOSTNAME" | $SUDO tee /etc/hostname >/dev/null
    $SUDO sed -i "s/127.0.1.1.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts 2>/dev/null || true
  else
    log "Hostname deja unique : $NEW_HOSTNAME"
  fi
fi

# ------------------------------------------------------------- 2. docker
if ! command -v docker >/dev/null 2>&1; then
  if [ "$INSTALL_DOCKER" = "1" ]; then
    log "Docker absent -> installation (get.docker.com)"
    curl -fsSL https://get.docker.com | $SUDO sh || fail "installation docker echouee"
    $SUDO systemctl enable --now docker || true
  else
    fail "docker non installe (cocher 'Installer Docker' ou installer manuellement)"
  fi
fi
log "Docker          : $(docker --version 2>/dev/null)"

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  fail "docker compose introuvable"
fi
log "Compose         : $DC"

$SUDO systemctl is-active docker >/dev/null 2>&1 || $SUDO systemctl start docker || true

# --------------------------------------------------- 3. arret / reset data
$SUDO mkdir -p "$EDGE_DIR" "$DATA_DIR/db" "$DATA_DIR/logs"

if [ -f "$EDGE_DIR/docker-compose.yml" ]; then
  log "Stack existante -> arret avant reconfiguration"
  (cd "$EDGE_DIR" && $SUDO $DC down --remove-orphans 2>&1 | sed 's/^/    /') || true
fi

# Toujours nettoyer les conteneurs orphelins issus d'un deploiement precedent
for c in tb-edge tb-edge-postgres; do
  docker ps -a --format '{{.Names}}' | grep -qx "$c" && { log "Suppression conteneur orphelin $c"; $SUDO docker rm -f "$c" >/dev/null 2>&1 || true; }
done

if [ "$RESET_DATA" = "1" ]; then
  log "RESET_DATA=1 -> purge de la base locale (identite Edge clonee)"
  $SUDO rm -rf "$DATA_DIR/db" "$DATA_DIR/logs"
  $SUDO mkdir -p "$DATA_DIR/db" "$DATA_DIR/logs"
fi
$SUDO chown -R 799:799 "$DATA_DIR/logs" 2>/dev/null || true
$SUDO chmod -R 777 "$DATA_DIR/logs" 2>/dev/null || true

# --------------------------------------------------------- 4. fichier .env
log "Ecriture $EDGE_DIR/.env"
$SUDO tee "$EDGE_DIR/.env" >/dev/null <<EOF
# Genere par ModBerry Manager le $(date '+%Y-%m-%d %H:%M:%S')
# Identite unique de cette carte - NE PAS copier sur une autre carte
EDGE_NAME=${EDGE_NAME:-modberry-$SHORT_SERIAL}
BOARD_SERIAL=$SERIAL
CLOUD_ROUTING_KEY=$EDGE_KEY
CLOUD_ROUTING_SECRET=$EDGE_SECRET
CLOUD_RPC_HOST=$CLOUD_RPC_HOST
CLOUD_RPC_PORT=$CLOUD_RPC_PORT
CLOUD_RPC_SSL_ENABLED=$CLOUD_RPC_SSL
EDGE_VERSION=$EDGE_VERSION
EDGE_HTTP_PORT=$EDGE_HTTP_PORT
EDGE_MQTT_PORT=$EDGE_MQTT_PORT
PG_VERSION=$PG_VERSION
PG_PASSWORD=$PG_PASSWORD
DATA_DIR=$DATA_DIR
EOF
$SUDO chmod 600 "$EDGE_DIR/.env"

# ------------------------------------------------ 5. docker-compose.yml
log "Ecriture $EDGE_DIR/docker-compose.yml"
$SUDO tee "$EDGE_DIR/docker-compose.yml" >/dev/null <<'EOF'
# Genere par ModBerry Manager - ThingsBoard Edge sur ModBerry CM5
services:
  postgres:
    container_name: tb-edge-postgres
    image: postgres:${PG_VERSION}
    restart: always
    environment:
      POSTGRES_DB: tb_edge
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${PG_PASSWORD}
    volumes:
      - ${DATA_DIR}/db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d tb_edge"]
      interval: 10s
      timeout: 5s
      retries: 12

  tb-edge:
    container_name: tb-edge
    image: thingsboard/tb-edge:${EDGE_VERSION}
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
      - ${DATA_DIR}/logs:/var/log/tb-edge
EOF

# ------------------------------------------------------------ 6. demarrage
if [ "$PULL_IMAGES" = "1" ]; then
  log "Telechargement des images (peut prendre plusieurs minutes)..."
  (cd "$EDGE_DIR" && $SUDO $DC pull 2>&1 | sed 's/^/    /') || log "AVERTISSEMENT: pull partiel, on continue avec les images locales"
fi

if [ "$START_EDGE" = "1" ]; then
  log "Demarrage de la stack ThingsBoard Edge..."
  (cd "$EDGE_DIR" && $SUDO $DC up -d 2>&1 | sed 's/^/    /') || fail "docker compose up a echoue"
  sleep 6
  log "Etat des conteneurs :"
  (cd "$EDGE_DIR" && $SUDO $DC ps 2>&1 | sed 's/^/    /') || true
else
  log "START_EDGE=0 -> stack configuree mais non demarree"
fi

log "=== Provisioning termine ==="
log "Interface Edge locale : http://$(hostname -I 2>/dev/null | awk '{print $1}'):$EDGE_HTTP_PORT"
log "Connexion au serveur  : verifier l'etat 'Active' dans ThingsBoard (Edge instances)"
exit 0
