#!/usr/bin/env python3
"""
ModBerry Manager - Interface web pour la gestion des ModBerry CM5
Version: 1.0
"""

import json
import os
import ipaddress
import re
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import wraps

import paramiko
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

import tb_edge
import tb_master
import tb_preflight
from jobs import manager as job_manager
from tb_cloud import ThingsBoardClient, ThingsBoardError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

app = Flask(__name__)
app.secret_key = os.environ.get("MODBERRY_SECRET_KEY", "votre_cle_secrete_a_changer")

SSH_USER = os.environ.get("MODBERRY_SSH_USER", "root")
SSH_PASSWORD = os.environ.get("MODBERRY_SSH_PASSWORD", "techbase")
SSH_TIMEOUT = 2
PING_TIMEOUT = 1
MAX_WORKERS = 32
AUTO_SCAN_INTERVAL_SECONDS = int(os.environ.get("MODBERRY_AUTO_SCAN_INTERVAL_SECONDS", 7200))
DEFAULT_WEB_IDENTITY = os.environ.get("MODBERRY_WEB_IDENTITY", "admin-ip")
DEFAULT_WEB_EMAIL = os.environ.get("MODBERRY_WEB_EMAIL", "admin-ip@modberry.local")
DEFAULT_WEB_PASSWORD = os.environ.get("MODBERRY_WEB_PASSWORD", "takieddine")

# Identifiants tenant ThingsBoard du parc Mobilis : ce sont toujours les memes
# sur cette infrastructure, on les prefixe donc dans l'interface. Le mot de
# passe n'est utilise que le temps de l'appel REST, jamais persiste.
DEFAULT_TB_USERNAME = os.environ.get("MODBERRY_TB_USERNAME", "tenant@mobilis.dz")
DEFAULT_TB_PASSWORD = os.environ.get("MODBERRY_TB_PASSWORD", "tenant")

# Valeurs par defaut alignees sur la carte de reference techbase (10.0.0.26).
DEFAULT_EDGE_SETTINGS = {
    "cloud_host": os.environ.get("MODBERRY_TB_HOST", "10.0.0.1"),
    "cloud_port": int(os.environ.get("MODBERRY_TB_RPC_PORT", "7071")),
    "cloud_ssl": False,
    "cloud_web_url": os.environ.get("MODBERRY_TB_URL", ""),
    "tb_username": DEFAULT_TB_USERNAME,
    "master_ip": os.environ.get("MODBERRY_MASTER_IP", tb_master.DEFAULT_MASTER_IP),
    "edge_image": tb_edge.DEFAULT_EDGE_IMAGE,
    "edge_dir": tb_edge.DEFAULT_EDGE_DIR,
    "compose_project": tb_edge.DEFAULT_COMPOSE_PROJECT,
    "edge_http_port": tb_edge.DEFAULT_EDGE_HTTP_PORT,
    "edge_mqtt_port": tb_edge.DEFAULT_EDGE_MQTT_PORT,
    "docker_data_root": "",
    "hostname_prefix": "modberry",
    "install_docker": True,
    "reset_data": True,
    "set_hostname": True,
    "pull_images": True,
    "start_edge": True,
    "transfer_image": True,
    "force_image_transfer": False,
    # Base locale de l'edge : sans elle, tb-edge meurt sur
    # « Connection to localhost:5432 refused ».
    "pg_image": tb_edge.DEFAULT_PG_IMAGE,
    "transfer_postgres": True,
    # Relance automatique de la stack apres un reboot de la carte.
    "enable_on_boot": True,
    "transfer_gateway": True,
    "install_gateway_service": True,
    "enable_gateway_service": False,  # bug GPIO a corriger avant demarrage
    # Verification des dependances avant toute operation longue : evite de
    # perdre 6 minutes de transfert sur un « docker: command not found ».
    "preflight_check": True,
}

scan_lock = threading.Lock()
scan_scheduler_started = False
scan_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "devices": 0,
    "modberries": 0,
    "networks": 0,
    "completed_networks": 0,
    "total_networks": 0,
    "current_network": None,
    "message": "idle",
}


def iso_now():
    return datetime.now().isoformat()


def load_config():
    """Charge la configuration."""
    default_config = {
        "users": {
            DEFAULT_WEB_IDENTITY: {
                "password": DEFAULT_WEB_PASSWORD,
                "email": DEFAULT_WEB_EMAIL,
                "display_name": "Admin",
            }
        },
        "devices": [],
        "modberries": [],
        "networks": [],
        "last_scan": None,
        "edge_settings": dict(DEFAULT_EDGE_SETTINGS),
        "edge_states": {},
        "edge_assignments": {},
    }

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as file_handle:
            config = json.load(file_handle)

        config.setdefault("users", {})
        config.setdefault("devices", [])
        config.setdefault("modberries", [])
        config.setdefault("networks", [])
        config.setdefault("last_scan", None)
        config.setdefault("edge_states", {})
        config.setdefault("edge_assignments", {})

        edge_settings = config.setdefault("edge_settings", {})
        for key, value in DEFAULT_EDGE_SETTINGS.items():
            edge_settings.setdefault(key, value)

        if DEFAULT_WEB_IDENTITY not in config["users"]:
            config["users"][DEFAULT_WEB_IDENTITY] = {
                "password": DEFAULT_WEB_PASSWORD,
                "email": DEFAULT_WEB_EMAIL,
                "display_name": "Admin",
            }

        return config

    save_config(default_config)
    return default_config


def save_config(config):
    """Sauvegarde la configuration."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as file_handle:
        json.dump(config, file_handle, indent=2, ensure_ascii=False)


def login_required(function):
    """Décorateur pour protéger les routes."""

    @wraps(function)
    def decorated_function(*args, **kwargs):
        if "logged_in" not in session:
            return redirect(url_for("login"))
        return function(*args, **kwargs)

    return decorated_function


def is_docker_network(network_str):
    """Vérifie si un réseau est Docker."""
    try:
        network = ipaddress.ip_network(network_str, strict=False)
    except ValueError:
        return True

    docker_ranges = [ipaddress.ip_network(f"172.{index}.0.0/16") for index in range(16, 32)]

    for docker_range in docker_ranges:
        try:
            if network.subnet_of(docker_range) or network == docker_range:
                return True
        except ValueError:
            pass

    return False


def get_physical_interfaces():
    """Récupère les interfaces physiques."""
    interfaces = []
    try:
        result = subprocess.check_output(["ip", "a"], text=True)
        pattern = r"^\d+: (eth[0-9]+|eno[0-9]+|enp[0-9]+s[0-9]+|enx[0-9a-f]+|wlan[0-9]+|usb[0-9]+):"
        for line in result.split("\n"):
            match = re.match(pattern, line)
            if match:
                interfaces.append(match.group(1))
    except Exception:
        pass
    return interfaces


def get_physical_networks():
    """Détecte les réseaux physiques."""
    networks = set()
    physical_interfaces = get_physical_interfaces()

    try:
        result = subprocess.check_output(["ip", "a"], text=True)
        current_iface = None
        for line in result.split("\n"):
            iface_match = re.match(r"^\d+: ([\w\-]+):", line)
            if iface_match:
                current_iface = iface_match.group(1)
                continue

            if current_iface and current_iface in physical_interfaces and "inet " in line:
                ip_match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
                if not ip_match:
                    continue

                ip = ip_match.group(1)
                mask = int(ip_match.group(2))
                if ip.startswith("127."):
                    continue

                try:
                    network = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
                    if 16 <= mask <= 30 and not is_docker_network(str(network)):
                        networks.add(str(network))
                except ValueError:
                    pass
    except Exception:
        pass

    default_networks = [
        "10.0.0.0/24",
        "192.168.0.0/24",
        "192.168.1.0/24",
        "192.168.4.0/24",
        "192.168.5.0/24",
        "192.168.14.0/24",
        "192.168.42.0/24",
    ]

    for network in default_networks:
        if not is_docker_network(network):
            networks.add(network)

    filtered_networks = [network for network in networks if not network.endswith("/32") and not network.endswith("/0")]
    return sorted(filtered_networks, key=lambda item: ipaddress.ip_network(item).prefixlen, reverse=True)


def expand_scan_networks(network_str):
    """Découpe un réseau large en sous-réseaux /24 pour le scan."""
    try:
        network = ipaddress.ip_network(network_str, strict=False)
    except ValueError:
        return []

    if network.prefixlen < 24:
        return [str(subnet) for subnet in network.subnets(new_prefix=24)]

    return [str(network)]


def ping_host(ip):
    """Ping un hôte."""
    try:
        subprocess.check_output(
            ["ping", "-c", "1", "-W", str(PING_TIMEOUT), str(ip)],
            stderr=subprocess.DEVNULL,
            timeout=PING_TIMEOUT + 1,
        )
        return True
    except Exception:
        return False


def check_port(ip, port):
    """Vérifie si un port est ouvert."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        result = sock.connect_ex((str(ip), port))
        sock.close()
        return result == 0
    except Exception:
        return False


