#!/usr/bin/env python3
"""
tb_master.py - Replication de la carte de reference vers une carte cible.

Automatise ce qui a ete fait manuellement dans la session de provisioning :

  1. inventaire de la carte de reference (techbase / 10.0.0.26) :
     image edge custom, compose, gateway Python, taille de l'archive
  2. transfert de l'image Docker custom : docker save (maitre) -> docker load
     (cible), en streaming a travers le serveur, sans stockage intermediaire
     durable. Le tag est custom, donc absent de tout registre public.
  3. transfert de la gateway I/O /opt/mobilis-gateway + unite systemd
     tb-edge-io.service

Les donnees stateful (base PostgreSQL de l'edge) ne sont JAMAIS repliquees :
c'est precisement ce qui rendait le clonage dd inutilisable (conflit
d'identite entre deux edges partageant la meme base).
"""

import os
import posixpath
import re
import shlex
import stat
import time

from tb_edge import (
    DEFAULT_EDGE_DIR,
    DEFAULT_EDGE_IMAGE,
    DEFAULT_PG_IMAGE,
    GATEWAY_DIR,
    GATEWAY_LOG,
    GATEWAY_SERVICE,
    SSHSession,
)

DEFAULT_MASTER_IP = "10.0.0.26"
IMAGE_TRANSFER_TIMEOUT = 5400  # 90 min : l'archive pese ~2.4 Go
CHUNK_SIZE = 4 * 1024 * 1024

# Fichiers de la gateway a ne pas transferer
GATEWAY_SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv"}


