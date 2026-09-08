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

DEFAULT_EDGE_SETTINGS = {
    "cloud_host": os.environ.get("MODBERRY_TB_HOST", ""),
    "cloud_port": int(os.environ.get("MODBERRY_TB_RPC_PORT", "7070")),
    "cloud_ssl": False,
    "cloud_web_url": os.environ.get("MODBERRY_TB_URL", ""),
    "edge_version": tb_edge.DEFAULT_EDGE_VERSION,
    "edge_dir": tb_edge.DEFAULT_EDGE_DIR,
    "edge_http_port": tb_edge.DEFAULT_EDGE_HTTP_PORT,
    "edge_mqtt_port": 1883,
    "hostname_prefix": "modberry",
    "install_docker": True,
    "reset_data": True,
    "set_hostname": True,
    "pull_images": True,
    "start_edge": True,
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
    device = find_device(ip)
    if not device:
        flash("Peripherique non trouve. Relancez un scan.", "error")
        return redirect(url_for("dashboard"))

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
        "cloud_port": parse_int(payload.get("cloud_port"), settings.get("cloud_port", 7070)),
        "cloud_ssl": parse_bool(payload.get("cloud_ssl", settings.get("cloud_ssl", False))),
        "edge_version": (payload.get("edge_version") or settings.get("edge_version") or tb_edge.DEFAULT_EDGE_VERSION).strip(),
        "edge_dir": (payload.get("edge_dir") or settings.get("edge_dir") or tb_edge.DEFAULT_EDGE_DIR).strip(),
        "edge_http_port": parse_int(payload.get("edge_http_port"), settings.get("edge_http_port", 8080)),
        "edge_mqtt_port": parse_int(payload.get("edge_mqtt_port"), settings.get("edge_mqtt_port", 1883)),
        "edge_name": (payload.get("edge_name") or "").strip(),
        "pg_password": (payload.get("pg_password") or "postgres").strip(),
        "reset_data": parse_bool(payload.get("reset_data", True)),
        "set_hostname": parse_bool(payload.get("set_hostname", settings.get("set_hostname", True))),
        "hostname_prefix": (payload.get("hostname_prefix") or settings.get("hostname_prefix") or "modberry").strip(),
        "install_docker": parse_bool(payload.get("install_docker", settings.get("install_docker", True))),
        "pull_images": parse_bool(payload.get("pull_images", True)),
        "start_edge": parse_bool(payload.get("start_edge", True)),
    }

    valid, error = tb_edge.validate_edge_key(options["edge_key"])
    if not valid:
        return jsonify({"success": False, "error": error}), 400
    valid, error = tb_edge.validate_edge_secret(options["edge_secret"])
    if not valid:
        return jsonify({"success": False, "error": error}), 400
    valid, error = tb_edge.validate_host(options["cloud_host"])
    if not valid:
        return jsonify({"success": False, "error": error}), 400

    # Memorise les reglages reutilisables (jamais la cle/secret specifiques)
    save_edge_settings(
        {
            "cloud_host": options["cloud_host"],
            "cloud_port": options["cloud_port"],
            "cloud_ssl": options["cloud_ssl"],
            "edge_version": options["edge_version"],
            "edge_dir": options["edge_dir"],
            "edge_http_port": options["edge_http_port"],
            "edge_mqtt_port": options["edge_mqtt_port"],
            "hostname_prefix": options["hostname_prefix"],
            "install_docker": options["install_docker"],
            "set_hostname": options["set_hostname"],
        }
    )

    user, password = ssh_credentials()
    job_key = f"deploy:{ip}"

    def target(job):
        job.log(f"Deploiement ThingsBoard Edge sur {ip}")
        job.log(f"Serveur cible  : {options['cloud_host']}:{options['cloud_port']} (ssl={options['cloud_ssl']})")
        job.log(f"Cle Edge       : {options['edge_key']}")
        job.log(f"Secret Edge    : {tb_edge.mask_secret(options['edge_secret'])}")
        job.log(f"Version Edge   : {options['edge_version']}")
        job.log(f"Reset data     : {options['reset_data']} | Hostname unique: {options['set_hostname']}")

        ok, message = tb_edge.deploy_edge(ip, user, password, options, job.log)

        job.log("Verification de l'etat apres deploiement...")
        try:
            state = tb_edge.probe_edge(ip, user, password, edge_dir=options["edge_dir"])
            store_edge_state(ip, state)
            label = tb_edge.status_label(state.get("status"))[0]
            job.log(f"Etat courant   : {label}")
            if state.get("cloud_hint"):
                job.log(f"Indice logs    : {state['cloud_hint']}")
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
        ip, user, password, action, edge_dir=settings.get("edge_dir", tb_edge.DEFAULT_EDGE_DIR)
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
    """Recupere les logs du conteneur tb-edge."""
    lines = parse_int(request.args.get("lines"), 200)
    settings = get_edge_settings()
    user, password = ssh_credentials()
    ok, output = tb_edge.fetch_edge_logs(ip, user, password, lines=lines)
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
    for key in ("cloud_web_url", "edge_version", "edge_dir", "hostname_prefix"):
        if key in payload and str(payload[key]).strip():
            updates[key] = str(payload[key]).strip()
    for key in ("cloud_port", "edge_http_port", "edge_mqtt_port"):
        if key in payload:
            updates[key] = parse_int(payload[key], DEFAULT_EDGE_SETTINGS[key])
    for key in ("cloud_ssl", "install_docker", "set_hostname", "reset_data", "pull_images", "start_edge"):
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

    username = (payload.get("tb_username") or "").strip()
    password = payload.get("tb_password") or ""
    verify_ssl = parse_bool(payload.get("tb_verify_ssl", False))

    if not base_url:
        raise ThingsBoardError("URL du serveur ThingsBoard manquante.")
    if not username or not password:
        raise ThingsBoardError("Identifiants tenant ThingsBoard requis.")

    client = ThingsBoardClient(base_url, username, password, verify_ssl=verify_ssl)
    client.login()
    if base_url != settings.get("cloud_web_url"):
        save_edge_settings({"cloud_web_url": base_url})
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
            return jsonify(
                {
                    "success": True,
                    "created": False,
                    "edge": client.edge_summary(existing),
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


if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "templates"), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
    start_scan_scheduler_once()
    app.run(host="0.0.0.0", port=int(os.environ.get("MODBERRY_PORT", "2310")), debug=False)