def is_modberry_device(hostname, serial):
    """Détecte si l'hôte ressemble à une ModBerry."""
    hostname = (hostname or "").lower()
    serial = (serial or "").lower()
    return "techbase" in hostname or "modberry" in hostname or serial not in {"", "unknown", "error"}


def _ssh_client(ip):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(str(ip), username=SSH_USER, password=SSH_PASSWORD, timeout=SSH_TIMEOUT)
    return ssh


def get_serial_via_ssh(ip):
    """Récupère le serial via SSH."""
    try:
        ssh = _ssh_client(ip)
        _, stdout, _ = ssh.exec_command("cat /proc/device-tree/serial-number 2>/dev/null || echo 'unknown'")
        serial = stdout.read().decode().strip()
        ssh.close()
        return serial if serial else "unknown"
    except Exception:
        return "error"


def get_hostname_via_ssh(ip):
    """Récupère le hostname via SSH."""
    try:
        ssh = _ssh_client(ip)
        _, stdout, _ = ssh.exec_command("hostname")
        hostname = stdout.read().decode().strip()
        ssh.close()
        return hostname if hostname else "unknown"
    except Exception:
        return "unknown"


def get_tb_edge_status(ip):
    """Vérifie si ThingsBoard Edge tourne."""
    try:
        ssh = _ssh_client(ip)
        _, stdout, _ = ssh.exec_command("docker ps --filter name=tb-edge --format '{{.Status}}' 2>/dev/null || echo 'not installed'")
        status = stdout.read().decode().strip()
        ssh.close()
        if status and status != "not installed":
            return status
        return "not installed"
    except Exception:
        return "unknown"


def probe_host(ip, subnet_str):
    """Inspecte un hôte et retourne un enregistrement normalisé."""
    online = ping_host(ip)
    ssh_open = check_port(ip, 22)

    if not online and not ssh_open:
        return None

    device = {
        "ip": str(ip),
        "hostname": "unknown",
        "serial": "unknown",
        "tb_edge": "not installed",
        "subnet": subnet_str,
        "online": True,
        "ssh": ssh_open,
        "kind": "reachable",
    }

    if ssh_open:
        hostname = get_hostname_via_ssh(ip)
        serial = get_serial_via_ssh(ip)
        device["hostname"] = hostname
        device["serial"] = serial
        if is_modberry_device(hostname, serial):
            device["kind"] = "modberry"
            device["tb_edge"] = get_tb_edge_status(ip)
        else:
            device["kind"] = "ssh-device"

    return device


def scan_subnet(subnet_str):
    """Scanne un sous-réseau."""
    found = []
    try:
        network = ipaddress.ip_network(subnet_str, strict=False)
        with scan_lock:
            scan_state["current_network"] = subnet_str
        for target_network in expand_scan_networks(str(network)):
            target = ipaddress.ip_network(target_network, strict=False)
            hosts = list(target.hosts())[:254]

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(probe_host, ip, target_network) for ip in hosts]
                for future in as_completed(futures):
                    device = future.result()
                    if device:
                        found.append(device)
    except Exception:
        pass

    return found


def scan_all_networks():
    """Scanne tous les réseaux physiques."""
    all_devices = []
    for network_str in get_physical_networks():
        all_devices.extend(scan_subnet(network_str))

    deduped = {}
    for device in all_devices:
        deduped[device["ip"]] = device

    return list(deduped.values())


def split_devices(devices):
    """Sépare les ModBerry des autres hôtes."""
    modberries = [device for device in devices if device.get("kind") == "modberry"]
    return devices, modberries


def get_scan_summary():
    config = load_config()
    with scan_lock:
        summary = dict(scan_state)

    summary["auto_scan_enabled"] = config.get("scan_settings", {}).get("auto_scan_enabled", True)
    summary["auto_scan_interval_seconds"] = config.get("scan_settings", {}).get("auto_scan_interval_seconds", AUTO_SCAN_INTERVAL_SECONDS)
    summary["device_count"] = len(config.get("devices", []))
    summary["modberry_count"] = len(config.get("modberries", []))
    summary["network_count"] = len(config.get("networks", []))
    summary["last_scan"] = config.get("last_scan")
    return summary


def compute_next_auto_scan_at(config):
    last_scan = config.get("last_scan")
    if not last_scan:
        return None

    try:
        last_scan_dt = datetime.fromisoformat(last_scan)
        interval_seconds = config.get("scan_settings", {}).get("auto_scan_interval_seconds", AUTO_SCAN_INTERVAL_SECONDS)
        return datetime.fromtimestamp(last_scan_dt.timestamp() + interval_seconds).isoformat()
    except Exception:
        return None


def can_start_scan():
    """Vrai si aucun scan n'est deja en cours."""
    with scan_lock:
        return not scan_state["running"]


def trigger_scan(source="manual"):
    with scan_lock:
        if scan_state["running"]:
            return False

        scan_state["running"] = True
        scan_state["started_at"] = iso_now()
        scan_state["finished_at"] = None
        scan_state["last_triggered_at"] = iso_now()
        scan_state["last_source"] = source
        scan_state["message"] = f"running:{source}"

    threading.Thread(target=run_scan_job, daemon=True).start()
    return True


def auto_scan_loop():
    while True:
        config = load_config()
        settings = config.get("scan_settings", {})
        interval_seconds = int(settings.get("auto_scan_interval_seconds", AUTO_SCAN_INTERVAL_SECONDS))
        enabled = settings.get("auto_scan_enabled", True)

        if not enabled:
            threading.Event().wait(60)
            continue

        next_auto_scan_at = compute_next_auto_scan_at(config)
        if next_auto_scan_at:
            try:
                next_scan_dt = datetime.fromisoformat(next_auto_scan_at)
                delay_seconds = max(0, int((next_scan_dt - datetime.now()).total_seconds()))
                threading.Event().wait(min(delay_seconds, 60))
            except Exception:
                threading.Event().wait(60)
        else:
            if can_start_scan():
                trigger_scan(source="auto")
            threading.Event().wait(interval_seconds)

        if next_auto_scan_at:
            try:
                next_scan_dt = datetime.fromisoformat(next_auto_scan_at)
                if datetime.now() >= next_scan_dt and can_start_scan():
                    trigger_scan(source="auto")
            except Exception:
                pass


def start_scan_scheduler_once():
    global scan_scheduler_started
    with scan_lock:
        if scan_scheduler_started:
            return
        scan_scheduler_started = True

    threading.Thread(target=auto_scan_loop, daemon=True).start()


def run_scan_job():
    """Exécute le scan en arrière-plan et persiste le résultat."""
    error_message = None
    config = None
    try:
        networks = get_physical_networks()
        with scan_lock:
            scan_state["total_networks"] = len(networks)
            scan_state["completed_networks"] = 0
            scan_state["current_network"] = None

        devices = []
        for index, network_str in enumerate(networks, start=1):
            with scan_lock:
                scan_state["current_network"] = network_str
                scan_state["completed_networks"] = index - 1

            devices.extend(scan_subnet(network_str))

        devices, modberries = split_devices(devices)
        config = load_config()
        config["devices"] = devices
        config["modberries"] = modberries
        config["networks"] = networks
        config["last_scan"] = datetime.now().isoformat()
        save_config(config)
    except Exception as exception:
        error_message = str(exception)
    finally:
        with scan_lock:
            scan_state["running"] = False
            scan_state["finished_at"] = datetime.now().isoformat()
            scan_state["started_at"] = None
            scan_state["current_network"] = None
            scan_state["completed_networks"] = scan_state.get("total_networks", 0)
            if error_message:
                scan_state["message"] = f"error: {error_message}"
            else:
                scan_state["message"] = "completed"
            scan_state["devices"] = len(config.get("devices", [])) if config else scan_state["devices"]
            scan_state["modberries"] = len(config.get("modberries", [])) if config else scan_state["modberries"]
            scan_state["networks"] = len(config.get("networks", [])) if config else scan_state["networks"]


