#!/usr/bin/env python3
"""
tb_preflight.py - Couche de verification des prerequis avant provisioning.

Motivation : l'echec le plus couteux du provisioning est de transferer
~1 Go d'archive Docker vers une carte... qui n'a pas encore Docker :

    [16:31:14] Chargement de l'image sur la cible (docker load)...
    [16:31:15] bash: line 1: docker: command not found
    [16:31:15] docker load a echoue (code 127).

6 minutes de transfert perdues. Ce module verifie **avant** toute operation
longue :

  - joignabilite SSH + privileges (root / sudo -n)
  - presence de docker, docker compose, daemon actif
  - outils systeme requis (gzip/gunzip, tar, curl, systemctl, awk, sed)
  - espace disque suffisant pour l'archive + l'image decompressee
  - reseau (acces au depot Docker si installation necessaire)
  - cote carte de reference : presence de l'image a repliquer

Chaque prerequis est renvoye sous forme de "check" structure :
    {key, label, ok, level, value, hint, fixable}

level : ok | warn | fail
fixable : True si ModBerry Manager sait le corriger automatiquement
          (typiquement : installer Docker CE).
"""

import shlex

import paramiko

from tb_edge import (
    DEFAULT_EDGE_IMAGE,
    PROVISION_SCRIPT,
    REMOTE_SCRIPT,
    SSHSession,
)

# Taille approximative de l'image edge decompressee (2.4 Go) + archive (1 Go)
# On demande une marge confortable pour docker load (copie temporaire).
REQUIRED_FREE_GB_TARGET = 8.0
RECOMMENDED_FREE_GB_TARGET = 12.0
REQUIRED_FREE_GB_MASTER = 2.0   # /tmp pour l'archive gzip
REQUIRED_FREE_GB_LOCAL = 2.0    # /tmp du serveur (relais de l'archive)

# Binaires indispensables sur la carte cible
REQUIRED_TOOLS = ("gzip", "gunzip", "tar", "awk", "sed", "systemctl")
# Binaires necessaires seulement pour installer Docker CE
DOCKER_INSTALL_TOOLS = ("curl", "apt-get", "dpkg")

DOCKER_REPO_URL = "https://download.docker.com/linux/debian/gpg"


def check(key, label, ok, level=None, value=None, hint=None, fixable=False):
    """Fabrique un resultat de verification homogene."""
    if level is None:
        level = "ok" if ok else "fail"
    return {
        "key": key,
        "label": label,
        "ok": bool(ok),
        "level": level,
        "value": value,
        "hint": hint,
        "fixable": bool(fixable),
    }


def _free_gb(ssh, path):
    """Espace libre en Go sur le systeme de fichiers contenant path."""
    out = ssh.out(
        f"df -Pk {shlex.quote(path)} 2>/dev/null | awk 'NR==2{{print $4}}'"
    )
    try:
        return round(int((out or "0").strip()) / (1024 * 1024), 2)
    except (TypeError, ValueError):
        return None


def _has(ssh, binary):
    return bool(ssh.out(f"command -v {shlex.quote(binary)} 2>/dev/null || true"))


