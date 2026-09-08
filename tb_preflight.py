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

import re
import shlex

import paramiko

from tb_edge import (
    DEFAULT_COMPOSE_PROJECT,
    DEFAULT_EDGE_DIR,
    DEFAULT_EDGE_HTTP_PORT,
    DEFAULT_EDGE_IMAGE,
    DEFAULT_EDGE_MQTT_PORT,
    DEFAULT_PG_IMAGE,
    PROVISION_SCRIPT,
    REMOTE_SCRIPT,
    SSHSession,
)

BOOT_SERVICE = "tb-edge-stack.service"

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
    pg_image=DEFAULT_PG_IMAGE,
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
        "pg_image_present": False,
        "stray_containers": [],
        "boot_service_enabled": False,
        "docker_boot_enabled": False,
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

                # image postgres : dependance DURE de la stack. Son absence
                # provoque « Connection to localhost:5432 refused ».
                pg_id = ssh.out(
                    f"docker image inspect {shlex.quote(pg_image)} "
                    "--format '{{.Id}}' 2>/dev/null || true"
                )
                report["pg_image_present"] = bool(pg_id)
                checks.append(
                    check(
                        "pg_image",
                        f"Base locale {pg_image}",
                        True,
                        level="ok" if pg_id else "warn",
                        value="presente" if pg_id else "absente",
                        hint=None
                        if pg_id
                        else "Sans cette image, tb-edge demarre puis meurt sur "
                        "« Connection to localhost:5432 refused ». Elle sera recuperee "
                        "du registre ou repliquee depuis la carte de reference.",
                        fixable=True,
                    )
                )

                # conteneurs edge lances a la main (docker run) : ils bloquent
                # les ports et tournent sans base ni .env
                stray = ssh.out(
                    "docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null "
                    "| grep -Ei 'tb-edge|thingsboard' "
                    "| grep -vE '^(tb-edge|tb-edge-postgres)\\|' || true"
                )
                stray_names = [
                    line.split("|")[0].strip()
                    for line in (stray or "").splitlines()
                    if line.strip()
                ]
                report["stray_containers"] = stray_names
                if stray_names:
                    checks.append(
                        check(
                            "stray_containers",
                            "Conteneurs edge lances manuellement",
                            False,
                            level="warn",
                            value=", ".join(stray_names[:5]),
                            hint="Issus d'un « docker run » manuel : sans base ni .env, ils "
                            "monopolisent les ports. Ils seront supprimes au deploiement "
                            "(ou via « Nettoyer »).",
                            fixable=True,
                        )
                    )

                # relance automatique apres reboot
                boot_enabled = ssh.out(
                    f"systemctl is-enabled {BOOT_SERVICE} 2>/dev/null || echo absent"
                )
                docker_boot = ssh.out(
                    "systemctl is-enabled docker 2>/dev/null || echo absent"
                )
                report["boot_service_enabled"] = "enabled" in (boot_enabled or "")
                report["docker_boot_enabled"] = "enabled" in (docker_boot or "")
                checks.append(
                    check(
                        "boot",
                        "Relance automatique au demarrage",
                        True,
                        level="ok" if report["docker_boot_enabled"] else "warn",
                        value=f"docker={docker_boot or '?'} | {BOOT_SERVICE}={boot_enabled or '?'}",
                        hint=None
                        if report["docker_boot_enabled"]
                        else "docker n'est pas active au boot : apres un redemarrage de la "
                        "carte, l'Edge ne repartira pas tout seul.",
                        fixable=True,
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
    pg_image=DEFAULT_PG_IMAGE,
    need_pg=True,
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
        "pg_image_present": False,
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

                # La carte de reference est le seul recours si le registre
                # public est inaccessible : postgres doit y etre presente.
                if need_pg:
                    pg_id = ssh.out(
                        f"docker image inspect {shlex.quote(pg_image)} "
                        "--format '{{.Id}}' 2>/dev/null || true"
                    )
                    report["pg_image_present"] = bool(pg_id)
                    checks.append(
                        check(
                            "pg_image",
                            f"Base locale {pg_image} (source de repli)",
                            True,
                            level="ok" if pg_id else "warn",
                            value="presente" if pg_id else "absente",
                            hint=None
                            if pg_id
                            else "Si la cible n'a pas d'acces au registre public, postgres ne "
                            "pourra pas etre repliquee depuis cette carte.",
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
    pg_image=DEFAULT_PG_IMAGE,
):
    """
    Verification complete : serveur local, carte de reference (si transfert),
    puis carte cible. Retourne un rapport agrege.
    """
    reports = {"local": preflight_local(need_image_transfer=need_image_transfer)}

    reports["target"] = preflight_target(
        target_ip,
        user,
        password,
        image=image,
        edge_dir=edge_dir,
        need_image_transfer=need_image_transfer,
        install_docker_allowed=install_docker_allowed,
        port=port,
        pg_image=pg_image,
    )

    # La carte de reference est inspectee si l'image edge OU la base postgres
    # doit en etre repliquee.
    need_master = str(master_ip) != str(target_ip) and (
        need_image_transfer or not reports["target"].get("pg_image_present")
    )
    if need_master:
        reports["master"] = preflight_master(
            master_ip, user, password, image=image, port=port,
            pg_image=pg_image,
            need_pg=not reports["target"].get("pg_image_present"),
        )
    else:
        reports["master"] = None

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


# ==================================================== DIAGNOSTIC COMPLET
# Motifs d'erreur connus dans les logs de tb-edge, avec leur cause reelle et
# le correctif. Evite de relire 300 lignes de stacktrace Java.
LOG_SIGNATURES = [
    (
        r"5432 refused|Connection to localhost:5432",
        "La base postgres n'est pas joignable",
        "Le conteneur tb-edge-postgres est absent ou arrete, ou l'edge a ete lance "
        "manuellement (docker run) sans la stack compose. Relancez le deploiement : "
        "il verifie l'image postgres et attend que la base soit prete.",
    ),
    (
        r"UNAUTHORIZED|Failed to establish.*credentials|routing key",
        "Identite Edge refusee par le serveur",
        "La Cle/Secret ne correspondent a aucun Edge du tenant, ou sont deja utilises "
        "par une autre carte. Recreez l'Edge et redeployez.",
    ),
    (
        r"Unable to connect|Connection refused.*(7070|7071)|UNAVAILABLE",
        "Serveur ThingsBoard injoignable sur le port RPC",
        "Verifiez l'hote et le port RPC (7071 sur ce parc, pas 7070) et que la carte "
        "route bien vers le serveur.",
    ),
    (
        r"Address already in use|port is already allocated",
        "Un port hote est deja occupe",
        "Un autre conteneur (souvent un « docker run » manuel) ou un service local "
        "tient le port. Utilisez « Nettoyer les conteneurs manuels ».",
    ),
    (
        r"OutOfMemoryError|Java heap space",
        "Memoire insuffisante pour la JVM",
        "Reduisez JAVA_OPTS (-Xmx) ou liberez de la memoire sur la carte.",
    ),
    (
        r"No space left on device",
        "Disque plein",
        "Liberez de l'espace : supprimez les images edge obsoletes "
        "(section « Image edge sur cette carte »).",
    ),
]


def diagnose(
    ip,
    user,
    password,
    edge_dir=DEFAULT_EDGE_DIR,
    compose_project=DEFAULT_COMPOSE_PROJECT,
    edge_image=DEFAULT_EDGE_IMAGE,
    pg_image=DEFAULT_PG_IMAGE,
    edge_http_port=DEFAULT_EDGE_HTTP_PORT,
    edge_mqtt_port=DEFAULT_EDGE_MQTT_PORT,
    port=22,
):
    """
    Diagnostic « pourquoi l'Edge ne tourne pas ».

    Rassemble en un seul appel SSH tout ce qu'il fallait aller chercher a la
    main : images, conteneurs (dont ceux lances par `docker run`), sante de
    postgres, causes d'erreur dans les logs, ports occupes, relance au boot.

    Retour dict : reachable, findings[] (level/title/detail/fix), containers,
    images, postgres, boot, verdict.
    """
    report = {
        "ip": str(ip),
        "reachable": False,
        "findings": [],
        "containers": [],
        "images": {"edge": False, "postgres": False},
        "postgres": {"present": False, "status": None, "ready": False},
        "edge": {"status": None, "started": False},
        "boot": {"docker": None, "unit": None},
        "compose_present": False,
        "env_present": False,
        "verdict": "",
        "error": None,
    }

    def add(level, title, detail, fix=None):
        report["findings"].append(
            {"level": level, "title": title, "detail": detail, "fix": fix}
        )

    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            report["reachable"] = True

            # ------------------------------------------------------- docker
            version = ssh.out("docker --version 2>/dev/null")
            if "Docker version" not in (version or ""):
                add(
                    "fail", "Docker absent",
                    "La commande docker est introuvable sur la carte.",
                    "Section « Prerequis » -> « Installer Docker CE sur la carte ».",
                )
                report["verdict"] = "Docker n'est pas installe."
                return report
            report["docker_version"] = version

            if ssh.run("docker info >/dev/null 2>&1", timeout=45)[0] != 0:
                add(
                    "fail", "Daemon Docker arrete",
                    "docker info ne repond pas.",
                    "systemctl start docker, puis relancez le diagnostic.",
                )

            # ------------------------------------------------------- images
            report["images"]["edge"] = bool(
                ssh.out(f"docker image inspect {shlex.quote(edge_image)} "
                        "--format '{{.Id}}' 2>/dev/null || true")
            )
            report["images"]["postgres"] = bool(
                ssh.out(f"docker image inspect {shlex.quote(pg_image)} "
                        "--format '{{.Id}}' 2>/dev/null || true")
            )

            if not report["images"]["edge"]:
                add(
                    "fail", f"Image edge {edge_image} absente",
                    "Le tag custom n'existe dans aucun registre public.",
                    "Section « Image edge sur cette carte » -> « Transferer l'image manquante ».",
                )
            if not report["images"]["postgres"]:
                add(
                    "fail", f"Base locale {pg_image} absente",
                    "C'est la cause directe de « Connection to localhost:5432 refused » : "
                    "tb-edge demarre, ne trouve aucune base et s'arrete.",
                    "Bouton « Installer / repliquer postgres » : pull si le registre repond, "
                    "sinon replication depuis la carte de reference.",
                )

            # --------------------------------------------------- conteneurs
            listing = ssh.out(
                "docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' 2>/dev/null"
            )
            for line in (listing or "").splitlines():
                parts = line.split("|")
                if len(parts) < 3:
                    continue
                report["containers"].append(
                    {
                        "name": parts[0].strip(),
                        "image": parts[1].strip(),
                        "status": parts[2].strip(),
                        "ports": parts[3].strip() if len(parts) > 3 else "",
                    }
                )

            names = {item["name"] for item in report["containers"]}
            stray = [
                item for item in report["containers"]
                if item["name"] not in {"tb-edge", "tb-edge-postgres"}
                and (
                    "tb-edge" in item["image"].lower()
                    or "thingsboard" in item["image"].lower()
                    # docker run <image-id> : image affichee sous forme d'ID
                    or re.fullmatch(r"[0-9a-f]{12}", item["image"].strip())
                )
            ]
            if stray:
                add(
                    "warn", "Conteneur(s) edge lance(s) manuellement",
                    "Detecte : "
                    + ", ".join(f"{item['name']} ({item['status']})" for item in stray)
                    + ". Un conteneur cree par « docker run » n'a ni base postgres ni "
                    "variables d'environnement : il echoue systematiquement sur "
                    "« localhost:5432 refused » et occupe les ports de la stack.",
                    "Bouton « Nettoyer les conteneurs manuels », puis deployez depuis "
                    "l'interface (la stack complete est demarree par docker compose).",
                )
            report["stray_containers"] = [item["name"] for item in stray]

            # ----------------------------------------------------- postgres
            if "tb-edge-postgres" in names:
                report["postgres"]["present"] = True
                report["postgres"]["status"] = next(
                    item["status"] for item in report["containers"]
                    if item["name"] == "tb-edge-postgres"
                )
                ready = ssh.run(
                    "docker exec tb-edge-postgres pg_isready -U postgres -d tb_edge "
                    ">/dev/null 2>&1", timeout=30
                )[0] == 0
                report["postgres"]["ready"] = ready
                if not ready:
                    add(
                        "fail", "Postgres present mais pas pret",
                        f"Etat du conteneur : {report['postgres']['status']}. "
                        "pg_isready echoue.",
                        "Consultez les logs de postgres ; un volume corrompu se corrige "
                        "avec un redeploiement « Purger les volumes » coche.",
                    )
                    logs = ssh.out("docker logs --tail 30 tb-edge-postgres 2>&1")
                    if logs:
                        report["postgres"]["logs"] = logs[-2000:]
            else:
                add(
                    "fail", "Conteneur tb-edge-postgres absent",
                    "La stack n'a jamais demarre la base, ou l'edge a ete lance seul "
                    "avec « docker run » au lieu de « docker compose up ».",
                    "Relancez le deploiement depuis l'interface : il attend que la base "
                    "soit prete avant de considerer l'edge demarre.",
                )

            # --------------------------------------------------------- edge
            if "tb-edge" in names:
                report["edge"]["status"] = next(
                    item["status"] for item in report["containers"]
                    if item["name"] == "tb-edge"
                )
                logs = ssh.out("docker logs --tail 400 tb-edge 2>&1", timeout=60)
                report["edge"]["started"] = "Started ThingsBoardEdge" in (logs or "")

                seen = set()
                for pattern, title, fix in LOG_SIGNATURES:
                    if re.search(pattern, logs or "", re.I) and title not in seen:
                        seen.add(title)
                        sample = next(
                            (
                                line.strip()
                                for line in reversed((logs or "").splitlines())
                                if re.search(pattern, line, re.I)
                            ),
                            "",
                        )
                        add("fail", title, sample[:300], fix)

                if report["edge"]["started"] and not seen:
                    if re.search(r"connected to cloud|edge connected", logs or "", re.I):
                        add("ok", "Edge demarre et connecte au serveur",
                            "L'application tourne et la liaison est etablie.")
                    else:
                        add("warn", "Edge demarre, liaison non confirmee",
                            "L'application tourne mais aucun message de connexion au "
                            "serveur n'apparait encore dans les logs.",
                            "Patientez, puis verifiez l'etat 'Active' de l'Edge dans "
                            "ThingsBoard.")
            else:
                add(
                    "warn", "Conteneur tb-edge absent",
                    "Aucun conteneur tb-edge n'existe sur la carte.",
                    "Lancez le deploiement depuis la section « 3. Deploiement ».",
                )

            # -------------------------------------------- fichiers de config
            report["compose_present"] = bool(
                ssh.out(f"test -f {shlex.quote(edge_dir)}/docker-compose.yml "
                        "&& echo yes || true")
            )
            report["env_present"] = bool(
                ssh.out(f"test -f {shlex.quote(edge_dir)}/.env && echo yes || true")
            )
            if not report["compose_present"] or not report["env_present"]:
                add(
                    "warn", "Configuration de la stack incomplete",
                    f"docker-compose.yml: {'ok' if report['compose_present'] else 'absent'} | "
                    f".env: {'ok' if report['env_present'] else 'absent'} dans {edge_dir}.",
                    "Le deploiement (re)ecrit ces deux fichiers.",
                )

            # ---------------------------------------------- ports et boot
            for label, value in (("HTTP", edge_http_port), ("MQTT", edge_mqtt_port)):
                holder = ssh.out(
                    f"ss -ltnp 2>/dev/null | grep -w ':{int(value)}' | head -1"
                )
                if holder and "docker" not in holder.lower():
                    add(
                        "warn", f"Port {label} {value} occupe par un autre service",
                        holder[:200],
                        f"Changez le port hote {label} dans les options avancees, ou "
                        "liberez le port.",
                    )

            docker_boot = ssh.out("systemctl is-enabled docker 2>/dev/null || echo absent")
            unit_boot = ssh.out(
                f"systemctl is-enabled {BOOT_SERVICE} 2>/dev/null || echo absent"
            )
            report["boot"]["docker"] = docker_boot
            report["boot"]["unit"] = unit_boot
            if "enabled" not in (docker_boot or ""):
                add(
                    "warn", "Docker n'est pas active au demarrage",
                    f"systemctl is-enabled docker = {docker_boot}.",
                    "Bouton « Activer la relance au boot » : sinon l'Edge ne repart pas "
                    "apres un redemarrage de la carte.",
                )

            # ------------------------------------------------------ verdict
            fails = [f for f in report["findings"] if f["level"] == "fail"]
            warns = [f for f in report["findings"] if f["level"] == "warn"]
            if report["edge"]["started"] and not fails:
                report["verdict"] = "Edge operationnel."
            elif fails:
                report["verdict"] = f"{len(fails)} probleme(s) bloquant(s) : " + "; ".join(
                    f["title"] for f in fails[:3]
                )
            elif warns:
                report["verdict"] = f"{len(warns)} point(s) d'attention."
            else:
                report["verdict"] = "Aucun probleme detecte."

    except Exception as exception:
        report["error"] = f"SSH indisponible: {exception}"
        report["verdict"] = report["error"]

    return report


def cleanup_stray_containers(ip, user, password, edge_image=DEFAULT_EDGE_IMAGE, port=22):
    """
    Supprime les conteneurs edge crees hors de la stack compose.

    Cible les noms generes par Docker (« loving_lamport »...) issus d'un
    `docker run` manuel : sans postgres ni `.env`, ils echouent toujours et
    bloquent les ports 8082/1884.
    Retour (ok, message, removed[]).
    """
    removed = []
    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            sudo = "" if (ssh.out("id -u") or "").strip() == "0" else "sudo -n "

            listing = ssh.out(
                "docker ps -a --format '{{.Names}}|{{.Image}}' 2>/dev/null"
            )
            candidates = []
            for line in (listing or "").splitlines():
                if "|" not in line:
                    continue
                name, _, image = line.partition("|")
                name, image = name.strip(), image.strip()
                if name in {"tb-edge", "tb-edge-postgres"} or not name:
                    continue
                if (
                    "tb-edge" in image.lower()
                    or "thingsboard" in image.lower()
                    or re.fullmatch(r"[0-9a-f]{12}", image)
                ):
                    candidates.append(name)

            for name in candidates:
                rc, _, _ = ssh.run(f"{sudo}docker rm -f {shlex.quote(name)}", timeout=120)
                if rc == 0:
                    removed.append(name)

            if not candidates:
                return True, "Aucun conteneur edge lance manuellement.", []
            if not removed:
                return False, f"Suppression echouee pour : {', '.join(candidates)}", []
            return True, (
                f"{len(removed)} conteneur(s) supprime(s) : {', '.join(removed)}. "
                "Les ports de la stack sont liberes."
            ), removed
    except Exception as exception:
        return False, f"Erreur SSH: {exception}", removed


BOOT_UNIT_TEMPLATE = """[Unit]
Description=ThingsBoard Edge stack (docker compose) - ModBerry Manager
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={edge_dir}
ExecStart=/usr/bin/env {compose} -p {project} up -d
ExecStop=/usr/bin/env {compose} -p {project} stop
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
"""


def set_boot_persistence(
    ip,
    user,
    password,
    enable=True,
    edge_dir=DEFAULT_EDGE_DIR,
    compose_project=DEFAULT_COMPOSE_PROJECT,
    port=22,
):
    """
    Active (ou desactive) la relance automatique de la stack au demarrage.

    `restart: always` ne suffit pas si docker.service n'est pas active au
    boot : la carte redemarre et l'Edge ne revient jamais. On active donc
    docker **et** une unite oneshot qui rejoue `compose up -d`.
    Retour (ok, message).
    """
    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            sudo = "" if (ssh.out("id -u") or "").strip() == "0" else "sudo -n "

            if not enable:
                ssh.run(f"{sudo}systemctl disable {BOOT_SERVICE}", timeout=60)
                return True, (
                    f"{BOOT_SERVICE} desactive. La stack ne sera plus relancee au boot "
                    "(docker.service reste inchange)."
                )

            compose = "docker compose"
            if not ssh.out("docker compose version --short 2>/dev/null"):
                if ssh.out("command -v docker-compose 2>/dev/null"):
                    compose = "docker-compose"
                else:
                    return False, "Aucun docker compose disponible sur la carte."

            ssh.run(f"{sudo}systemctl enable docker", timeout=60)
            ssh.run(f"{sudo}systemctl enable containerd", timeout=60)

            unit = BOOT_UNIT_TEMPLATE.format(
                edge_dir=edge_dir, compose=compose, project=compose_project
            )
            rc, _, err = ssh.run(
                f"{sudo}tee /etc/systemd/system/{BOOT_SERVICE} >/dev/null "
                f"<<'UNIT_EOF'\n{unit}UNIT_EOF",
                timeout=45,
            )
            if rc != 0:
                return False, f"Ecriture de l'unite impossible: {err}"

            ssh.run(f"{sudo}systemctl daemon-reload", timeout=60)
            rc, _, err = ssh.run(f"{sudo}systemctl enable {BOOT_SERVICE}", timeout=60)
            if rc != 0:
                return False, f"Activation de {BOOT_SERVICE} echouee: {err}"

            docker_state = ssh.out("systemctl is-enabled docker 2>/dev/null")
            unit_state = ssh.out(f"systemctl is-enabled {BOOT_SERVICE} 2>/dev/null")
            return True, (
                f"Relance au boot activee (docker={docker_state or '?'}, "
                f"{BOOT_SERVICE}={unit_state or '?'}). "
                "L'Edge repartira automatiquement apres un redemarrage."
            )
    except Exception as exception:
        return False, f"Erreur SSH: {exception}"


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