def change_ip(ip, new_ip, new_mask="24", gateway="10.0.0.1", dns="8.8.8.8"):
    """Change l'IP d'un ModBerry."""
    try:
        ssh = _ssh_client(ip)
        stdin, stdout, stderr = ssh.exec_command(
            r"nmcli -t -f NAME,TYPE,DEVICE connection show --active | awk -F: '$2 ~ /(ethernet|802-3-ethernet)/ {print $1; exit}'"
        )
        active_connection = stdout.read().decode().strip() or "Wired connection 1"
        commands = [
            f'nmcli connection modify "{active_connection}" ipv4.addresses {new_ip}/{new_mask}',
            f'nmcli connection modify "{active_connection}" ipv4.gateway {gateway}',
            f'nmcli connection modify "{active_connection}" ipv4.dns "{dns}"',
            f'nmcli connection modify "{active_connection}" ipv4.method manual',
            f'nmcli connection down "{active_connection}"',
            f'nmcli connection up "{active_connection}"',
        ]

        for command in commands:
            _, stdout, _ = ssh.exec_command(command)
            stdout.read().decode().strip()

        ssh.close()
        return True, f"IP changée vers {new_ip}"
    except Exception as exception:
        return False, f"Erreur: {exception}"


@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identity = request.form.get("identity")
        password = request.form.get("password")
        config = load_config()

        for username, user_data in config.get("users", {}).items():
            if password == user_data.get("password") and identity in {username, user_data.get("email")}:
                session["logged_in"] = True
                session["username"] = username
                session["email"] = user_data.get("email", identity)
                return redirect(url_for("dashboard"))

        flash("Identifiant ou mot de passe incorrect", "error")
        return render_template("login.html")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    config = load_config()
    return render_template(
        "dashboard.html",
        devices=config.get("devices", []),
        modberries=config.get("modberries", []),
        networks=config.get("networks", []),
        last_scan=config.get("last_scan"),
        scan_state=get_scan_summary(),
        scan_settings=config.get("scan_settings", {}),
    )


@app.route("/scan", methods=["POST"])
def scan():
    """Déclenche un scan réseau."""
    if not trigger_scan(source="manual"):
        return jsonify({"success": True, "running": True, "started": False, "message": "Scan deja en cours"}), 202

    return jsonify({"success": True, "running": True, "started": True, "message": "Scan demarre en arriere-plan"}), 202


@app.route("/edit_ip/<ip>", methods=["GET", "POST"])
@login_required
def edit_ip(ip):
    if request.method == "POST":
        new_ip = request.form.get("new_ip")
        new_mask = request.form.get("new_mask", "24")
        gateway = request.form.get("gateway", "10.0.0.1")
        dns = request.form.get("dns", "8.8.8.8")

        success, message = change_ip(ip, new_ip, new_mask, gateway, dns)
        if success:
            flash(f"✅ {message}", "success")
            devices = scan_all_networks()
            devices, modberries = split_devices(devices)
            config = load_config()
            config["devices"] = devices
            config["modberries"] = modberries
            config["networks"] = get_physical_networks()
            config["last_scan"] = datetime.now().isoformat()
            save_config(config)
            return redirect(url_for("dashboard"))

        flash(f"❌ {message}", "error")
        return redirect(url_for("edit_ip", ip=ip))

    config = load_config()
    modberry = next((item for item in config.get("devices", []) if item.get("ip") == ip), None)
    if not modberry:
        modberry = next((item for item in config.get("modberries", []) if item.get("ip") == ip), None)
    if not modberry:
        flash("ModBerry non trouvé", "error")
        return redirect(url_for("dashboard"))

    return render_template("edit_ip.html", modberry=modberry)


@app.route("/api/modberries")
@login_required
def api_modberries():
    """API pour récupérer la liste des ModBerry."""
    return jsonify(load_config().get("modberries", []))


@app.route("/api/devices")
@login_required
def api_devices():
    """API pour récupérer tous les périphériques découverts."""
    return jsonify(load_config().get("devices", []))


@app.route("/api/networks")
@login_required
def api_networks():
    """API pour récupérer les réseaux détectés."""
    return jsonify(get_physical_networks())


@app.route("/api/scan/status")
@login_required
def api_scan_status():
    """Retourne l'état du scan en cours."""
    return jsonify(get_scan_summary())


@app.route("/api/system/status")
@login_required
def api_system_status():
    """Retourne un résumé système utile pour le dashboard."""
    config = load_config()
    return jsonify(
        {
            "service": "modberry_manager",
            "auto_scan_enabled": config.get("scan_settings", {}).get("auto_scan_enabled", True),
            "auto_scan_interval_seconds": config.get("scan_settings", {}).get("auto_scan_interval_seconds", AUTO_SCAN_INTERVAL_SECONDS),
            "next_auto_scan_at": compute_next_auto_scan_at(config),
            "last_scan": config.get("last_scan"),
            "device_count": len(config.get("devices", [])),
            "modberry_count": len(config.get("modberries", [])),
            "network_count": len(config.get("networks", [])),
        }
    )


@app.route("/api/export")
@login_required
def api_export():
    """Exporte l'état courant en JSON."""
    config = load_config()
    return jsonify(
        {
            "scan": get_scan_summary(),
            "devices": config.get("devices", []),
            "modberries": config.get("modberries", []),
            "networks": config.get("networks", []),
            "users": list(config.get("users", {}).keys()),
        }
    )


# =============================================================================
#  THINGSBOARD EDGE - configuration, provisioning et supervision
# =============================================================================


def get_edge_settings():
    return load_config().get("edge_settings", dict(DEFAULT_EDGE_SETTINGS))


def save_edge_settings(updates):
    config = load_config()
    settings = config.setdefault("edge_settings", dict(DEFAULT_EDGE_SETTINGS))
    settings.update(updates)
    save_config(config)
    return settings


def find_device(ip):
    config = load_config()
    for bucket in ("devices", "modberries"):
        for device in config.get(bucket, []):
            if device.get("ip") == ip:
                return device
    return None


def get_key_assignments(exclude_ip=None):
    """
    Retourne {routing_key: ip} des cles Edge deja affectees.

    Sert a empecher qu'une meme paire Cle/Secret soit deployee sur deux cartes :
    deux edges partageant la meme identite entrent en conflit sur le serveur.
    """
    config = load_config()
    assignments = {}

    for ip, state in config.get("edge_states", {}).items():
        key = (state or {}).get("edge_key")
        if key and str(ip) != str(exclude_ip):
            assignments[key] = ip

    for ip, record in config.get("edge_assignments", {}).items():
        key = (record or {}).get("edge_key")
        if key and str(ip) != str(exclude_ip):
            assignments[key] = ip

    return assignments


def record_key_assignment(ip, edge_key, edge_name=None):
    """Memorise la cle affectee a une carte (registre du parc)."""
    config = load_config()
    config.setdefault("edge_assignments", {})[ip] = {
        "edge_key": edge_key,
        "edge_name": edge_name,
        "assigned_at": iso_now(),
    }
    save_config(config)


def store_edge_state(ip, state):
    """Persiste l'etat Edge d'une carte et met a jour la colonne tb_edge."""
    config = load_config()
    config.setdefault("edge_states", {})[ip] = state

    summary = state.get("container_status") or tb_edge.status_label(state.get("status"))[0]
    for bucket in ("devices", "modberries"):
        for device in config.get(bucket, []):
            if device.get("ip") == ip:
                device["tb_edge"] = summary
                device["edge_status"] = state.get("status")
                device["edge_key"] = state.get("edge_key")
                device["edge_connected"] = state.get("cloud_connected")

    save_config(config)
    return state


def ssh_credentials():
    return SSH_USER, SSH_PASSWORD


def parse_bool(value):
    return str(value).strip().lower() in {"1", "true", "on", "yes", "oui"}