# =========================================================== CARTE CIBLE
def preflight_target(
    ip,
    user,
    password,
    image=DEFAULT_EDGE_IMAGE,
    edge_dir=None,
    need_image_transfer=True,
    install_docker_allowed=True,
    port=22,
):
    """
    Verifie qu'une carte cible est prete a recevoir le provisioning.

    Retour (dict) :
      ip, reachable, checks[], ok, blocking[], fixable[], summary,
      docker_installed, compose_available, docker_running, image_present,
      needs_docker_install
    """
    report = {
        "ip": str(ip),
        "role": "target",
        "reachable": False,
        "checks": [],
        "ok": False,
        "blocking": [],
        "warnings": [],
        "fixable": [],
        "summary": "",
        "docker_installed": False,
        "compose_available": False,
        "docker_running": False,
        "image_present": False,
        "needs_docker_install": False,
        "free_gb": None,
        "error": None,
    }
    checks = report["checks"]

    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            report["reachable"] = True
            checks.append(
                check("ssh", "Connexion SSH", True, value=f"{user}@{ip}")
            )

            # ---------------------------------------------------- privileges
            uid = (ssh.out("id -u") or "").strip()
            if uid == "0":
                checks.append(check("root", "Privileges root", True, value="root"))
            else:
                sudo_ok = ssh.out("sudo -n true 2>/dev/null && echo yes || echo no")
                checks.append(
                    check(
                        "root",
                        "Privileges root",
                        sudo_ok.strip() == "yes",
                        value=f"uid={uid}, sudo -n {'ok' if sudo_ok.strip() == 'yes' else 'refuse'}",
                        hint=None
                        if sudo_ok.strip() == "yes"
                        else "L'utilisateur SSH doit etre root ou disposer de sudo sans mot de passe.",
                    )
                )

            # -------------------------------------------------------- systeme
            os_pretty = ssh.out(". /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\"")
            arch = ssh.out("uname -m 2>/dev/null")
            checks.append(
                check(
                    "os",
                    "Systeme",
                    bool(os_pretty),
                    level="ok" if os_pretty else "warn",
                    value=f"{os_pretty or '?'} / {arch or '?'}",
                )
            )
            report["arch"] = arch or None
            report["os_pretty"] = os_pretty or None

            # --------------------------------------------------------- docker
            docker_version = ssh.out("docker --version 2>/dev/null")
            docker_installed = "Docker version" in (docker_version or "")
            report["docker_installed"] = docker_installed
            report["docker_version"] = docker_version or None

            if docker_installed:
                checks.append(
                    check("docker", "Docker CE", True, value=docker_version)
                )
            else:
                report["needs_docker_install"] = True
                checks.append(
                    check(
                        "docker",
                        "Docker CE",
                        False,
                        level="fail" if not install_docker_allowed else "warn",
                        value="absent (docker: command not found)",
                        hint=(
                            "Docker sera installe automatiquement avant le transfert de l'image."
                            if install_docker_allowed
                            else "Cochez « Installer Docker CE si absent » ou installez Docker "
                            "manuellement : sans lui, docker load echoue (code 127)."
                        ),
                        fixable=True,
                    )
                )

            # -------------------------------------------------------- compose
            if docker_installed:
                compose = ssh.out(
                    "docker compose version --short 2>/dev/null "
                    "|| docker-compose version --short 2>/dev/null"
                )
                report["compose_available"] = bool(compose)
                checks.append(
                    check(
                        "compose",
                        "Plugin docker compose",
                        bool(compose),
                        level="ok" if compose else "warn",
                        value=compose or "absent",
                        hint=None
                        if compose
                        else "docker-compose-plugin sera installe pendant le provisioning.",
                        fixable=True,
                    )
                )

                daemon = ssh.out("systemctl is-active docker 2>/dev/null || echo unknown")
                report["docker_running"] = "active" in (daemon or "")
                checks.append(
                    check(
                        "docker_daemon",
                        "Daemon Docker actif",
                        report["docker_running"],
                        level="ok" if report["docker_running"] else "warn",
                        value=daemon or "?",
                        hint=None
                        if report["docker_running"]
                        else "Le daemon sera demarre (systemctl start docker).",
                        fixable=True,
                    )
                )

                # image edge deja presente ?
                image_id = ssh.out(
                    f"docker image inspect {shlex.quote(image)} "
                    "--format '{{.Id}}' 2>/dev/null || true"
                )
                report["image_present"] = bool(image_id)
                checks.append(
                    check(
                        "edge_image",
                        f"Image {image}",
                        True,
                        level="ok" if image_id else "warn",
                        value="presente" if image_id else "absente",
                        hint=None
                        if image_id
                        else "Elle sera transferee depuis la carte de reference (docker save/load).",
                    )
                )

                root_dir = ssh.out("docker info -f '{{.DockerRootDir}}' 2>/dev/null") or None
                report["docker_data_root"] = root_dir
            else:
                report["docker_data_root"] = None

            # ---------------------------------------------------- outils base
            missing = [tool for tool in REQUIRED_TOOLS if not _has(ssh, tool)]
            checks.append(
                check(
                    "tools",
                    "Outils systeme (gzip, tar, systemctl...)",
                    not missing,
                    value="tous presents" if not missing else f"manquants: {', '.join(missing)}",
                    hint=None
                    if not missing
                    else "Installez-les : apt-get install -y " + " ".join(missing),
                )
            )

            if report["needs_docker_install"] and install_docker_allowed:
                missing_install = [
                    tool for tool in DOCKER_INSTALL_TOOLS if not _has(ssh, tool)
                ]
                checks.append(
                    check(
                        "docker_install_tools",
                        "Prerequis d'installation Docker (curl, apt-get)",
                        not missing_install,
                        value="ok" if not missing_install else f"manquants: {', '.join(missing_install)}",
                        hint=None
                        if not missing_install
                        else "Impossible d'installer Docker CE sans ces outils.",
                    )
                )

                # Le depot Docker doit etre joignable
                reachable = ssh.out(
                    f"curl -fsI -m 8 {shlex.quote(DOCKER_REPO_URL)} >/dev/null 2>&1 "
                    "&& echo yes || echo no"
                )
                repo_ok = reachable.strip() == "yes"
                checks.append(
                    check(
                        "docker_repo",
                        "Acces au depot download.docker.com",
                        repo_ok,
                        level="ok" if repo_ok else "warn",
                        value="joignable" if repo_ok else "injoignable",
                        hint=None
                        if repo_ok
                        else "Sans acces internet, installez Docker CE hors ligne (paquets .deb) "
                        "ou verifiez la passerelle/DNS de la carte.",
                    )
                )

            # ------------------------------------------------- espace disque
            free_root = _free_gb(ssh, "/")
            free_tmp = _free_gb(ssh, "/tmp")
            report["free_gb"] = free_root
            report["free_gb_tmp"] = free_tmp

            if need_image_transfer:
                worst = min(x for x in (free_root, free_tmp) if x is not None) if (
                    free_root is not None or free_tmp is not None
                ) else None
                if worst is None:
                    checks.append(
                        check("disk", "Espace disque", True, level="warn",
                              value="indetermine")
                    )
                else:
                    enough = worst >= REQUIRED_FREE_GB_TARGET
                    comfortable = worst >= RECOMMENDED_FREE_GB_TARGET
                    checks.append(
                        check(
                            "disk",
                            "Espace disque libre",
                            enough,
                            level="ok" if comfortable else ("warn" if enough else "fail"),
                            value=f"/ {free_root} Go | /tmp {free_tmp} Go",
                            hint=None
                            if comfortable
                            else (
                                f"{REQUIRED_FREE_GB_TARGET} Go minimum requis "
                                f"({RECOMMENDED_FREE_GB_TARGET} Go recommandes) : archive ~1 Go "
                                "+ image decompressee ~2.4 Go + postgres."
                            ),
                        )
                    )

            # ------------------------------------------- repertoire edge
            if edge_dir:
                parent_ok = ssh.out(
                    f"test -d {shlex.quote(edge_dir)} && echo exists "
                    f"|| (mkdir -p {shlex.quote(edge_dir)} 2>/dev/null && echo created || echo fail)"
                )
                state = (parent_ok or "").strip()
                checks.append(
                    check(
                        "edge_dir",
                        f"Repertoire {edge_dir}",
                        state in {"exists", "created"},
                        value=state or "?",
                        hint=None
                        if state in {"exists", "created"}
                        else "Impossible de creer le repertoire d'installation.",
                    )
                )

            # ------------------------------------------------------- ports
            if edge_dir:
                busy = ssh.out(
                    "ss -ltnp 2>/dev/null | awk '{print $4}' | grep -Eo '[0-9]+$' "
                    "| sort -u | tr '\\n' ' '"
                )
                report["listening_ports"] = (busy or "").split()

    except paramiko.AuthenticationException:
        report["error"] = "Authentification SSH refusee (utilisateur/mot de passe)."
        checks.append(
            check("ssh", "Connexion SSH", False, value=report["error"],
                  hint="Verifiez les identifiants SSH du parc.")
        )
    except Exception as exception:
        report["error"] = f"SSH indisponible: {exception}"
        checks.append(
            check("ssh", "Connexion SSH", False, value=str(exception),
                  hint="Carte injoignable : verifiez l'IP, le reseau et le service ssh.")
        )

    return _finalize(report)