# ============================================================== INVENTAIRE
def inspect_master(ip, user, password, edge_dir=DEFAULT_EDGE_DIR, port=22):
    """
    Inventorie la carte de reference : image edge, compose, gateway.
    Retour dict avec reachable / error / images / compose_present / gateway_*
    """
    info = {
        "ip": str(ip),
        "reachable": False,
        "error": None,
        "hostname": None,
        "docker_version": None,
        "edge_images": [],
        "pg_images": [],
        "pg_image_present": False,
        "compose_present": False,
        "compose_path": posixpath.join(edge_dir, "docker-compose.yml"),
        "compose_project": None,
        "gateway_present": False,
        "gateway_files": [],
        "gateway_service_present": False,
        "gateway_service_state": None,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            info["reachable"] = True
            info["hostname"] = ssh.out("hostname")
            info["docker_version"] = ssh.out("docker --version 2>/dev/null") or None

            # Images tb-edge disponibles, avec leur taille
            images = ssh.out(
                "docker images --format '{{.Repository}}:{{.Tag}}|{{.Size}}' 2>/dev/null "
                "| grep -i 'tb-edge' || true"
            )
            for line in (images or "").splitlines():
                if "|" in line:
                    name, _, size = line.partition("|")
                    info["edge_images"].append({"image": name.strip(), "size": size.strip()})

            # Image postgres : obligatoire pour la stack, souvent absente des
            # cartes neuves sans acces au registre public.
            pg_images = ssh.out(
                "docker images --format '{{.Repository}}:{{.Tag}}|{{.Size}}' 2>/dev/null "
                "| grep -i '^postgres:' || true"
            )
            for line in (pg_images or "").splitlines():
                if "|" in line:
                    name, _, size = line.partition("|")
                    info["pg_images"].append({"image": name.strip(), "size": size.strip()})
            info["pg_image_present"] = any(
                item["image"] == DEFAULT_PG_IMAGE for item in info["pg_images"]
            )

            compose = ssh.out(f"cat {shlex.quote(info['compose_path'])} 2>/dev/null")
            info["compose_present"] = bool(compose)
            if compose:
                info["compose_content"] = compose

            info["compose_project"] = ssh.out(
                f"cd {shlex.quote(edge_dir)} 2>/dev/null && "
                "grep -h COMPOSE_PROJECT_NAME .env 2>/dev/null | cut -d= -f2"
            ) or None

            listing = ssh.out(
                f"ls -1 {shlex.quote(GATEWAY_DIR)} 2>/dev/null | head -60 || true"
            )
            gateway_files = [line.strip() for line in (listing or "").splitlines() if line.strip()]
            info["gateway_files"] = gateway_files
            info["gateway_present"] = bool(gateway_files)

            unit = ssh.out(
                f"systemctl cat {shlex.quote(GATEWAY_SERVICE)} 2>/dev/null | head -40 || true"
            )
            info["gateway_service_present"] = bool(unit)
            if unit:
                info["gateway_service_unit"] = unit
            info["gateway_service_state"] = ssh.out(
                f"systemctl is-active {shlex.quote(GATEWAY_SERVICE)} 2>/dev/null || echo unknown"
            )

    except Exception as exception:
        info["error"] = f"SSH indisponible: {exception}"

    return info


# ================================================ TRANSFERT IMAGE DOCKER
def transfer_edge_image(
    master_ip,
    target_ip,
    user,
    password,
    image=DEFAULT_EDGE_IMAGE,
    on_line=print,
    workdir="/tmp",
    port=22,
    force=False,
    min_free_gb=6.0,
):
    """
    Transfere l'image Docker custom du maitre vers la cible.

    L'image porte un tag custom (4.3.1.3EDGE-mobilis) absent des registres
    publics : elle doit donc passer par docker save -> docker load.

    Strategie : docker save | gzip cote maitre vers un fichier temporaire,
    rapatriement en streaming via SFTP, envoi vers la cible, puis docker load.

    force=True : supprime l'image existante sur la cible et la retransfere
    (utile quand la carte de reference a ete mise a jour).

    Un controle de dependances est effectue AVANT le transfert : sans docker
    sur la cible, on ne perd pas 6 minutes a pousser 1 Go pour finir sur
    « docker: command not found » (code 127).
    Retour (ok, message).
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", image)[:60]
    archive_name = f"tb-image-{safe}-{int(time.time())}.tar.gz"
    remote_archive = posixpath.join(workdir, archive_name)
    local_archive = os.path.join("/tmp", archive_name)

    try:
        # ------------------------------- cible : dependances et etat de l'image
        with SSHSession(target_ip, user, password, port=port) as target:
            ok, message = _assert_docker_ready(target, target_ip, on_line, min_free_gb)
            if not ok:
                return False, message

            already = target.out(
                f"docker image inspect {shlex.quote(image)} "
                "--format '{{.Id}}' 2>/dev/null || true"
            )
            if already and force:
                on_line(f"force=1 -> suppression de l'image existante {image} sur {target_ip}...")
                removed, remove_message = _remove_image(target, image, on_line)
                on_line(remove_message)
                if not removed:
                    return False, f"Impossible de supprimer l'image existante: {remove_message}"
                already = None
            if already:
                on_line(f"L'image {image} est deja presente sur {target_ip}, transfert inutile.")
                on_line("Cochez « Forcer le retransfert » pour la remplacer.")
                return True, "Image deja presente sur la cible."

        # ------------------------------------------- maitre : export de l'image
        on_line(f"Connexion a la carte de reference {master_ip}...")
        with SSHSession(master_ip, user, password, port=port) as master:
            exists = master.out(
                f"docker image inspect {shlex.quote(image)} "
                "--format '{{.Id}}' 2>/dev/null || true"
            )
            if not exists:
                return False, f"L'image {image} est absente de la carte de reference {master_ip}."

            on_line(f"Export de {image} (docker save | gzip) - plusieurs minutes...")
            rc = master.run_streaming(
                f"docker save {shlex.quote(image)} | gzip -1 - > {shlex.quote(remote_archive)} "
                f"&& ls -lh {shlex.quote(remote_archive)}",
                on_line,
                timeout=IMAGE_TRANSFER_TIMEOUT,
            )
            if rc != 0:
                master.run(f"rm -f {shlex.quote(remote_archive)}", timeout=20)
                return False, f"docker save a echoue (code {rc})."

            size_bytes = master.out(
                f"stat -c %s {shlex.quote(remote_archive)} 2>/dev/null || echo 0"
            )
            try:
                total = int(size_bytes)
            except ValueError:
                total = 0
            on_line(f"Archive prete : {total / (1024 ** 3):.2f} Go")

            on_line("Rapatriement de l'archive vers le serveur...")
            _sftp_download(master, remote_archive, local_archive, total, on_line)
            master.run(f"rm -f {shlex.quote(remote_archive)}", timeout=30)

        # ------------------------------------------- cible : envoi et chargement
        on_line(f"Envoi de l'archive vers {target_ip}...")
        with SSHSession(target_ip, user, password, port=port) as target:
            # Deuxieme controle : le daemon peut s'etre arrete entre-temps.
            ok, message = _assert_docker_ready(target, target_ip, on_line, min_free_gb)
            if not ok:
                return False, message

            local_size = os.path.getsize(local_archive)
            _sftp_upload(target, local_archive, remote_archive, local_size, on_line)

            on_line("Chargement de l'image sur la cible (docker load)...")
            rc = target.run_streaming(
                f"gunzip -c {shlex.quote(remote_archive)} | docker load",
                on_line,
                timeout=IMAGE_TRANSFER_TIMEOUT,
            )
            target.run(f"rm -f {shlex.quote(remote_archive)}", timeout=30)

            if rc != 0:
                return False, f"docker load a echoue (code {rc})."

            check = target.out(
                f"docker image inspect {shlex.quote(image)} "
                "--format '{{.Id}}' 2>/dev/null || true"
            )
            if not check:
                return False, f"L'image {image} reste introuvable apres chargement."

        on_line(f"Image {image} disponible sur {target_ip}.")
        return True, "Image Docker transferee avec succes."

    except Exception as exception:
        return False, f"Erreur pendant le transfert de l'image: {exception}"
    finally:
        try:
            if os.path.exists(local_archive):
                os.remove(local_archive)
        except OSError:
            pass


# ==================================================== IMAGE POSTGRES
def ensure_postgres_image(
    master_ip,
    target_ip,
    user,
    password,
    image=DEFAULT_PG_IMAGE,
    on_line=print,
    port=22,
    allow_pull=True,
):
    """
    Garantit que l'image postgres est disponible sur la carte cible.

    C'est la panne suivante rencontree apres le probleme Docker : le script se
    contentait d'un `docker pull postgres:16` avec un simple AVERTISSEMENT en
    cas d'echec. Sur un parc sans acces internet, l'image n'arrivait jamais,
    postgres ne demarrait pas, et l'edge tombait sur :

        Connection to localhost:5432 refused.

    Strategie, dans l'ordre :
      1. deja presente sur la cible -> rien a faire
      2. docker pull (si autorise et si le registre repond)
      3. replication depuis la carte de reference (docker save -> load),
         exactement comme pour l'image edge custom

    Retour (ok, message).
    """
    try:
        with SSHSession(target_ip, user, password, port=port) as target:
            present = target.out(
                f"docker image inspect {shlex.quote(image)} "
                "--format '{{.Id}}' 2>/dev/null || true"
            )
            if present:
                on_line(f"Image {image} deja presente sur {target_ip}.")
                return True, f"{image} deja presente."

            if allow_pull:
                on_line(f"Recuperation de {image} depuis le registre...")
                sudo = "" if (target.out("id -u") or "").strip() == "0" else "sudo -n "
                rc = target.run_streaming(
                    f"{sudo}docker pull {shlex.quote(image)} 2>&1",
                    on_line,
                    timeout=1800,
                )
                if rc == 0:
                    check = target.out(
                        f"docker image inspect {shlex.quote(image)} "
                        "--format '{{.Id}}' 2>/dev/null || true"
                    )
                    if check:
                        return True, f"{image} recuperee depuis le registre."
                on_line(
                    f"Le pull de {image} a echoue (registre inaccessible ?) "
                    "-> replication depuis la carte de reference."
                )
            else:
                on_line(f"Pull desactive -> replication de {image} depuis la carte de reference.")
    except Exception as exception:
        return False, f"Erreur SSH sur la cible: {exception}"

    # ------------------------------- repli : replication depuis le maitre
    if str(master_ip) == str(target_ip):
        return False, (
            f"{image} est absente et le registre est inaccessible. "
            "Aucune carte de reference distincte pour la repliquer."
        )

    ok, message = transfer_edge_image(
        master_ip, target_ip, user, password,
        image=image, on_line=on_line, port=port,
        min_free_gb=2.0,   # postgres:16 arm64 ~ 130 Mo compresse
    )
    if ok:
        return True, f"{image} repliquee depuis {master_ip}."
    return False, (
        f"{image} indisponible : ni le registre ni la carte de reference n'ont pu la fournir "
        f"({message}). Sans postgres, l'edge echoue sur « Connection to localhost:5432 refused »."
    )


# ------------------------------------------------ dependances de la cible
def _free_gb(session, path="/"):
    out = session.out(f"df -Pk {shlex.quote(path)} 2>/dev/null | awk 'NR==2{{print $4}}'")
    try:
        return round(int((out or "0").strip()) / (1024 * 1024), 2)
    except (TypeError, ValueError):
        return None


def _assert_docker_ready(session, ip, on_line, min_free_gb=6.0):
    """
    Verifie que la cible peut reellement executer `docker load`.

    C'est le garde-fou qui manquait : le transfert de 1 Go se terminait par
    « bash: line 1: docker: command not found ».
    Retour (ok, message).
    """
    version = session.out("docker --version 2>/dev/null")
    if "Docker version" not in (version or ""):
        return False, (
            f"Docker est absent de {ip} : « docker load » echouerait (code 127). "
            "Activez « Installer Docker CE si absent » ou lancez « Verifier les prerequis » "
            "puis « Installer Docker » avant le transfert."
        )
    on_line(f"Docker sur la cible : {version}")

    if session.run("docker info >/dev/null 2>&1", timeout=60)[0] != 0:
        sudo = "" if (session.out("id -u") or "").strip() == "0" else "sudo -n "
        on_line("Daemon Docker injoignable -> tentative de demarrage...")
        session.run(f"{sudo}systemctl start docker", timeout=60)
        if session.run("docker info >/dev/null 2>&1", timeout=60)[0] != 0:
            return False, (
                f"Le daemon Docker de {ip} ne repond pas (docker info echoue). "
                "Verifiez : systemctl status docker."
            )
        on_line("Daemon Docker demarre.")

    for tool in ("gunzip", "gzip"):
        if not session.out(f"command -v {tool} 2>/dev/null || true"):
            return False, (
                f"{tool} est absent de {ip} : impossible de decompresser l'archive. "
                f"Installez-le (apt-get install -y gzip)."
            )

    free = _free_gb(session, "/")
    if free is not None and free < min_free_gb:
        return False, (
            f"Espace disque insuffisant sur {ip} : {free} Go libres, "
            f"{min_free_gb} Go requis (archive ~1 Go + image ~2.4 Go)."
        )
    if free is not None:
        on_line(f"Espace libre sur la cible : {free} Go")

    return True, "Dependances de la cible validees."


def _remove_image(session, image, on_line=print):
    """Supprime une image Docker sur l'hote de la session (rmi -f)."""
    sudo = "" if (session.out("id -u") or "").strip() == "0" else "sudo -n "

    # Les conteneurs qui utilisent l'image doivent d'abord etre supprimes.
    users = session.out(
        "docker ps -a --filter ancestor=" + shlex.quote(image) + " --format '{{.Names}}' 2>/dev/null"
    )
    for name in [line.strip() for line in (users or "").splitlines() if line.strip()]:
        on_line(f"  suppression du conteneur {name} qui utilise l'image...")
        session.run(f"{sudo}docker rm -f {shlex.quote(name)}", timeout=120)

    rc, out, err = session.run(
        f"{sudo}docker rmi -f {shlex.quote(image)} 2>&1", timeout=300
    )
    output = (out or err or "").strip()
    if rc != 0:
        return False, output or f"docker rmi a echoue (code {rc})."

    still = session.out(
        f"docker image inspect {shlex.quote(image)} --format '{{{{.Id}}}}' 2>/dev/null || true"
    )
    if still:
        return False, f"L'image {image} est toujours presente apres suppression."
    return True, f"Image {image} supprimee."


def list_images(ip, user, password, filter_text="tb-edge", port=22):
    """
    Liste les images Docker d'une carte (filtrees sur tb-edge par defaut).

    Sert a l'ecran de gestion d'image : voir ce qui occupe le disque et
    supprimer une image obsolete avant d'en charger une nouvelle.
    """
    info = {
        "ip": str(ip),
        "reachable": False,
        "docker_installed": False,
        "images": [],
        "disk_free_gb": None,
        "error": None,
    }
    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            info["reachable"] = True
            version = ssh.out("docker --version 2>/dev/null")
            info["docker_installed"] = "Docker version" in (version or "")
            info["docker_version"] = version or None

            if not info["docker_installed"]:
                info["error"] = "Docker absent sur cette carte."
                return info

            listing = ssh.out(
                "docker images --format "
                "'{{.Repository}}:{{.Tag}}|{{.Size}}|{{.ID}}|{{.CreatedSince}}' 2>/dev/null"
            )
            for line in (listing or "").splitlines():
                parts = line.split("|")
                if len(parts) < 2:
                    continue
                name = parts[0].strip()
                if filter_text and filter_text.lower() not in name.lower():
                    continue
                info["images"].append(
                    {
                        "image": name,
                        "size": parts[1].strip(),
                        "id": parts[2].strip() if len(parts) > 2 else "",
                        "created": parts[3].strip() if len(parts) > 3 else "",
                    }
                )

            info["disk_free_gb"] = _free_gb(ssh, "/")
    except Exception as exception:
        info["error"] = f"SSH indisponible: {exception}"

    return info


def delete_image(ip, user, password, image, on_line=print, port=22):
    """
    Supprime une image Docker sur une carte.

    Utile avant de charger une nouvelle version de l'image edge : le disque
    des CM5 ne permet pas de garder plusieurs images de 2.4 Go.
    Retour (ok, message).
    """
    if not image or not str(image).strip():
        return False, "Nom d'image manquant."
    image = str(image).strip()

    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            version = ssh.out("docker --version 2>/dev/null")
            if "Docker version" not in (version or ""):
                return False, f"Docker est absent de {ip} : aucune image a supprimer."

            present = ssh.out(
                f"docker image inspect {shlex.quote(image)} --format '{{{{.Id}}}}' 2>/dev/null || true"
            )
            if not present:
                return True, f"L'image {image} n'est pas presente sur {ip}."

            on_line(f"Suppression de {image} sur {ip}...")
            ok, message = _remove_image(ssh, image, on_line)
            if ok:
                free = _free_gb(ssh, "/")
                if free is not None:
                    message += f" Espace libre : {free} Go."
            return ok, message
    except Exception as exception:
        return False, f"Erreur SSH: {exception}"


def _progress_reporter(label, total, on_line):
    state = {"last": 0.0, "last_pct": -10}

    def callback(transferred, _total=None):
        reference = _total or total
        now = time.time()
        pct = int(transferred * 100 / reference) if reference else 0
        if pct - state["last_pct"] >= 10 or (now - state["last"] > 20 and pct > state["last_pct"]):
            state["last"] = now
            state["last_pct"] = pct
            on_line(f"  {label} {pct}% ({transferred / (1024 ** 3):.2f} Go)")

    return callback


def _sftp_download(session, remote_path, local_path, total, on_line):
    sftp = session.client.open_sftp()
    try:
        sftp.get(remote_path, local_path, callback=_progress_reporter("recu", total, on_line))
    finally:
        sftp.close()


def _sftp_upload(session, local_path, remote_path, total, on_line):
    sftp = session.client.open_sftp()
    try:
        sftp.put(local_path, remote_path, callback=_progress_reporter("envoye", total, on_line))
    finally:
        sftp.close()


# ==================================================== GATEWAY MOBILIS I/O
GATEWAY_UNIT_TEMPLATE = """[Unit]
Description=ModBerry I/O Gateway (ThingsBoard Edge)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={gateway_dir}
ExecStart=/usr/bin/python3 {gateway_dir}/edge_gateway.py
Restart=always
RestartSec=10
StandardOutput=append:{log_path}
StandardError=append:{log_path}

[Install]
WantedBy=multi-user.target
"""


def transfer_gateway(
    master_ip,
    target_ip,
    user,
    password,
    on_line=print,
    gateway_dir=GATEWAY_DIR,
    install_service=False,
    enable_service=False,
    port=22,
):
    """
    Copie /opt/mobilis-gateway du maitre vers la cible, puis installe
    optionnellement l'unite systemd tb-edge-io.service.

    Le service n'est PAS demarre par defaut : le bug GPIO
    ("Device or resource busy") doit etre corrige au prealable.
    """
    staging = os.path.join("/tmp", f"mobilis-gateway-{int(time.time())}")

    try:
        on_line(f"Recuperation de {gateway_dir} depuis {master_ip}...")
        os.makedirs(staging, exist_ok=True)

        with SSHSession(master_ip, user, password, port=port) as master:
            count = _download_tree(master, gateway_dir, staging, on_line)
            if not count:
                return False, f"{gateway_dir} est vide ou absent sur {master_ip}."
            on_line(f"{count} fichier(s) recupere(s).")

            unit = master.out(f"systemctl cat {shlex.quote(GATEWAY_SERVICE)} 2>/dev/null || true")

        on_line(f"Envoi vers {target_ip}...")
        with SSHSession(target_ip, user, password, port=port) as target:
            sudo = "" if user == "root" else "sudo -n "
            target.run(f"{sudo}mkdir -p {shlex.quote(gateway_dir)}", timeout=30)
            sent = _upload_tree(target, staging, gateway_dir, on_line)
            on_line(f"{sent} fichier(s) transfere(s) dans {gateway_dir}.")

            if install_service:
                on_line(f"Installation de l'unite {GATEWAY_SERVICE}...")
                content = GATEWAY_UNIT_TEMPLATE.format(
                    gateway_dir=gateway_dir, log_path=GATEWAY_LOG
                )
                unit_path = f"/etc/systemd/system/{GATEWAY_SERVICE}"
                target.run(
                    f"{sudo}tee {unit_path} >/dev/null <<'UNIT_EOF'\n{content}UNIT_EOF",
                    timeout=30,
                )
                target.run(f"{sudo}systemctl daemon-reload", timeout=60)

                if enable_service:
                    on_line("Activation et demarrage du service...")
                    target.run(f"{sudo}systemctl enable --now {GATEWAY_SERVICE}", timeout=90)
                    state = target.out(f"systemctl is-active {GATEWAY_SERVICE} 2>/dev/null")
                    on_line(f"Etat du service : {state or 'inconnu'}")
                    on_line(
                        "Rappel : surveiller les erreurs GPIO "
                        "(\"Device or resource busy\", \"port is not configure yet\")."
                    )
                else:
                    on_line(
                        f"Unite installee mais non demarree. Activer avec : "
                        f"systemctl enable --now {GATEWAY_SERVICE}"
                    )
                    on_line(
                        "Recommande : corriger le bug GPIO de edge_gateway.py "
                        "avant de demarrer le service."
                    )
            elif unit:
                on_line("Unite systemd du maitre detectee mais non installee (option desactivee).")

        return True, "Gateway transferee avec succes."

    except Exception as exception:
        return False, f"Erreur pendant le transfert de la gateway: {exception}"
    finally:
        _cleanup_tree(staging)


def _download_tree(session, remote_dir, local_dir, on_line, depth=0):
    """Telecharge recursivement un dossier distant (hors __pycache__)."""
    sftp = session.client.open_sftp()
    count = 0
    try:
        try:
            entries = sftp.listdir_attr(remote_dir)
        except IOError:
            return 0

        for entry in entries:
            if entry.filename in GATEWAY_SKIP_DIRS:
                continue
            remote_path = posixpath.join(remote_dir, entry.filename)
            local_path = os.path.join(local_dir, entry.filename)

            if stat.S_ISDIR(entry.st_mode):
                if depth >= 3:
                    continue
                os.makedirs(local_path, exist_ok=True)
                sftp.close()
                count += _download_tree(session, remote_path, local_path, on_line, depth + 1)
                sftp = session.client.open_sftp()
            elif stat.S_ISREG(entry.st_mode):
                sftp.get(remote_path, local_path)
                count += 1
    finally:
        try:
            sftp.close()
        except Exception:
            pass
    return count


def _upload_tree(session, local_dir, remote_dir, on_line):
    """Envoie recursivement un dossier local vers la cible."""
    sftp = session.client.open_sftp()
    count = 0
    try:
        for root, dirs, files in os.walk(local_dir):
            dirs[:] = [d for d in dirs if d not in GATEWAY_SKIP_DIRS]
            relative = os.path.relpath(root, local_dir)
            target_dir = remote_dir if relative == "." else posixpath.join(
                remote_dir, relative.replace(os.sep, "/")
            )
            try:
                sftp.stat(target_dir)
            except IOError:
                try:
                    sftp.mkdir(target_dir)
                except IOError:
                    pass

            for filename in files:
                local_path = os.path.join(root, filename)
                remote_path = posixpath.join(target_dir, filename)
                sftp.put(local_path, remote_path)
                if filename.endswith(".py"):
                    sftp.chmod(remote_path, 0o755)
                count += 1
    finally:
        sftp.close()
    return count


def _cleanup_tree(path):
    if not path or not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


# ===================================================== ETAT DE LA GATEWAY
def probe_gateway(ip, user, password, port=22):
    """Etat de la gateway I/O sur une carte, avec detection du bug GPIO."""
    state = {
        "present": False,
        "files": 0,
        "service_installed": False,
        "service_state": None,
        "service_enabled": None,
        "gpio_errors": 0,
        "last_error": None,
    }
    try:
        with SSHSession(ip, user, password, port=port) as ssh:
            listing = ssh.out(f"ls -1 {shlex.quote(GATEWAY_DIR)} 2>/dev/null | wc -l || echo 0")
            try:
                state["files"] = int((listing or "0").strip())
            except ValueError:
                state["files"] = 0
            state["present"] = state["files"] > 0

            unit = ssh.out(
                f"systemctl list-unit-files {shlex.quote(GATEWAY_SERVICE)} 2>/dev/null "
                f"| grep -c {shlex.quote(GATEWAY_SERVICE)} || echo 0"
            )
            state["service_installed"] = (unit or "0").strip() not in {"0", ""}

            if state["service_installed"]:
                state["service_state"] = ssh.out(
                    f"systemctl is-active {shlex.quote(GATEWAY_SERVICE)} 2>/dev/null || echo unknown"
                )
                state["service_enabled"] = ssh.out(
                    f"systemctl is-enabled {shlex.quote(GATEWAY_SERVICE)} 2>/dev/null || echo unknown"
                )

            errors = ssh.out(
                f"grep -Ec 'Device or resource busy|not configure yet' "
                f"{shlex.quote(GATEWAY_LOG)} 2>/dev/null || echo 0"
            )
            try:
                state["gpio_errors"] = int((errors or "0").strip())
            except ValueError:
                state["gpio_errors"] = 0

            if state["gpio_errors"]:
                state["last_error"] = ssh.out(
                    f"grep -E 'Device or resource busy|not configure yet' "
                    f"{shlex.quote(GATEWAY_LOG)} 2>/dev/null | tail -1"
                )[:300] or None
    except Exception as exception:
        state["error"] = str(exception)

    return state