def parse_int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@app.route("/edge/<ip>")
@login_required
def edge_page(ip):
    """Page de configuration ThingsBoard Edge pour une carte."""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        flash("Adresse IP invalide.", "error")
        return redirect(url_for("dashboard"))

    device = find_device(ip)
    if not device:
        # Carte pas encore scannee : on autorise la configuration directe par IP,
        # utile pour provisionner une carte neuve avant tout scan complet.
        device = {
            "ip": ip,
            "hostname": "inconnu (hors scan)",
            "serial": "",
            "subnet": "",
            "ssh": True,
            "kind": "manual",
        }
        flash(
            f"{ip} n'apparait pas dans le dernier scan : la page est ouverte en saisie directe.",
            "info",
        )

    config = load_config()
    settings = config.get("edge_settings", dict(DEFAULT_EDGE_SETTINGS))
    state = config.get("edge_states", {}).get(ip)
    job = job_manager.snapshot(f"deploy:{ip}")

    return render_template(
        "edge.html",
        device=device,
        settings=settings,
        state=state,
        status_label=tb_edge.status_label,
        job=job,
        default_edge_version=tb_edge.DEFAULT_EDGE_VERSION,
        default_tb_username=settings.get("tb_username") or DEFAULT_TB_USERNAME,
        default_tb_password=DEFAULT_TB_PASSWORD,
    )


@app.route("/api/edge/<ip>/probe", methods=["POST", "GET"])
@login_required
def api_edge_probe(ip):
    """Inspecte l'etat de ThingsBoard Edge sur la carte."""
    settings = get_edge_settings()
    user, password = ssh_credentials()
    state = tb_edge.probe_edge(
        ip,
        user,
        password,
        edge_dir=settings.get("edge_dir", tb_edge.DEFAULT_EDGE_DIR),
    )
    store_edge_state(ip, state)

    label, css_class = tb_edge.status_label(state.get("status"))
    return jsonify({"success": True, "state": state, "label": label, "css_class": css_class})


# ------------------- Prerequis : Docker et dependances systeme ---------------


@app.route("/api/edge/<ip>/preflight", methods=["POST", "GET"])
@login_required
def api_edge_preflight(ip):
    """
    Verifie les prerequis avant deploiement : SSH, privileges, Docker, compose,
    outils systeme, espace disque, image sur la carte de reference.

    A appeler AVANT le transfert de l'image : c'est ce qui evite de pousser
    ~1 Go pour finir sur « docker: command not found » (code 127).
    """
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    settings = get_edge_settings()

    master_ip = (
        payload.get("master_ip") or settings.get("master_ip") or tb_master.DEFAULT_MASTER_IP
    ).strip()
    image = (payload.get("edge_image") or settings.get("edge_image") or tb_edge.DEFAULT_EDGE_IMAGE).strip()
    pg_image = (payload.get("pg_image") or settings.get("pg_image") or tb_edge.DEFAULT_PG_IMAGE).strip()
    edge_dir = (payload.get("edge_dir") or settings.get("edge_dir") or tb_edge.DEFAULT_EDGE_DIR).strip()
    need_image = parse_bool(payload.get("transfer_image", settings.get("transfer_image", True)))
    install_docker_allowed = parse_bool(
        payload.get("install_docker", settings.get("install_docker", True))
    )

    if str(master_ip) == str(ip):
        need_image = False

    user, password = ssh_credentials()
    report = tb_preflight.preflight_all(
        ip,
        master_ip,
        user,
        password,
        image=image,
        pg_image=pg_image,
        edge_dir=edge_dir,
        need_image_transfer=need_image,
        install_docker_allowed=install_docker_allowed,
    )
    return jsonify({"success": True, "preflight": report})


@app.route("/api/edge/<ip>/docker/install", methods=["POST"])
@login_required
def api_edge_docker_install(ip):
    """
    Installe Docker CE sur la carte, sans toucher a la stack Edge.

    Etape independante et rejouable : on prepare la carte, puis on transfere
    l'image seulement lorsque Docker repond.
    """
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    settings = get_edge_settings()
    docker_data_root = (
        payload.get("docker_data_root") or settings.get("docker_data_root") or ""
    ).strip()

    user, password = ssh_credentials()

    def target(job):
        job.log(f"=== Installation des dependances Docker sur {ip} ===")
        ok, message = tb_preflight.install_docker(
            ip, user, password, on_line=job.log,
            docker_data_root=docker_data_root or None,
        )
        if ok:
            job.log("")
            job.log("Nouvelle verification des prerequis...")
            report = tb_preflight.preflight_target(
                ip, user, password,
                image=settings.get("edge_image", tb_edge.DEFAULT_EDGE_IMAGE),
                need_image_transfer=True,
            )
            job.log(report["summary"])
            job.meta["preflight"] = report
        return ok, message

    started, job = job_manager.start(
        f"deploy:{ip}", f"Installation Docker {ip}", target,
        meta={"ip": ip, "kind": "docker-install"},
    )
    if not started:
        return jsonify(
            {"success": True, "started": False, "running": True, "job": job.snapshot(0),
             "message": "Une operation est deja en cours sur cette carte."}
        ), 202

    return jsonify({"success": True, "started": True, "running": True, "job": job.snapshot(0)}), 202


# --------------------------- Gestion des images Docker -----------------------


@app.route("/api/edge/<ip>/images", methods=["GET"])
@login_required
def api_edge_images(ip):
    """Liste les images Docker de la carte (filtre tb-edge par defaut)."""
    filter_text = request.args.get("filter", "tb-edge")
    if filter_text.lower() in {"all", "*", "tout"}:
        filter_text = ""

    user, password = ssh_credentials()
    return jsonify({"success": True, "docker": tb_master.list_images(
        ip, user, password, filter_text=filter_text
    )})


@app.route("/api/edge/<ip>/images/delete", methods=["POST"])
@login_required
def api_edge_image_delete(ip):
    """
    Supprime une image Docker de la carte.

    Indispensable avant de charger une nouvelle image edge : le stockage des
    CM5 ne permet pas de conserver plusieurs images de ~2.4 Go. La suppression
    force aussi le retransfert au prochain deploiement.
    """
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    settings = get_edge_settings()
    image = (payload.get("image") or settings.get("edge_image") or "").strip()
    if not image:
        return jsonify({"success": False, "error": "Nom d'image manquant."}), 400

    stop_stack = parse_bool(payload.get("stop_stack", True))
    user, password = ssh_credentials()
    messages = []

    # L'image est utilisee par la stack : on l'arrete d'abord, sinon rmi echoue.
    if stop_stack:
        stopped, stop_message = tb_edge.control_edge(
            ip, user, password, "down",
            edge_dir=settings.get("edge_dir", tb_edge.DEFAULT_EDGE_DIR),
            compose_project=settings.get("compose_project", tb_edge.DEFAULT_COMPOSE_PROJECT),
        )
        messages.append(
            f"Arret de la stack : {'ok' if stopped else 'non necessaire ou echoue'}"
        )

    ok, message = tb_master.delete_image(
        ip, user, password, image, on_line=lambda line: messages.append(line)
    )
    messages.append(message)

    docker = tb_master.list_images(ip, user, password, filter_text="tb-edge")

    state = None
    try:
        state = tb_edge.probe_edge(
            ip, user, password, edge_dir=settings.get("edge_dir", tb_edge.DEFAULT_EDGE_DIR)
        )
        store_edge_state(ip, state)
    except Exception:
        state = None

    return jsonify(
        {
            "success": ok,
            "message": message,
            "log": messages,
            "docker": docker,
            "state": state,
        }
    ), (200 if ok else 500)