# ================================================= CARTE DE REFERENCE
def preflight_master(
    master_ip,
    user,
    password,
    image=DEFAULT_EDGE_IMAGE,
    port=22,
):
    """Verifie que la carte de reference peut exporter l'image demandee."""
    report = {
        "ip": str(master_ip),
        "role": "master",
        "reachable": False,
        "checks": [],
        "ok": False,
        "blocking": [],
        "warnings": [],
        "fixable": [],
        "summary": "",
        "image_present": False,
        "error": None,
    }
    checks = report["checks"]

    try:
        with SSHSession(master_ip, user, password, port=port) as ssh:
            report["reachable"] = True
            checks.append(check("ssh", "Connexion SSH (reference)", True, value=str(master_ip)))

            docker_version = ssh.out("docker --version 2>/dev/null")
            docker_ok = "Docker version" in (docker_version or "")
            checks.append(
                check(
                    "docker",
                    "Docker sur la carte de reference",
                    docker_ok,
                    value=docker_version or "absent",
                    hint=None if docker_ok else "La carte de reference doit avoir Docker pour exporter l'image.",
                )
            )

            if docker_ok:
                image_id = ssh.out(
                    f"docker image inspect {shlex.quote(image)} "
                    "--format '{{.Id}}' 2>/dev/null || true"
                )
                report["image_present"] = bool(image_id)
                checks.append(
                    check(
                        "edge_image",
                        f"Image {image} disponible",
                        bool(image_id),
                        value="presente" if image_id else "ABSENTE",
                        hint=None
                        if image_id
                        else "Verifiez le tag exact avec « Inventorier » : sans cette image, "
                        "aucun transfert n'est possible.",
                    )
                )

                free_tmp = _free_gb(ssh, "/tmp")
                enough = free_tmp is None or free_tmp >= REQUIRED_FREE_GB_MASTER
                checks.append(
                    check(
                        "disk",
                        "Espace /tmp pour l'archive",
                        enough,
                        level="ok" if enough else "fail",
                        value=f"{free_tmp} Go" if free_tmp is not None else "indetermine",
                        hint=None
                        if enough
                        else f"{REQUIRED_FREE_GB_MASTER} Go requis pour docker save | gzip.",
                    )
                )

                missing = [tool for tool in ("gzip", "stat") if not _has(ssh, tool)]
                checks.append(
                    check(
                        "tools",
                        "Outils d'export (gzip, stat)",
                        not missing,
                        value="ok" if not missing else f"manquants: {', '.join(missing)}",
                    )
                )

    except paramiko.AuthenticationException:
        report["error"] = "Authentification SSH refusee sur la carte de reference."
        checks.append(check("ssh", "Connexion SSH (reference)", False, value=report["error"]))
    except Exception as exception:
        report["error"] = f"Carte de reference injoignable: {exception}"
        checks.append(check("ssh", "Connexion SSH (reference)", False, value=str(exception)))

    return _finalize(report)


