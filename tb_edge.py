#!/usr/bin/env python3
"""
tb_edge.py - Gestion de ThingsBoard Edge sur les cartes ModBerry CM5 via SSH.

Deux responsabilites :
  1. PROBE  : inspecter l'etat d'une carte (docker, stack tb-edge, identite
              Edge configuree, connexion au serveur ThingsBoard).
  2. DEPLOY : pousser le script assets/edge_provision.sh et l'executer avec
              les identifiants (Cle Edge / Secret Edge) fournis depuis le web.

Aucune dependance hors paramiko.
"""

import os
import re
import shlex
import time

import paramiko

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROVISION_SCRIPT = os.path.join(BASE_DIR, "assets", "edge_provision.sh")
REMOTE_SCRIPT = "/tmp/modberry_edge_provision.sh"

# Valeurs alignees sur la carte de reference "techbase" (10.0.0.26).
# Voir README - Architecture de reference.
DEFAULT_EDGE_DIR = "/root/tb-edge-mobilis"
DEFAULT_EDGE_HTTP_PORT = 8082          # 8082 -> 8080 dans le conteneur
DEFAULT_EDGE_MQTT_PORT = 1884          # 1884 -> 1883 (1883 est pris par mosquitto)
DEFAULT_EDGE_IMAGE = "thingsboard/tb-edge:4.3.1.3EDGE-mobilis"
DEFAULT_CLOUD_RPC_PORT = 7071
DEFAULT_COMPOSE_PROJECT = "tbedge-mobilis"
DEFAULT_DOCKER_DATA_ROOT = "/mnt/ssd/docker"

# Gateway I/O Python (VTEKE / Modbus / GPIO), executee nativement en systemd
GATEWAY_DIR = "/opt/mobilis-gateway"
GATEWAY_SERVICE = "tb-edge-io.service"
GATEWAY_LOG = "/var/log/mobilis-gateway.log"

# Conserve pour compatibilite avec l'ancien nommage
DEFAULT_EDGE_VERSION = DEFAULT_EDGE_IMAGE

SSH_CONNECT_TIMEOUT = 8
EXEC_TIMEOUT = 25
DEPLOY_TIMEOUT = 1800  # 30 min : pull des images ARM64 sur lien lent

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ---------------------------------------------------------------- validation
def validate_edge_key(value):
    """La Cle Edge ThingsBoard est un UUID."""
    value = (value or "").strip()
    if not value:
        return False, "La Cle Edge est obligatoire."
    if not UUID_RE.match(value):
        return False, "La Cle Edge doit etre un UUID (ex: 1666b31c-e954-04e6-a19d-01adbeaa6eae)."
    return True, value


def validate_edge_secret(value):
    """Le Secret Edge est une chaine alphanumerique (20 car. par defaut)."""
    value = (value or "").strip()
    if not value:
        return False, "Le Secret Edge est obligatoire."
    if not re.match(r"^[A-Za-z0-9]{8,64}$", value):
        return False, "Le Secret Edge doit contenir 8 a 64 caracteres alphanumeriques."
    return True, value


# Identite de la carte de reference : ne doit JAMAIS etre reutilisee ailleurs.
# Deux Edge partageant la meme routing key entrent en conflit sur le serveur.
MASTER_ROUTING_KEYS = {
    "b534512b-9c61-ed18-0765-86e95dc799d1",  # techbase / 10.0.0.26
}


def check_key_not_reused(edge_key, target_ip, assignments):
    """
    Verifie qu'une routing key n'est pas deja affectee a une autre carte.

    assignments : {routing_key: ip} des cles deja deployees.
    Retour : (ok, message)
    """
    key = (edge_key or "").strip().lower()
    if not key:
        return True, ""

    if key in {item.lower() for item in MASTER_ROUTING_KEYS}:
        return False, (
            "Cette Cle Edge est celle de la carte de reference (techbase). "
            "Chaque carte doit avoir sa propre paire Cle/Secret : creez un nouvel "
            "Edge dans ThingsBoard pour cette carte."
        )

    for assigned_key, assigned_ip in (assignments or {}).items():
        if assigned_key.lower() == key and str(assigned_ip) != str(target_ip):
            return False, (
                f"Cette Cle Edge est deja utilisee par la carte {assigned_ip}. "
                "Reutiliser une meme cle provoque un conflit d'identite sur le serveur "
                "ThingsBoard : creez un Edge distinct pour cette carte."
            )

    return True, ""