@app.route("/api/edge/<ip>/images/transfer", methods=["POST"])
@login_required
def api_edge_image_transfer(ip):
    """
    Transfere (ou retransfere) l'image edge depuis la carte de reference,
    sans reconfigurer la stack. Verifie Docker au prealable.
    """
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    settings = get_edge_settings()

    master_ip = (payload.get("master_ip") or settings.get("master_ip") or tb_master.DEFAULT_MASTER_IP).strip()
    image = (payload.get("image") or payload.get("edge_image") or settings.get("edge_image") or tb_edge.DEFAULT_EDGE_IMAGE).strip()
    force = parse_bool(payload.get("force", False))
    install_docker_allowed = parse_bool(
        payload.get("install_docker", settings.get("install_docker", True))
    )

    if str(master_ip) == str(ip):
        return jsonify({"success": False, "error": "La cible est la carte de reference elle-meme."}), 400

    user, password = ssh_credentials()

    def target(job):
        job.log(f"=== Transfert de l'image {image} vers {ip} ===")
        job.log(f"Carte de reference : {master_ip} | force={force}")

        job.log("")
        job.log("--- Prerequis ---")
        report = tb_preflight.preflight_all(
            ip, master_ip, user, password,
            image=image,
            need_image_transfer=True,
            install_docker_allowed=install_docker_allowed,
        )
        for scope, part in report["reports"].items():
            if not part:
                continue
            job.log(f"[{scope}] {part['summary']}")
            for item in part["checks"]:
                marker = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}.get(item["level"], "    ")
                job.log(f"  {marker} {item['label']}: {item['value'] or '-'}")

        if not report["ok"]:
            details = "; ".join(f"{item['label']} ({item['scope']})" for item in report["blocking"])
            return False, f"Prerequis non satisfaits : {details}"

        if report["needs_docker_install"]:
            if not install_docker_allowed:
                return False, (
                    "Docker est absent de la carte : activez l'installation automatique "
                    "avant de transferer l'image."
                )
            job.log("")
            job.log("--- Installation de Docker CE (avant transfert) ---")
            ok, message = tb_preflight.install_docker(ip, user, password, on_line=job.log)
            job.log(message)
            if not ok:
                return False, message

        job.log("")
        job.log("--- Transfert de l'image ---")
        return tb_master.transfer_edge_image(
            master_ip, ip, user, password,
            image=image, on_line=job.log, force=force,
        )

    started, job = job_manager.start(
        f"deploy:{ip}", f"Image edge {ip}", target,
        meta={"ip": ip, "kind": "image-transfer", "image": image},
    )
    if not started:
        return jsonify(
            {"success": True, "started": False, "running": True, "job": job.snapshot(0),
             "message": "Une operation est deja en cours sur cette carte."}
        ), 202

    return jsonify({"success": True, "started": True, "running": True, "job": job.snapshot(0)}), 202


@app.route("/api/edge/<ip>/postgres/ensure", methods=["POST"])
@login_required
def api_edge_postgres_ensure(ip):
    """
    Garantit la presence de l'image postgres sur la carte.

    Pull si le registre repond, sinon replication depuis la carte de reference.
    Sans cette image, tb-edge demarre puis meurt sur
    « Connection to localhost:5432 refused ».
    """
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    settings = get_edge_settings()
    master_ip = (payload.get("master_ip") or settings.get("master_ip") or tb_master.DEFAULT_MASTER_IP).strip()
    image = (payload.get("pg_image") or settings.get("pg_image") or tb_edge.DEFAULT_PG_IMAGE).strip()
    allow_pull = parse_bool(payload.get("pull_images", settings.get("pull_images", True)))

    user, password = ssh_credentials()

    def target(job):
        job.log(f"=== Base locale postgres sur {ip} ===")
        job.log(f"Image : {image} | pull autorise : {allow_pull} | reference : {master_ip}")
        return tb_master.ensure_postgres_image(
            master_ip, ip, user, password,
            image=image, on_line=job.log, allow_pull=allow_pull,
        )

    started, job = job_manager.start(
        f"deploy:{ip}", f"Postgres {ip}", target,
        meta={"ip": ip, "kind": "postgres", "image": image},
    )
    if not started:
        return jsonify(
            {"success": True, "started": False, "running": True, "job": job.snapshot(0),
             "message": "Une operation est deja en cours sur cette carte."}
        ), 202

    return jsonify({"success": True, "started": True, "running": True, "job": job.snapshot(0)}), 202


@app.route("/api/edge/<ip>/diagnose", methods=["GET", "POST"])
@login_required
def api_edge_diagnose(ip):
    """
    Diagnostic complet d'une carte : pourquoi l'Edge ne tourne pas.

    Regroupe en un appel ce qu'il fallait aller chercher a la main en SSH :
    images presentes, conteneurs (y compris ceux lances manuellement),
    sante de postgres, causes d'erreur dans les logs, conflits de ports,
    relance au boot. Chaque probleme est accompagne du correctif.
    """
    settings = get_edge_settings()
    user, password = ssh_credentials()
    report = tb_preflight.diagnose(
        ip, user, password,
        edge_dir=settings.get("edge_dir", tb_edge.DEFAULT_EDGE_DIR),
        compose_project=settings.get("compose_project", tb_edge.DEFAULT_COMPOSE_PROJECT),
        edge_image=settings.get("edge_image", tb_edge.DEFAULT_EDGE_IMAGE),
        pg_image=settings.get("pg_image", tb_edge.DEFAULT_PG_IMAGE),
        edge_http_port=settings.get("edge_http_port", tb_edge.DEFAULT_EDGE_HTTP_PORT),
        edge_mqtt_port=settings.get("edge_mqtt_port", tb_edge.DEFAULT_EDGE_MQTT_PORT),
    )
    return jsonify({"success": True, "diagnose": report})


@app.route("/api/edge/<ip>/cleanup", methods=["POST"])
@login_required
def api_edge_cleanup(ip):
    """
    Supprime les conteneurs edge lances manuellement (`docker run`).

    Ces conteneurs portent un nom genere (ex. « loving_lamport »), n'ont ni
    base ni variables d'environnement, et bloquent les ports de la stack.
    """
    user, password = ssh_credentials()
    settings = get_edge_settings()
    ok, message, removed = tb_preflight.cleanup_stray_containers(
        ip, user, password,
        edge_image=settings.get("edge_image", tb_edge.DEFAULT_EDGE_IMAGE),
    )
    return jsonify({"success": ok, "message": message, "removed": removed}), (200 if ok else 500)


@app.route("/api/edge/<ip>/boot", methods=["POST"])
@login_required
def api_edge_boot(ip):
    """Active ou desactive la relance automatique de la stack au demarrage."""
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    enable = parse_bool(payload.get("enable", True))
    settings = get_edge_settings()
    user, password = ssh_credentials()

    ok, message = tb_preflight.set_boot_persistence(
        ip, user, password,
        enable=enable,
        edge_dir=settings.get("edge_dir", tb_edge.DEFAULT_EDGE_DIR),
        compose_project=settings.get("compose_project", tb_edge.DEFAULT_COMPOSE_PROJECT),
    )
    return jsonify({"success": ok, "message": message}), (200 if ok else 500)