# ======================================================= SERVEUR LOCAL
def preflight_local(need_image_transfer=True):
    """
    Verifie le serveur qui heberge ModBerry Manager : il sert de relais pour
    l'archive Docker (SFTP master -> /tmp local -> SFTP cible).
    """
    import os
    import shutil

    report = {
        "ip": "local",
        "role": "server",
        "reachable": True,
        "checks": [],
        "ok": False,
        "blocking": [],
        "warnings": [],
        "fixable": [],
        "summary": "",
        "error": None,
    }
    checks = report["checks"]

    checks.append(
        check(
            "provision_script",
            "Script de provisioning",
            os.path.exists(PROVISION_SCRIPT),
            value=PROVISION_SCRIPT,
            hint=None if os.path.exists(PROVISION_SCRIPT)
            else "assets/edge_provision.sh est introuvable sur le serveur.",
        )
    )

    try:
        import paramiko as _paramiko  # noqa: F401
        checks.append(check("paramiko", "Module paramiko", True, value=_paramiko.__version__))
    except Exception as exception:
        checks.append(check("paramiko", "Module paramiko", False, value=str(exception)))

    try:
        import requests as _requests  # noqa: F401
        checks.append(check("requests", "Module requests", True, value=_requests.__version__))
    except Exception as exception:
        checks.append(check("requests", "Module requests", False, value=str(exception)))

    if need_image_transfer:
        usage = shutil.disk_usage("/tmp")
        free_gb = round(usage.free / (1024 ** 3), 2)
        enough = free_gb >= REQUIRED_FREE_GB_LOCAL
        checks.append(
            check(
                "disk",
                "Espace /tmp du serveur (relais de l'archive)",
                enough,
                value=f"{free_gb} Go libres",
                hint=None
                if enough
                else f"{REQUIRED_FREE_GB_LOCAL} Go requis : l'archive gzip transite par /tmp.",
            )
        )

    return _finalize(report)