def validate_host(value):
    value = (value or "").strip()
    if not value:
        return False, "L'hote du serveur ThingsBoard est obligatoire."
    if not re.match(r"^[A-Za-z0-9]([A-Za-z0-9\.\-]*[A-Za-z0-9])?$", value):
        return False, "Hote invalide (IP ou nom DNS attendu)."
    return True, value


def mask_secret(value, keep=4):
    value = value or ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * (len(value) - keep)}{value[-keep:]}"


# --------------------------------------------------------------------- SSH
class SSHSession:
    """Petit wrapper paramiko avec contexte."""

    def __init__(self, ip, user, password, port=22, timeout=SSH_CONNECT_TIMEOUT):
        self.ip = str(ip)
        self.user = user
        self.password = password
        self.port = port
        self.timeout = timeout
        self.client = None

    def __enter__(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.ip,
            port=self.port,
            username=self.user,
            password=self.password,
            timeout=self.timeout,
            banner_timeout=self.timeout,
            auth_timeout=self.timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        self.client = client
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        return False

    def run(self, command, timeout=EXEC_TIMEOUT):
        """Execute une commande, retourne (rc, stdout, stderr)."""
        _, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out.strip(), err.strip()

    def out(self, command, timeout=EXEC_TIMEOUT, default=""):
        try:
            rc, out, _ = self.run(command, timeout=timeout)
            return out if rc == 0 and out else (out or default)
        except Exception:
            return default

    def run_streaming(self, command, on_line, timeout=DEPLOY_TIMEOUT):
        """Execute une commande longue en streamant chaque ligne vers on_line."""
        transport = self.client.get_transport()
        channel = transport.open_session()
        channel.settimeout(5)
        channel.get_pty()
        channel.exec_command(command)

        buffer = ""
        started = time.time()
        while True:
            if channel.recv_ready():
                chunk = channel.recv(65536).decode("utf-8", "replace")
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    on_line(line.rstrip("\r"))
            elif channel.exit_status_ready():
                break
            else:
                if time.time() - started > timeout:
                    on_line("ERREUR: delai d'execution depasse, abandon.")
                    channel.close()
                    return 124
                time.sleep(0.3)

        # drain
        while channel.recv_ready():
            buffer += channel.recv(65536).decode("utf-8", "replace")
        for line in buffer.splitlines():
            on_line(line.rstrip("\r"))

        return channel.recv_exit_status()

    def put_file(self, local_path, remote_path, mode=0o700):
        sftp = self.client.open_sftp()
        try:
            sftp.put(local_path, remote_path)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()


# ------------------------------------------------------------------- PROBE
def _parse_env(text):
    env = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def probe_edge(ip, user, password, edge_dir=DEFAULT_EDGE_DIR, port=22):
    """
    Inspecte une carte ModBerry et retourne l'etat complet de son Edge.

    Retour (dict) :
      reachable, error
      docker_installed, docker_version, compose_available, docker_running
      configured        -> un .env avec CLOUD_ROUTING_KEY existe
      edge_key, edge_name, board_serial, cloud_host, cloud_port, edge_version
      container_state   -> running | exited | absent
      container_status  -> texte docker ps
      postgres_status
      cloud_connected   -> True si les logs montrent une connexion au serveur
      cloud_hint        -> ligne de log expliquant l'etat
      edge_url          -> interface locale
      status            -> not_installed | not_configured | stopped | starting
                           | connected | error
    """
    state = {
        "ip": str(ip),
        "reachable": False,
        "error": None,
        "docker_installed": False,
        "docker_version": None,
        "compose_available": False,
        "docker_running": False,
        "configured": False,
        "edge_key": None,
        "edge_name": None,
        "board_serial": None,
        "cloud_host": None,
        "cloud_port": None,
        "edge_version": None,
        "edge_http_port": DEFAULT_EDGE_HTTP_PORT,
        "container_state": "absent",
        "container_status": None,
        "postgres_status": None,
        "cloud_connected": False,
        "cloud_hint": None,
        "edge_url": None,
        "status": "unknown",
        # specifiques au parc ModBerry
        "os_pretty": None,
        "model": None,
        "arch": None,
        "edge_image": None,
        "image_present": False,
        "docker_data_root": None,
        "compose_project": None,
        "gateway_present": False,
        "gateway_service_state": None,
        "gateway_gpio_errors": 0,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            state["reachable"] = True

            state["os_pretty"] = ssh.out(
                ". /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\""
            ) or None
            state["model"] = ssh.out(
                "tr -d '\\000' < /proc/device-tree/model 2>/dev/null"
            ) or None
            state["arch"] = ssh.out("uname -m 2>/dev/null") or None

            docker_version = ssh.out("docker --version 2>/dev/null")
            if docker_version and "Docker version" in docker_version:
                state["docker_installed"] = True
                state["docker_version"] = docker_version

            if state["docker_installed"]:
                compose = ssh.out(
                    "docker compose version --short 2>/dev/null "
                    "|| docker-compose version --short 2>/dev/null"
                )
                state["compose_available"] = bool(compose)
                state["docker_running"] = "active" in ssh.out(
                    "systemctl is-active docker 2>/dev/null || echo unknown"
                )
                state["docker_data_root"] = ssh.out(
                    "docker info -f '{{.DockerRootDir}}' 2>/dev/null"
                ) or None

            env_text = ssh.out(
                f"cat {shlex.quote(edge_dir)}/.env 2>/dev/null "
                f"|| sudo -n cat {shlex.quote(edge_dir)}/.env 2>/dev/null"
            )
            env = _parse_env(env_text)
            if env.get("CLOUD_ROUTING_KEY"):
                state["configured"] = True
                state["edge_key"] = env.get("CLOUD_ROUTING_KEY")
                state["edge_name"] = env.get("EDGE_NAME")
                state["board_serial"] = env.get("BOARD_SERIAL")
                state["cloud_host"] = env.get("CLOUD_RPC_HOST")
                state["cloud_port"] = env.get("CLOUD_RPC_PORT")
                state["compose_project"] = env.get("COMPOSE_PROJECT_NAME")
                state["edge_image"] = env.get("EDGE_IMAGE") or env.get("EDGE_VERSION")
                state["edge_version"] = state["edge_image"]
                try:
                    state["edge_http_port"] = int(env.get("EDGE_HTTP_PORT") or DEFAULT_EDGE_HTTP_PORT)
                except ValueError:
                    pass

            if state["docker_installed"]:
                # L'image edge porte un tag custom, absent des registres publics :
                # sa presence locale conditionne tout demarrage.
                wanted_image = state["edge_image"] or DEFAULT_EDGE_IMAGE
                image_id = ssh.out(
                    f"docker image inspect {shlex.quote(wanted_image)} "
                    "--format '{{.Id}}' 2>/dev/null || true"
                )
                state["image_present"] = bool(image_id)
                if not state["edge_image"]:
                    state["edge_image"] = wanted_image if image_id else None

                ps_line = ssh.out(
                    "docker ps -a --filter name=^/tb-edge$ --format '{{.State}}|{{.Status}}' 2>/dev/null"
                )
                if "|" in ps_line:
                    container_state, _, container_status = ps_line.partition("|")
                    state["container_state"] = container_state.strip() or "absent"
                    state["container_status"] = container_status.strip()

                state["postgres_status"] = ssh.out(
                    "docker ps -a --filter name=^/tb-edge-postgres$ --format '{{.Status}}' 2>/dev/null"
                ) or None

            if state["container_state"] == "running":
                logs = ssh.out(
                    "docker logs --tail 400 tb-edge 2>&1 | "
                    "grep -Ei 'edge (is )?connect|connected to cloud|Failed to establish|"
                    "UNAUTHORIZED|Unable to connect|routing key|Started ThingsBoardEdge|"
                    "License|handshake' | tail -12",
                    timeout=EXEC_TIMEOUT,
                )
                lowered = (logs or "").lower()
                if "unauthorized" in lowered or "failed to establish" in lowered or "unable to connect" in lowered:
                    state["cloud_connected"] = False
                    state["cloud_hint"] = _last_meaningful_line(logs)
                elif "connected to cloud" in lowered or "edge connected" in lowered or "started thingsboardedge" in lowered:
                    state["cloud_connected"] = True
                    state["cloud_hint"] = _last_meaningful_line(logs)
                else:
                    state["cloud_hint"] = _last_meaningful_line(logs) or "Demarrage en cours..."

            # Gateway I/O Python (executee nativement en systemd)
            gateway_files = ssh.out(
                f"ls -1 {shlex.quote(GATEWAY_DIR)} 2>/dev/null | wc -l || echo 0"
            )
            try:
                state["gateway_present"] = int((gateway_files or "0").strip()) > 0
            except ValueError:
                state["gateway_present"] = False

            if state["gateway_present"]:
                state["gateway_service_state"] = ssh.out(
                    f"systemctl is-active {shlex.quote(GATEWAY_SERVICE)} 2>/dev/null || echo absent"
                )
                gpio_errors = ssh.out(
                    f"grep -Ec 'Device or resource busy|not configure yet' "
                    f"{shlex.quote(GATEWAY_LOG)} 2>/dev/null || echo 0"
                )
                try:
                    state["gateway_gpio_errors"] = int((gpio_errors or "0").strip())
                except ValueError:
                    state["gateway_gpio_errors"] = 0

            state["edge_url"] = f"http://{state['ip']}:{state['edge_http_port']}"
            state["status"] = _derive_status(state)

    except paramiko.AuthenticationException:
        state["error"] = "Authentification SSH refusee (verifier utilisateur/mot de passe)."
        state["status"] = "error"
    except Exception as exception:
        state["error"] = f"SSH indisponible: {exception}"
        state["status"] = "error"

    return state


def _last_meaningful_line(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else None


def _derive_status(state):
    if not state["docker_installed"]:
        return "not_installed"
    if not state["configured"] and state["container_state"] == "absent":
        # Docker present mais image custom absente : la replication depuis la
        # carte de reference est necessaire avant tout provisioning.
        return "image_missing" if not state["image_present"] else "not_configured"
    if state["container_state"] == "running":
        return "connected" if state["cloud_connected"] else "starting"
    if state["container_state"] in {"exited", "created", "paused", "restarting", "dead"}:
        return "stopped"
    if state["configured"]:
        return "stopped"
    return "not_configured"


STATUS_LABELS = {
    "not_installed": ("Docker absent", "badge-warning"),
    "image_missing": ("Image edge absente", "badge-warning"),
    "not_configured": ("Edge non configure", "badge-warning"),
    "stopped": ("Edge arrete", "badge-danger"),
    "starting": ("Demarrage / non connecte", "badge-info"),
    "connected": ("Connecte au serveur", "badge-success"),
    "error": ("Injoignable", "badge-danger"),
    "unknown": ("Inconnu", "badge-warning"),
}


def status_label(status):
    return STATUS_LABELS.get(status, STATUS_LABELS["unknown"])


# ------------------------------------------------------------------ DEPLOY
def deploy_edge(ip, user, password, options, on_line, port=22):
    """
    Pousse et execute le script de provisioning sur la carte.

    options (dict) :
      edge_key, edge_secret, cloud_host   (obligatoires)
      cloud_port, cloud_ssl, edge_version, edge_dir, edge_http_port,
      edge_mqtt_port, edge_name, pg_password,
      reset_data, set_hostname, hostname_prefix, install_docker,
      pull_images, start_edge
    Retour : (ok: bool, message: str)
    """
    ok, edge_key = validate_edge_key(options.get("edge_key"))
    if not ok:
        return False, edge_key
    ok, edge_secret = validate_edge_secret(options.get("edge_secret"))
    if not ok:
        return False, edge_secret
    ok, cloud_host = validate_host(options.get("cloud_host"))
    if not ok:
        return False, cloud_host

    if not os.path.exists(PROVISION_SCRIPT):
        return False, f"Script de provisioning introuvable: {PROVISION_SCRIPT}"

    edge_dir = options.get("edge_dir") or DEFAULT_EDGE_DIR

    env_pairs = {
        "EDGE_KEY": edge_key,
        "EDGE_SECRET": edge_secret,
        "CLOUD_RPC_HOST": cloud_host,
        "CLOUD_RPC_PORT": str(options.get("cloud_port") or DEFAULT_CLOUD_RPC_PORT),
        "CLOUD_RPC_SSL": "true" if options.get("cloud_ssl") else "false",
        "EDGE_IMAGE": options.get("edge_image") or options.get("edge_version") or DEFAULT_EDGE_IMAGE,
        "EDGE_DIR": edge_dir,
        "COMPOSE_PROJECT": options.get("compose_project") or DEFAULT_COMPOSE_PROJECT,
        "EDGE_HTTP_PORT": str(options.get("edge_http_port") or DEFAULT_EDGE_HTTP_PORT),
        "EDGE_MQTT_PORT": str(options.get("edge_mqtt_port") or DEFAULT_EDGE_MQTT_PORT),
        "PG_PASSWORD": options.get("pg_password") or "postgres",
        "RESET_DATA": "1" if options.get("reset_data", True) else "0",
        "SET_HOSTNAME": "1" if options.get("set_hostname") else "0",
        "HOSTNAME_PREFIX": options.get("hostname_prefix") or "modberry",
        "INSTALL_DOCKER": "1" if options.get("install_docker") else "0",
        "PULL_IMAGES": "1" if options.get("pull_images", True) else "0",
        "START_EDGE": "1" if options.get("start_edge", True) else "0",
    }
    if options.get("edge_name"):
        env_pairs["EDGE_NAME"] = options["edge_name"]
    if options.get("docker_data_root"):
        env_pairs["DOCKER_DATA_ROOT"] = options["docker_data_root"]

    env_prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in env_pairs.items())

    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            on_line(f"Connexion SSH etablie sur {ip}.")
            on_line("Envoi du script de provisioning...")
            ssh.put_file(PROVISION_SCRIPT, REMOTE_SCRIPT)
            on_line(f"Script deploye dans {REMOTE_SCRIPT}.")

            sudo = "" if user == "root" else "sudo -n -E "
            command = f"{sudo}env {env_prefix} bash {REMOTE_SCRIPT} 2>&1"
            on_line("Execution du provisioning (cela peut prendre plusieurs minutes)...")

            rc = ssh.run_streaming(command, on_line, timeout=DEPLOY_TIMEOUT)
            ssh.run(f"rm -f {REMOTE_SCRIPT}", timeout=10)

            if rc == 0:
                return True, "Provisioning termine avec succes."
            return False, f"Le script a retourne le code {rc}."
    except paramiko.AuthenticationException:
        return False, "Authentification SSH refusee."
    except Exception as exception:
        return False, f"Erreur SSH: {exception}"


# ----------------------------------------------------------------- CONTROL
CONTROL_COMMANDS = {
    "start": "up -d",
    "stop": "stop",
    "restart": "restart",
    "down": "down",
}


def control_edge(
    ip,
    user,
    password,
    action,
    edge_dir=DEFAULT_EDGE_DIR,
    compose_project=DEFAULT_COMPOSE_PROJECT,
    port=22,
):
    """start | stop | restart | down la stack docker de l'Edge."""
    if action not in CONTROL_COMMANDS:
        return False, f"Action inconnue: {action}"

    sudo = "" if user == "root" else "sudo -n "
    subcommand = CONTROL_COMMANDS[action]
    project = f"-p {shlex.quote(compose_project)} " if compose_project else ""
    command = (
        f"cd {shlex.quote(edge_dir)} && "
        f"({sudo}docker compose {project}{subcommand} "
        f"|| {sudo}docker-compose {project}{subcommand}) 2>&1"
    )
    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            rc, out, err = ssh.run(command, timeout=180)
            output = (out or err or "").strip()
            if rc == 0:
                return True, output or f"Action '{action}' executee."
            return False, output or f"Action '{action}' echouee (code {rc})."
    except Exception as exception:
        return False, f"Erreur SSH: {exception}"


def fetch_edge_logs(ip, user, password, lines=200, port=22):
    """Recupere les dernieres lignes de logs du conteneur tb-edge."""
    sudo_prefix = "" if user == "root" else "sudo -n "
    command = (
        f"docker logs --tail {int(lines)} tb-edge 2>&1 "
        f"|| {sudo_prefix}docker logs --tail {int(lines)} tb-edge 2>&1"
    )
    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            _, out, err = ssh.run(command, timeout=60)
            return True, (out or err or "Aucun log disponible.")
    except Exception as exception:
        return False, f"Erreur SSH: {exception}"