@app.route("/api/edge/<ip>/deploy", methods=["POST"])
@login_required
def api_edge_deploy(ip):
    """Deploie / reconfigure ThingsBoard Edge sur la carte (tache longue)."""
    payload = request.get_json(silent=True) or request.form.to_dict()
    settings = get_edge_settings()

    options = {
        "edge_key": (payload.get("edge_key") or "").strip(),
        "edge_secret": (payload.get("edge_secret") or "").strip(),
        "cloud_host": (payload.get("cloud_host") or settings.get("cloud_host") or "").strip(),
        "cloud_port": parse_int(payload.get("cloud_port"), settings.get("cloud_port", tb_edge.DEFAULT_CLOUD_RPC_PORT)),
        "cloud_ssl": parse_bool(payload.get("cloud_ssl", settings.get("cloud_ssl", False))),
        "edge_image": (payload.get("edge_image") or settings.get("edge_image") or tb_edge.DEFAULT_EDGE_IMAGE).strip(),
        "edge_dir": (payload.get("edge_dir") or settings.get("edge_dir") or tb_edge.DEFAULT_EDGE_DIR).strip(),
        "compose_project": (payload.get("compose_project") or settings.get("compose_project") or tb_edge.DEFAULT_COMPOSE_PROJECT).strip(),
        "edge_http_port": parse_int(payload.get("edge_http_port"), settings.get("edge_http_port", tb_edge.DEFAULT_EDGE_HTTP_PORT)),
        "edge_mqtt_port": parse_int(payload.get("edge_mqtt_port"), settings.get("edge_mqtt_port", tb_edge.DEFAULT_EDGE_MQTT_PORT)),
        "edge_name": (payload.get("edge_name") or "").strip(),
        "pg_password": (payload.get("pg_password") or "postgres").strip(),
        "pg_image": (payload.get("pg_image") or settings.get("pg_image") or tb_edge.DEFAULT_PG_IMAGE).strip(),
        "enable_on_boot": parse_bool(payload.get("enable_on_boot", settings.get("enable_on_boot", True))),
        "docker_data_root": (payload.get("docker_data_root") or settings.get("docker_data_root") or "").strip(),
        "reset_data": parse_bool(payload.get("reset_data", True)),
        "set_hostname": parse_bool(payload.get("set_hostname", settings.get("set_hostname", True))),
        "hostname_prefix": (payload.get("hostname_prefix") or settings.get("hostname_prefix") or "modberry").strip(),
        "install_docker": parse_bool(payload.get("install_docker", settings.get("install_docker", True))),
        "pull_images": parse_bool(payload.get("pull_images", True)),
        "start_edge": parse_bool(payload.get("start_edge", True)),
    }

    # Etapes de replication depuis la carte de reference
    master_ip = (payload.get("master_ip") or settings.get("master_ip") or tb_master.DEFAULT_MASTER_IP).strip()
    do_transfer_image = parse_bool(payload.get("transfer_image", settings.get("transfer_image", True)))
    do_transfer_gateway = parse_bool(payload.get("transfer_gateway", settings.get("transfer_gateway", True)))
    install_gateway_service = parse_bool(payload.get("install_gateway_service", settings.get("install_gateway_service", True)))
    enable_gateway_service = parse_bool(payload.get("enable_gateway_service", False))
    force_image_transfer = parse_bool(payload.get("force_image_transfer", settings.get("force_image_transfer", False)))
    do_preflight = parse_bool(payload.get("preflight_check", settings.get("preflight_check", True)))
    do_transfer_postgres = parse_bool(payload.get("transfer_postgres", settings.get("transfer_postgres", True)))

    valid, error = tb_edge.validate_edge_key(options["edge_key"])
    if not valid:
        return jsonify({"success": False, "error": error}), 400
    valid, error = tb_edge.validate_edge_secret(options["edge_secret"])
    if not valid:
        return jsonify({"success": False, "error": error}), 400
    valid, error = tb_edge.validate_host(options["cloud_host"])
    if not valid:
        return jsonify({"success": False, "error": error}), 400

    # Garde-fou : une meme identite Edge ne peut pas servir sur deux cartes.
    unique, error = tb_edge.check_key_not_reused(
        options["edge_key"], ip, get_key_assignments(exclude_ip=ip)
    )
    if not unique:
        return jsonify({"success": False, "error": error}), 409

    if str(master_ip) == str(ip):
        do_transfer_image = False
        do_transfer_gateway = False

    # Memorise les reglages reutilisables (jamais la cle/secret specifiques)
    save_edge_settings(
        {
            "cloud_host": options["cloud_host"],
            "cloud_port": options["cloud_port"],
            "cloud_ssl": options["cloud_ssl"],
            "edge_image": options["edge_image"],
            "edge_dir": options["edge_dir"],
            "compose_project": options["compose_project"],
            "edge_http_port": options["edge_http_port"],
            "edge_mqtt_port": options["edge_mqtt_port"],
            "docker_data_root": options["docker_data_root"],
            "hostname_prefix": options["hostname_prefix"],
            "install_docker": options["install_docker"],
            "set_hostname": options["set_hostname"],
            "master_ip": master_ip,
            "preflight_check": do_preflight,
            "force_image_transfer": force_image_transfer,
            "pg_image": options["pg_image"],
            "transfer_postgres": do_transfer_postgres,
            "enable_on_boot": options["enable_on_boot"],
        }
    )

    user, password = ssh_credentials()
    job_key = f"deploy:{ip}"

    def target(job):
        job.log(f"=== Provisioning de {ip} ===")
        job.log(f"Carte de reference : {master_ip}")
        job.log(f"Serveur ThingsBoard: {options['cloud_host']}:{options['cloud_port']} (ssl={options['cloud_ssl']})")
        job.log(f"Cle Edge           : {options['edge_key']}")
        job.log(f"Secret Edge        : {tb_edge.mask_secret(options['edge_secret'])}")
        job.log(f"Image edge         : {options['edge_image']}")
        job.log(f"Projet compose     : {options['compose_project']} dans {options['edge_dir']}")
        job.log(f"Ports hote         : HTTP {options['edge_http_port']} | MQTT {options['edge_mqtt_port']}")
        job.log(f"Base locale        : {options['pg_image']}")
        job.log(f"Reset volumes      : {options['reset_data']} | Hostname unique: {options['set_hostname']}")
        job.log(f"Prerequis verifies : {do_preflight} | Retransfert force: {force_image_transfer}")
        job.log(f"Relance au boot    : {options['enable_on_boot']}")

        # ---- Etape 1 : verification des prerequis (avant toute operation longue)
        job.log("")
        job.log("--- Etape 1/5 : verification des prerequis ---")
        if do_preflight:
            report = tb_preflight.preflight_all(
                ip,
                master_ip,
                user,
                password,
                image=options["edge_image"],
                pg_image=options["pg_image"],
                edge_dir=options["edge_dir"],
                need_image_transfer=do_transfer_image,
                install_docker_allowed=options["install_docker"],
            )
            for scope, part in report["reports"].items():
                if not part:
                    continue
                job.log(f"[{scope}] {part['summary']}")
                for item in part["checks"]:
                    marker = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}.get(item["level"], "    ")
                    job.log(f"  {marker} {item['label']}: {item['value'] or '-'}")
                    if item["hint"] and item["level"] != "ok":
                        job.log(f"       -> {item['hint']}")

            job.meta["preflight"] = report
            if not report["ok"]:
                details = "; ".join(
                    f"{item['label']} ({item['scope']})" for item in report["blocking"]
                )
                return False, (
                    f"Prerequis non satisfaits, deploiement annule avant tout transfert : {details}"
                )
        else:
            job.log("Verification desactivee (option decochee).")

        # ---- Etape 2 : Docker doit exister AVANT le transfert de l'image.
        # Sans cela, on pousse 1 Go pour finir sur « docker: command not found ».
        job.log("")
        job.log("--- Etape 2/5 : dependances Docker sur la cible ---")
        docker_ready = tb_preflight.preflight_target(
            ip,
            user,
            password,
            image=options["edge_image"],
            install_docker_allowed=options["install_docker"],
            need_image_transfer=False,
        )
        if docker_ready.get("docker_installed") and docker_ready.get("docker_running"):
            job.log(f"Docker deja operationnel : {docker_ready.get('docker_version')}")
        elif options["install_docker"]:
            job.log("Docker absent ou inactif -> installation de Docker CE maintenant.")
            ok, message = tb_preflight.install_docker(
                ip, user, password,
                on_line=job.log,
                docker_data_root=options["docker_data_root"] or None,
            )
            job.log(message)
            if not ok:
                return False, f"Installation de Docker interrompue: {message}"
        else:
            return False, (
                "Docker est absent de la carte et l'option « Installer Docker CE si absent » "
                "est decochee : le transfert de l'image echouerait (docker load, code 127)."
            )

        # ---- Etape 3 : image Docker custom (absente des registres publics)
        if do_transfer_image:
            job.log("")
            job.log("--- Etape 3/5 : image Docker custom ---")
            ok, message = tb_master.transfer_edge_image(
                master_ip, ip, user, password,
                image=options["edge_image"], on_line=job.log,
                force=force_image_transfer,
            )
            job.log(message)
            if not ok:
                return False, f"Transfert de l'image interrompu: {message}"
        else:
            job.log("")
            job.log("--- Etape 3/5 : transfert d'image ignore ---")

        # ---- Etape 4 : base locale postgres (dependance DURE de l'edge)
        # Sans elle, tb-edge demarre puis meurt sur
        #   Connection to localhost:5432 refused
        job.log("")
        job.log("--- Etape 4/5 : base locale postgres ---")
        if do_transfer_postgres:
            ok, message = tb_master.ensure_postgres_image(
                master_ip, ip, user, password,
                image=options["pg_image"], on_line=job.log,
                allow_pull=options["pull_images"],
            )
            job.log(message)
            if not ok:
                return False, f"Base locale indisponible: {message}"
        else:
            job.log("Etape ignoree (option decochee).")
            job.log(
                "ATTENTION: si postgres est absent de la carte, tb-edge echouera sur "
                "« Connection to localhost:5432 refused »."
            )

        # ---- Etape 5 : compose + demarrage (attente de postgres puis tb-edge)
        job.log("")
        job.log("--- Etape 5/5 : provisioning et demarrage de la stack ---")
        ok, message = tb_edge.deploy_edge(ip, user, password, options, job.log)

        if ok:
            record_key_assignment(ip, options["edge_key"], options.get("edge_name"))

        # ---- Complement : gateway I/O Python
        if ok and do_transfer_gateway:
            job.log("")
            job.log("--- Complement : gateway I/O Python ---")
            gateway_ok, gateway_message = tb_master.transfer_gateway(
                master_ip, ip, user, password,
                on_line=job.log,
                install_service=install_gateway_service,
                enable_service=enable_gateway_service,
            )
            job.log(gateway_message)
            if not gateway_ok:
                job.log("AVERTISSEMENT: la stack Edge est en place, mais la gateway a echoue.")
        elif ok:
            job.log("")
            job.log("--- Complement : transfert de la gateway ignore ---")

        # ---- Verification finale
        job.log("")
        job.log("Verification de l'etat apres deploiement...")
        try:
            state = tb_edge.probe_edge(ip, user, password, edge_dir=options["edge_dir"])
            store_edge_state(ip, state)
            job.log(f"Etat courant       : {tb_edge.status_label(state.get('status'))[0]}")
            if state.get("cloud_hint"):
                job.log(f"Indice logs        : {state['cloud_hint']}")
            if state.get("gateway_gpio_errors"):
                job.log(
                    f"ATTENTION: {state['gateway_gpio_errors']} erreur(s) GPIO dans les logs "
                    "de la gateway (Device or resource busy) - bug a corriger."
                )
            job.meta["state"] = state
        except Exception as exception:
            job.log(f"Verification impossible: {exception}")

        return ok, message

    started, job = job_manager.start(
        job_key,
        f"Deploiement Edge {ip}",
        target,
        meta={"ip": ip, "edge_key": options["edge_key"]},
    )

    if not started:
        return jsonify(
            {
                "success": True,
                "started": False,
                "running": True,
                "job": job.snapshot(0),
                "message": "Un deploiement est deja en cours sur cette carte.",
            }
        ), 202

    return jsonify({"success": True, "started": True, "running": True, "job": job.snapshot(0)}), 202