# ============================================================== SYNTHESE
def _finalize(report):
    """Calcule ok / blocking / warnings / summary a partir des checks."""
    blocking = [c for c in report["checks"] if c["level"] == "fail"]
    warnings = [c for c in report["checks"] if c["level"] == "warn"]
    fixable = [c for c in report["checks"] if c["fixable"] and not c["ok"]]

    report["blocking"] = [
        {"key": c["key"], "label": c["label"], "value": c["value"], "hint": c["hint"]}
        for c in blocking
    ]
    report["warnings"] = [
        {"key": c["key"], "label": c["label"], "value": c["value"], "hint": c["hint"]}
        for c in warnings
    ]
    report["fixable"] = [c["key"] for c in fixable]
    report["ok"] = not blocking

    if not report["reachable"]:
        report["summary"] = report.get("error") or "Hote injoignable."
    elif blocking:
        report["summary"] = f"{len(blocking)} prerequis bloquant(s) : " + ", ".join(
            c["label"] for c in blocking
        )
    elif warnings:
        report["summary"] = f"Pret, avec {len(warnings)} point(s) d'attention."
    else:
        report["summary"] = "Tous les prerequis sont satisfaits."

    return report


def preflight_all(
    target_ip,
    master_ip,
    user,
    password,
    image=DEFAULT_EDGE_IMAGE,
    edge_dir=None,
    need_image_transfer=True,
    install_docker_allowed=True,
    port=22,
):
    """
    Verification complete : serveur local, carte de reference (si transfert),
    puis carte cible. Retourne un rapport agrege.
    """
    reports = {"local": preflight_local(need_image_transfer=need_image_transfer)}

    if need_image_transfer and str(master_ip) != str(target_ip):
        reports["master"] = preflight_master(master_ip, user, password, image=image, port=port)
    else:
        reports["master"] = None

    reports["target"] = preflight_target(
        target_ip,
        user,
        password,
        image=image,
        edge_dir=edge_dir,
        need_image_transfer=need_image_transfer,
        install_docker_allowed=install_docker_allowed,
        port=port,
    )

    parts = [value for value in reports.values() if value]
    ok = all(part["ok"] for part in parts)
    blocking = []
    for name, part in reports.items():
        if not part:
            continue
        for item in part["blocking"]:
            blocking.append({**item, "scope": name})

    return {
        "ok": ok,
        "reports": reports,
        "blocking": blocking,
        "needs_docker_install": bool(reports["target"].get("needs_docker_install")),
        "image_present_target": bool(reports["target"].get("image_present")),
        "summary": (
            "Prerequis valides : le deploiement peut demarrer."
            if ok
            else f"{len(blocking)} prerequis bloquant(s) - deploiement deconseille."
        ),
    }


# ============================================ INSTALLATION DOCKER SEULE
def install_docker(ip, user, password, on_line=print, docker_data_root=None, port=22):
    """
    Installe Docker CE sur la carte **avant** tout transfert d'image.

    Reutilise assets/edge_provision.sh en mode DOCKER_ONLY=1 : le script
    s'arrete apres l'installation de Docker/compose (et le deplacement
    eventuel du data-root), sans toucher a la stack.
    """
    import os

    if not os.path.exists(PROVISION_SCRIPT):
        return False, f"Script de provisioning introuvable: {PROVISION_SCRIPT}"

    env_pairs = {
        "DOCKER_ONLY": "1",
        "INSTALL_DOCKER": "1",
        # valeurs factices : non utilisees en mode DOCKER_ONLY mais le script
        # les controle au demarrage.
        "EDGE_KEY": "00000000-0000-0000-0000-000000000000",
        "EDGE_SECRET": "preflightdockeronly",
        "CLOUD_RPC_HOST": "127.0.0.1",
    }
    if docker_data_root:
        env_pairs["DOCKER_DATA_ROOT"] = docker_data_root

    env_prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in env_pairs.items())

    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            on_line(f"Installation de Docker CE sur {ip}...")
            ssh.put_file(PROVISION_SCRIPT, REMOTE_SCRIPT)
            sudo = "" if user == "root" else "sudo -n -E "
            rc = ssh.run_streaming(
                f"{sudo}env {env_prefix} bash {REMOTE_SCRIPT} 2>&1",
                on_line,
                timeout=1800,
            )
            ssh.run(f"rm -f {REMOTE_SCRIPT}", timeout=10)

            if rc != 0:
                return False, f"Installation de Docker echouee (code {rc})."

            version = ssh.out("docker --version 2>/dev/null")
            if "Docker version" not in (version or ""):
                return False, "Docker reste introuvable apres installation."
            on_line(f"Docker operationnel : {version}")
            return True, f"Docker installe : {version}"
    except Exception as exception:
        return False, f"Erreur SSH pendant l'installation de Docker: {exception}"