@app.route("/api/edge/<ip>/job")
@login_required
def api_edge_job(ip):
    """Retourne les logs incrementaux du deploiement en cours."""
    offset = parse_int(request.args.get("offset"), 0)
    snapshot = job_manager.snapshot(f"deploy:{ip}", offset)
    if not snapshot:
        return jsonify({"success": True, "job": None})
    return jsonify({"success": True, "job": snapshot})


@app.route("/api/edge/<ip>/control", methods=["POST"])
@login_required
def api_edge_control(ip):
    """start | stop | restart | down de la stack Edge."""
    payload = request.get_json(silent=True) or request.form.to_dict()
    action = (payload.get("action") or "").strip()
    if action not in tb_edge.CONTROL_COMMANDS:
        return jsonify({"success": False, "error": f"Action invalide: {action}"}), 400

    settings = get_edge_settings()
    user, password = ssh_credentials()
    ok, message = tb_edge.control_edge(
        ip,
        user,
        password,
        action,
        edge_dir=settings.get("edge_dir", tb_edge.DEFAULT_EDGE_DIR),
        compose_project=settings.get("compose_project", tb_edge.DEFAULT_COMPOSE_PROJECT),
    )

    state = None
    if ok:
        try:
            state = tb_edge.probe_edge(
                ip, user, password, edge_dir=settings.get("edge_dir", tb_edge.DEFAULT_EDGE_DIR)
            )
            store_edge_state(ip, state)
        except Exception:
            state = None

    return jsonify({"success": ok, "message": message, "state": state}), (200 if ok else 500)


@app.route("/api/edge/<ip>/logs")
@login_required
def api_edge_logs(ip):
    """Recupere les logs d'un conteneur de la stack (tb-edge par defaut)."""
    lines = parse_int(request.args.get("lines"), 200)
    container = (request.args.get("container") or "tb-edge").strip()
    if container not in {"tb-edge", "tb-edge-postgres"}:
        return jsonify({"success": False, "error": f"Conteneur inconnu: {container}"}), 400

    user, password = ssh_credentials()
    ok, output = tb_edge.fetch_edge_logs(ip, user, password, lines=lines, container=container)
    return jsonify({"success": ok, "logs": output}), (200 if ok else 500)


@app.route("/api/edge/settings", methods=["GET", "POST"])
@login_required
def api_edge_settings():
    """Lit ou met a jour les reglages Edge globaux (serveur ThingsBoard, etc.)."""
    if request.method == "GET":
        return jsonify(get_edge_settings())

    payload = request.get_json(silent=True) or request.form.to_dict()
    updates = {}
    if "cloud_host" in payload:
        valid, error = tb_edge.validate_host(payload.get("cloud_host"))
        if not valid:
            return jsonify({"success": False, "error": error}), 400
        updates["cloud_host"] = error
    for key in (
        "cloud_web_url", "edge_version", "edge_image", "edge_dir",
        "compose_project", "hostname_prefix", "tb_username", "master_ip",
    ):
        if key in payload and str(payload[key]).strip():
            updates[key] = str(payload[key]).strip()
    for key in ("cloud_port", "edge_http_port", "edge_mqtt_port"):
        if key in payload:
            updates[key] = parse_int(payload[key], DEFAULT_EDGE_SETTINGS[key])
    for key in (
        "cloud_ssl", "install_docker", "set_hostname", "reset_data",
        "pull_images", "start_edge", "transfer_image", "force_image_transfer",
        "preflight_check", "transfer_gateway", "install_gateway_service",
        "enable_gateway_service",
    ):
        if key in payload:
            updates[key] = parse_bool(payload[key])

    settings = save_edge_settings(updates)
    return jsonify({"success": True, "settings": settings})


# --------------------------- Serveur ThingsBoard (tenant) --------------------


def build_tb_client(payload):
    settings = get_edge_settings()
    base_url = (payload.get("tb_url") or settings.get("cloud_web_url") or "").strip()
    if not base_url and settings.get("cloud_host"):
        base_url = f"http://{settings['cloud_host']}:8080"

    # Les identifiants tenant du parc Mobilis sont constants : s'ils ne sont
    # pas fournis, on retombe sur tenant@mobilis.dz / tenant.
    username = (payload.get("tb_username") or "").strip() or (
        settings.get("tb_username") or DEFAULT_TB_USERNAME
    )
    password = payload.get("tb_password") or DEFAULT_TB_PASSWORD
    verify_ssl = parse_bool(payload.get("tb_verify_ssl", False))

    if not base_url:
        raise ThingsBoardError("URL du serveur ThingsBoard manquante.")
    if not username or not password:
        raise ThingsBoardError("Identifiants tenant ThingsBoard requis.")

    client = ThingsBoardClient(base_url, username, password, verify_ssl=verify_ssl)
    client.login()
    updates = {}
    if base_url != settings.get("cloud_web_url"):
        updates["cloud_web_url"] = base_url
    if username != settings.get("tb_username"):
        # Seul l'identifiant est memorise, jamais le mot de passe.
        updates["tb_username"] = username
    if updates:
        save_edge_settings(updates)
    return client


@app.route("/api/tb/edges", methods=["POST"])
@login_required
def api_tb_edges():
    """Liste les Edge instances du tenant ThingsBoard."""
    payload = request.get_json(silent=True) or request.form.to_dict()
    try:
        client = build_tb_client(payload)
        edges = [client.edge_summary(edge) for edge in client.list_edges()]
        return jsonify({"success": True, "edges": edges, "count": len(edges)})
    except ThingsBoardError as exception:
        return jsonify({"success": False, "error": str(exception)}), 400


@app.route("/api/tb/edge/lookup", methods=["POST"])
@login_required
def api_tb_edge_lookup():
    """Cherche un Edge par Cle (routing key) ou par nom et renvoie son etat."""
    payload = request.get_json(silent=True) or request.form.to_dict()
    routing_key = (payload.get("edge_key") or "").strip()
    name = (payload.get("edge_name") or "").strip()

    try:
        client = build_tb_client(payload)
        edge = None
        if routing_key:
            edge = client.find_edge_by_routing_key(routing_key)
        if not edge and name:
            edge = client.find_edge_by_name(name)

        if not edge:
            return jsonify({"success": True, "found": False, "edge": None})

        return jsonify({"success": True, "found": True, "edge": client.edge_summary(edge)})
    except ThingsBoardError as exception:
        return jsonify({"success": False, "error": str(exception)}), 400


@app.route("/api/tb/edge/create", methods=["POST"])
@login_required
def api_tb_edge_create():
    """
    Cree un Edge dans le tenant ThingsBoard et retourne sa Cle + son Secret,
    directement utilisables pour provisionner la carte.
    """
    payload = request.get_json(silent=True) or request.form.to_dict()
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "Le nom de l'Edge est obligatoire."}), 400

    edge_type = (payload.get("type") or "default").strip() or "default"
    label = (payload.get("label") or "").strip() or None
    routing_key = (payload.get("routing_key") or "").strip() or None
    secret = (payload.get("secret") or "").strip() or None

    if routing_key:
        valid, error = tb_edge.validate_edge_key(routing_key)
        if not valid:
            return jsonify({"success": False, "error": error}), 400
        routing_key = error
    if secret:
        valid, error = tb_edge.validate_edge_secret(secret)
        if not valid:
            return jsonify({"success": False, "error": error}), 400
        secret = error

    try:
        client = build_tb_client(payload)

        existing = client.find_edge_by_name(name)
        if existing:
            summary = client.edge_summary(existing)
            target_ip = (payload.get("target_ip") or "").strip()
            unique, warning = tb_edge.check_key_not_reused(
                summary.get("routing_key"), target_ip, get_key_assignments(exclude_ip=target_ip)
            )
            return jsonify(
                {
                    "success": True,
                    "created": False,
                    "edge": summary,
                    "warning": None if unique else warning,
                    "message": f"Un Edge nomme '{name}' existe deja, ses identifiants sont reutilises.",
                }
            )

        edge = client.create_edge(name, edge_type, label, routing_key, secret)
        return jsonify(
            {
                "success": True,
                "created": True,
                "edge": client.edge_summary(edge),
                "message": f"Edge '{name}' cree dans le tenant.",
            }
        )
    except ThingsBoardError as exception:
        return jsonify({"success": False, "error": str(exception)}), 400


@app.route("/api/edge/states")
@login_required
def api_edge_states():
    """Retourne les derniers etats Edge connus, par IP."""
    return jsonify(load_config().get("edge_states", {}))


# ------------------------- Carte de reference et gateway I/O -----------------


@app.route("/api/master/inspect", methods=["GET", "POST"])
@login_required
def api_master_inspect():
    """Inventorie la carte de reference (image edge, compose, gateway)."""
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    settings = get_edge_settings()
    master_ip = (
        payload.get("master_ip")
        or request.args.get("master_ip")
        or settings.get("master_ip")
        or tb_master.DEFAULT_MASTER_IP
    ).strip()

    valid, error = tb_edge.validate_host(master_ip)
    if not valid:
        return jsonify({"success": False, "error": error}), 400

    user, password = ssh_credentials()
    info = tb_master.inspect_master(
        master_ip, user, password, edge_dir=settings.get("edge_dir", tb_edge.DEFAULT_EDGE_DIR)
    )

    if master_ip != settings.get("master_ip"):
        save_edge_settings({"master_ip": master_ip})

    return jsonify({"success": bool(info.get("reachable")), "master": info})


@app.route("/api/edge/<ip>/gateway", methods=["GET"])
@login_required
def api_gateway_state(ip):
    """Etat de la gateway I/O Python sur une carte."""
    user, password = ssh_credentials()
    return jsonify({"success": True, "gateway": tb_master.probe_gateway(ip, user, password)})


@app.route("/api/edge/<ip>/gateway/transfer", methods=["POST"])
@login_required
def api_gateway_transfer(ip):
    """Transfere la gateway I/O depuis la carte de reference (tache de fond)."""
    payload = request.get_json(silent=True) or request.form.to_dict()
    settings = get_edge_settings()
    master_ip = (payload.get("master_ip") or settings.get("master_ip") or tb_master.DEFAULT_MASTER_IP).strip()

    if str(master_ip) == str(ip):
        return jsonify({"success": False, "error": "La cible est la carte de reference elle-meme."}), 400

    install_service = parse_bool(payload.get("install_service", True))
    enable_service = parse_bool(payload.get("enable_service", False))
    user, password = ssh_credentials()

    def target(job):
        job.log(f"Transfert de la gateway I/O de {master_ip} vers {ip}")
        if enable_service:
            job.log(
                "ATTENTION: demarrage du service demande alors que le bug GPIO "
                "(Device or resource busy) n'est pas corrige."
            )
        return tb_master.transfer_gateway(
            master_ip, ip, user, password,
            on_line=job.log,
            install_service=install_service,
            enable_service=enable_service,
        )

    started, job = job_manager.start(
        f"deploy:{ip}", f"Gateway I/O {ip}", target, meta={"ip": ip, "kind": "gateway"}
    )
    if not started:
        return jsonify(
            {"success": True, "started": False, "running": True, "job": job.snapshot(0),
             "message": "Une operation est deja en cours sur cette carte."}
        ), 202

    return jsonify({"success": True, "started": True, "running": True, "job": job.snapshot(0)}), 202


@app.route("/api/edge/<ip>/gateway/control", methods=["POST"])
@login_required
def api_gateway_control(ip):
    """start | stop | restart | enable | disable du service tb-edge-io."""
    payload = request.get_json(silent=True) or request.form.to_dict()
    action = (payload.get("action") or "").strip()
    if action not in {"start", "stop", "restart", "enable", "disable", "status"}:
        return jsonify({"success": False, "error": f"Action invalide: {action}"}), 400

    user, password = ssh_credentials()
    sudo = "" if user == "root" else "sudo -n "
    try:
        with tb_edge.SSHSession(ip, user, password) as ssh:
            if action == "status":
                output = ssh.out(f"systemctl status {tb_edge.GATEWAY_SERVICE} --no-pager | head -20")
                return jsonify({"success": True, "message": output or "Service introuvable."})

            rc, out, err = ssh.run(f"{sudo}systemctl {action} {tb_edge.GATEWAY_SERVICE} 2>&1", timeout=90)
            state = ssh.out(f"systemctl is-active {tb_edge.GATEWAY_SERVICE} 2>/dev/null || echo absent")
            message = (out or err or f"Action '{action}' executee.").strip()
            return jsonify(
                {"success": rc == 0, "message": f"{message} (etat: {state})", "service_state": state}
            ), (200 if rc == 0 else 500)
    except Exception as exception:
        return jsonify({"success": False, "error": f"Erreur SSH: {exception}"}), 500


@app.route("/api/edge/<ip>/gateway/logs")
@login_required
def api_gateway_logs(ip):
    """Logs de la gateway I/O, avec mise en evidence des erreurs GPIO."""
    lines = parse_int(request.args.get("lines"), 200)
    user, password = ssh_credentials()
    try:
        with tb_edge.SSHSession(ip, user, password) as ssh:
            logs = ssh.out(
                f"tail -n {lines} {tb_edge.GATEWAY_LOG} 2>/dev/null "
                f"|| journalctl -u {tb_edge.GATEWAY_SERVICE} -n {lines} --no-pager 2>/dev/null",
                timeout=60,
            )
            return jsonify({"success": True, "logs": logs or "Aucun log disponible."})
    except Exception as exception:
        return jsonify({"success": False, "error": f"Erreur SSH: {exception}"}), 500


@app.route("/api/preflight/server")
@login_required
def api_preflight_server():
    """Prerequis du serveur ModBerry Manager lui-meme (relais de l'archive)."""
    return jsonify({"success": True, "preflight": tb_preflight.preflight_local()})


@app.route("/api/edge/assignments")
@login_required
def api_edge_assignments():
    """Registre des identites Edge affectees, pour eviter tout doublon."""
    config = load_config()
    return jsonify(
        {
            "assignments": config.get("edge_assignments", {}),
            "keys_in_use": get_key_assignments(),
            "master_keys": sorted(tb_edge.MASTER_ROUTING_KEYS),
        }
    )


if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "templates"), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
    start_scan_scheduler_once()
    app.run(host="0.0.0.0", port=int(os.environ.get("MODBERRY_PORT", "2310")), debug=False)
