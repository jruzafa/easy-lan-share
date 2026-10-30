#!/usr/bin/env python3
"""LanShare: gestor web de carpetas compartidas (NFS, Samba, SSHFS) entre equipos Ubuntu.

Corre en un solo equipo y controla el resto por SSH (clave pública + sudo).
Solo usa la librería estándar de Python.
"""
import argparse
import base64
import concurrent.futures as cf
import getpass
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lanshare"
STATE_FILE = CONFIG_DIR / "state.json"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=accept-new"]

LOCK = threading.RLock()
SUDO_PW = {}  # host_id -> contraseña sudo, solo en memoria


# ---------------------------------------------------------------- estado

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"hosts": [], "shares": []}


def save_state(state):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_FILE)


def local_ip():
    try:
        out = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True).stdout
        return re.search(r"\bsrc (\S+)", out).group(1)
    except Exception:
        return "127.0.0.1"


def ensure_local_host(state):
    if not any(h.get("local") for h in state["hosts"]):
        state["hosts"].insert(0, {
            "id": "local", "name": socket.gethostname(), "address": local_ip(),
            "user": getpass.getuser(), "port": 22, "local": True,
        })
    else:
        for h in state["hosts"]:
            if h.get("local"):
                h["address"] = local_ip()


def get_host(state, host_id):
    for h in state["hosts"]:
        if h["id"] == host_id:
            return h
    raise ApiError(f"Equipo desconocido: {host_id}")


def get_share(state, share_id):
    for s in state["shares"]:
        if s["id"] == share_id:
            return s
    raise ApiError(f"Compartición desconocida: {share_id}", 404)


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- ejecución

def ssh_base(host):
    return ["ssh", *SSH_OPTS, "-p", str(host.get("port", 22)), f"{host['user']}@{host['address']}"]


def run_root(host, script, timeout=900):
    """Ejecuta un script bash como root (sudo) en el equipo. Devuelve (ok, salida)."""
    pw = SUDO_PW.get(host["id"])
    sudo = "sudo -S -p ''" if pw else "sudo -n"
    stdin = (pw + "\n") if pw else ""
    if host.get("local"):
        cmd = [*shlex.split(sudo), "bash", "-c", script]
    else:
        b64 = base64.b64encode(script.encode()).decode()
        # Comillas simples: funciona igual con bash, zsh o fish como shell remota.
        remote = f"{sudo} bash -c 'S=$(echo {b64} | base64 -d); exec bash -c \"$S\" lanshare'"
        cmd = [*ssh_base(host), remote]
    return _run(cmd, stdin, timeout)


def run_user(host, command, timeout=30):
    """Ejecuta un comando como el usuario normal (sin sudo)."""
    if host.get("local"):
        cmd = ["bash", "-c", command]
    else:
        b64 = base64.b64encode(command.encode()).decode()
        cmd = [*ssh_base(host), f"bash -c 'S=$(echo {b64} | base64 -d); exec bash -c \"$S\"'"]
    return _run(cmd, "", timeout)


def _run(cmd, stdin, timeout):
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "Tiempo de espera agotado"
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0 and "a password is required" in out:
        out += "\n→ Este equipo necesita la contraseña de sudo (botón «Sudo» en la pestaña Equipos)."
    if p.returncode == 255 and not stdin and "Permission denied" in out:
        out += "\n→ La clave SSH no está autorizada. Ejecuta en una terminal: ssh-copy-id usuario@equipo"
    return p.returncode == 0, out


# ---------------------------------------------------------------- validación

BAD_PATH_CHARS = set("\"'\\$`#\n\t;&|<>*?")


def check_path(p, what):
    p = (p or "").strip()
    if not p.startswith("/") or any(c in BAD_PATH_CHARS for c in p) or ".." in p.split("/"):
        raise ApiError(f"{what} no válida: debe ser absoluta y sin caracteres especiales")
    return p.rstrip("/") or "/"


def check_word(v, what, pattern=r"^[A-Za-z0-9._-]{1,32}$"):
    v = (v or "").strip()
    if not re.match(pattern, v):
        raise ApiError(f"{what} no válido (solo letras, números, . _ -)")
    return v


def fstab_escape(p):
    return p.replace(" ", "\\040")


def b64(s):
    return base64.b64encode(s.encode()).decode()


# ---------------------------------------------------------------- scripts

PRELUDE = r"""
set -e
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 LANG=C.UTF-8 LANGUAGE=
ensure_pkg() {
  local need=""
  for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || need="$need $p"; done
  if [ -n "$need" ]; then
    echo "Instalando paquetes:$need"
    apt-get update -qq >/dev/null
    apt-get install -y -qq $need >/dev/null
  fi
}
remove_block() { [ -f "$1" ] && sed -i "/^# BEGIN lanshare:$2\$/,/^# END lanshare:$2\$/d" "$1" || true; }
"""


def server_setup_script(share, server, clients):
    sid, path, ro = share["id"], share["path"], share.get("readonly")
    s = PRELUDE + f'P="{path}"\n[ -d "$P" ] || {{ echo "La carpeta $P no existe en el servidor"; exit 1; }}\n'
    if share["type"] == "nfs":
        opts = ["ro" if ro else "rw", "sync", "no_subtree_check"]
        if share.get("map_owner", True):
            opts += ["all_squash", "anonuid=$OU", "anongid=$OG"]
        clients_spec = " ".join(f"{c['address']}({','.join(opts)})" for c in clients)
        s += f"""
ensure_pkg nfs-kernel-server
OU=$(stat -c %u "$P"); OG=$(stat -c %g "$P")
mkdir -p /etc/exports.d
echo "\\"$P\\" {clients_spec}" > /etc/exports.d/lanshare-{sid}.exports
systemctl enable --now nfs-kernel-server >/dev/null 2>&1 || true
exportfs -ra
echo "Exportado por NFS: $P"
"""
    elif share["type"] == "samba":
        user = share["smb_user"]
        allow = " ".join(c["address"] for c in clients) + " 127.0.0.1"
        s += f"""
ensure_pkg samba
OWNER=$(stat -c %U "$P")
id -u {user} >/dev/null 2>&1 || {{ echo "Creando usuario {user} (sin login)"; useradd -M -s /usr/sbin/nologin {user}; }}
PW=$(echo {b64(share["smb_password"])} | base64 -d)
printf '%s\\n%s\\n' "$PW" "$PW" | smbpasswd -s -a {user} >/dev/null
smbpasswd -e {user} >/dev/null
C=/etc/samba/lanshare.conf
touch $C
grep -qx 'include = /etc/samba/lanshare.conf' /etc/samba/smb.conf || printf '\\ninclude = /etc/samba/lanshare.conf\\n' >> /etc/samba/smb.conf
remove_block $C {sid}
cat >> $C <<EOF
# BEGIN lanshare:{sid}
[{share["smb_name"]}]
   path = $P
   browseable = yes
   read only = {"yes" if ro else "no"}
   valid users = {user}
   force user = $OWNER
   create mask = 0664
   directory mask = 0775
   hosts allow = {allow}
# END lanshare:{sid}
EOF
testparm -s >/dev/null 2>&1 || {{ echo "Configuración de Samba inválida"; testparm -s 2>&1 | tail -5; exit 1; }}
systemctl enable --now smbd >/dev/null 2>&1 || true
systemctl reload smbd || systemctl restart smbd
echo "Compartido por Samba: //{server["address"]}/{share["smb_name"]}"
"""
    elif share["type"] == "sshfs":
        keys = "\n".join(c["_pubkey"] for c in clients if c.get("_pubkey"))
        s += f"""
H=$(getent passwd {server["user"]} | cut -d: -f6)
install -d -m 700 -o {server["user"]} -g "$(id -gn {server["user"]})" "$H/.ssh"
touch "$H/.ssh/authorized_keys"
echo {b64(keys)} | base64 -d | while read -r k; do
  [ -z "$k" ] && continue
  grep -qxF "$k" "$H/.ssh/authorized_keys" || echo "$k" >> "$H/.ssh/authorized_keys"
done
chown {server["user"]}: "$H/.ssh/authorized_keys"; chmod 600 "$H/.ssh/authorized_keys"
echo "Claves SSH de los clientes autorizadas en el servidor"
"""
    return s


def server_teardown_script(share):
    sid = share["id"]
    if share["type"] == "nfs":
        return PRELUDE + f"rm -f /etc/exports.d/lanshare-{sid}.exports\ncommand -v exportfs >/dev/null && exportfs -ra\necho 'Exportación NFS eliminada'\n"
    if share["type"] == "samba":
        return PRELUDE + f"remove_block /etc/samba/lanshare.conf {sid}\nsystemctl reload smbd 2>/dev/null || true\necho 'Recurso Samba eliminado'\n"
    return "echo 'SSHFS: nada que quitar en el servidor'\n"


def sshfs_key_script(client, server):
    return PRELUDE + f"""
ensure_pkg sshfs
install -d -m 700 /etc/lanshare
[ -f /etc/lanshare/id_lanshare ] || ssh-keygen -q -t ed25519 -N '' -C "lanshare@$(hostname)" -f /etc/lanshare/id_lanshare
touch /etc/lanshare/known_hosts
ssh-keygen -F {server["address"]} -f /etc/lanshare/known_hosts >/dev/null || ssh-keyscan -T 5 -p {server.get("port", 22)} {server["address"]} >> /etc/lanshare/known_hosts 2>/dev/null
echo "PUBKEY:$(cat /etc/lanshare/id_lanshare.pub)"
"""


def client_setup_script(share, server, client):
    sid, mp, ro = share["id"], share["mountpoint"], share.get("readonly")
    t = share["type"]
    common = "_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=15"
    if ro:
        common = "ro," + common
    pre, line = "", ""
    if t == "nfs":
        pre = "ensure_pkg nfs-common\n"
        line = f"{server['address']}:{fstab_escape(share['path'])} {fstab_escape(mp)} nfs {common},timeo=50 0 0"
    elif t == "samba":
        creds = f"username={share['smb_user']}\npassword={share['smb_password']}\n"
        pre = f"""ensure_pkg cifs-utils
install -d -m 700 /etc/lanshare
echo {b64(creds)} | base64 -d > /etc/lanshare/creds-{sid}
chmod 600 /etc/lanshare/creds-{sid}
"""
        line = (f"//{server['address']}/{share['smb_name']} {fstab_escape(mp)} cifs "
                f"{common},credentials=/etc/lanshare/creds-{sid},uid=$CU,gid=$CG,iocharset=utf8,vers=3.0 0 0")
    elif t == "sshfs":
        pre = "ensure_pkg sshfs\n"
        line = (f"{server['user']}@{server['address']}:{fstab_escape(share['path'])} {fstab_escape(mp)} fuse.sshfs "
                f"{common},allow_other,reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,"
                f"port={server.get('port', 22)},IdentityFile=/etc/lanshare/id_lanshare,"
                f"UserKnownHostsFile=/etc/lanshare/known_hosts,uid=$CU,gid=$CG 0 0")
    return PRELUDE + pre + f"""
CU=$(id -u {client["user"]}); CG=$(id -g {client["user"]})
MP="{mp}"
AUTO=$(systemd-escape -p --suffix=automount "$MP")
MNT=$(systemd-escape -p --suffix=mount "$MP")
systemctl stop "$AUTO" "$MNT" 2>/dev/null || true
mkdir -p "$MP"
remove_block /etc/fstab {sid}
cat >> /etc/fstab <<EOF
# BEGIN lanshare:{sid}
{line}
# END lanshare:{sid}
EOF
systemctl daemon-reload
systemctl restart "$AUTO"
if timeout 25 ls "$MP" >/dev/null 2>&1; then
  echo "Montado en $MP"
else
  echo "ERROR: la entrada está en fstab pero no se pudo acceder a $MP"
  journalctl -u "$MNT" -n 5 --no-pager 2>/dev/null || true
  exit 1
fi
"""


def client_teardown_script(share):
    sid, mp = share["id"], share["mountpoint"]
    return PRELUDE + f"""
MP="{mp}"
systemctl stop "$(systemd-escape -p --suffix=automount "$MP")" "$(systemd-escape -p --suffix=mount "$MP")" 2>/dev/null || true
umount -l "$MP" 2>/dev/null || true
remove_block /etc/fstab {sid}
systemctl daemon-reload
rm -f /etc/lanshare/creds-{sid}
rmdir "$MP" 2>/dev/null || true
echo "Desmontado y quitado de fstab: $MP"
"""


# ---------------------------------------------------------------- operaciones

def preflight(state, share):
    """Comprueba SSH y sudo en todos los equipos implicados antes de tocar nada."""
    ids = list(dict.fromkeys([share["server"], *share["clients"]]))
    hosts = [get_host(state, i) for i in ids]
    with cf.ThreadPoolExecutor(len(hosts)) as ex:
        results = list(ex.map(lambda h: run_root(h, "true", 30), hosts))
    bad = [f"• {h['name']}: {out or 'sin respuesta'}" for h, (ok, out) in zip(hosts, results) if not ok]
    if bad:
        raise ApiError("No se ha cambiado nada. Antes hay que resolver esto:\n\n" + "\n\n".join(bad))


def sudo_status(state, ids):
    """Estado de sudo por equipo, sin cambiar nada: ok, need_pw (falta o es incorrecta) o error (SSH)."""
    hosts = [get_host(state, i) for i in dict.fromkeys(ids)]
    if not hosts:
        return {}
    with cf.ThreadPoolExecutor(len(hosts)) as ex:
        results = list(ex.map(lambda h: run_root(h, "true", 20), hosts))
    res = {}
    for h, (ok, out) in zip(hosts, results):
        if ok:
            res[h["id"]] = {"status": "ok"}
        elif re.search(r"password is required|incorrect password|Sorry, try again", out):
            res[h["id"]] = {"status": "need_pw", "wrong": h["id"] in SUDO_PW}
        else:
            res[h["id"]] = {"status": "error", "msg": out or "sin respuesta"}
    return res


def apply_share(state, share):
    """Configura servidor y clientes. Devuelve log y fija share['status']."""
    server = get_host(state, share["server"])
    clients = [dict(get_host(state, c)) for c in share["clients"]]
    log, ok = [], True

    if share["type"] == "sshfs":
        for c in clients:
            good, out = run_root(c, sshfs_key_script(c, server))
            m = re.search(r"^PUBKEY:(.+)$", out, re.M)
            log.append(f"[{c['name']}] preparar clave SSHFS\n" + re.sub(r"^PUBKEY:.*$", "", out, flags=re.M).strip())
            if good and m:
                c["_pubkey"] = m.group(1).strip()
            else:
                ok = False

    good, out = run_root(server, server_setup_script(share, server, clients))
    log.append(f"[{server['name']}] servidor\n{out}")
    if not good:
        share["status"] = "error"
        return "\n\n".join(log)

    for c in clients:
        if share["type"] == "sshfs" and not c.get("_pubkey"):
            continue
        good, out = run_root(c, client_setup_script(share, server, c))
        log.append(f"[{c['name']}] cliente\n{out}")
        ok = ok and good
    share["status"] = "ok" if ok else "error"
    return "\n\n".join(log)


def teardown_share(state, share, clients=None):
    log, ok = [], True
    for cid in clients if clients is not None else share["clients"]:
        try:
            c = get_host(state, cid)
        except ApiError:
            continue
        good, out = run_root(c, client_teardown_script(share))
        log.append(f"[{c['name']}] cliente\n{out}")
        ok = ok and good
    try:
        server = get_host(state, share["server"])
        good, out = run_root(server, server_teardown_script(share))
        log.append(f"[{server['name']}] servidor\n{out}")
        ok = ok and good
    except ApiError:
        pass
    return ok, "\n\n".join(log)


def check_share(state, share):
    res = {}
    for cid in share["clients"]:
        c = get_host(state, cid)
        mp = shlex.quote(share["mountpoint"])
        good, out = run_user(c, f"timeout 10 ls {mp} >/dev/null 2>&1 && findmnt -n -o SOURCE,FSTYPE --target {mp} | tail -1", 20)
        res[c["name"]] = out if good and out else "no montado / inaccesible"
    return res


def validate_share(state, data, old=None):
    t = data.get("type")
    if t not in ("nfs", "samba", "sshfs"):
        raise ApiError("Tipo de compartición no válido")
    server = get_host(state, data.get("server"))
    clients = data.get("clients") or []
    if not clients:
        raise ApiError("Elige al menos un equipo cliente")
    for c in clients:
        get_host(state, c)
    share = {
        "id": old["id"] if old else uuid.uuid4().hex[:8],
        "type": t,
        "name": (data.get("name") or "").strip()[:60],
        "server": server["id"],
        "path": check_path(data.get("path"), "Carpeta origen"),
        "clients": clients,
        "mountpoint": check_path(data.get("mountpoint"), "Punto de montaje"),
        "readonly": bool(data.get("readonly")),
        "created": old["created"] if old else int(time.time()),
    }
    if share["mountpoint"] in ("/", "/home", "/etc", "/usr", "/var", "/mnt", "/media"):
        raise ApiError("Ese punto de montaje no es seguro; usa algo como /mnt/compartida")
    if t == "nfs":
        share["map_owner"] = bool(data.get("map_owner", True))
    if t == "samba":
        share["smb_name"] = check_word(data.get("smb_name") or Path(share["path"]).name, "Nombre del recurso Samba")
        share["smb_user"] = check_word(data.get("smb_user") or server["user"], "Usuario Samba", r"^[a-z_][a-z0-9_-]{0,31}$")
        pw = data.get("smb_password") or (old or {}).get("smb_password")
        if not pw:
            raise ApiError("Indica la contraseña Samba")
        share["smb_password"] = pw
    if not share["name"]:
        share["name"] = Path(share["path"]).name or share["path"]
    for s in state["shares"]:
        if s["id"] != share["id"] and s["mountpoint"] == share["mountpoint"] and set(s["clients"]) & set(clients):
            raise ApiError(f"El punto de montaje ya lo usa «{s['name']}» en alguno de esos clientes")
    return share


# ---------------------------------------------------------------- descubrimiento

def discover(known_users):
    """Busca equipos con SSH en la LAN. known_users: {ip: usuario} de los equipos ya añadidos.
    Devuelve (equipos, nº de dispositivos ocultos por no tener SSH)."""
    found = {}

    def add(ip, name=None, src=None):
        try:
            if ipaddress.ip_address(ip).version != 4:
                return
        except ValueError:
            return
        h = found.setdefault(ip, {"address": ip, "name": None, "ssh": False, "sources": []})
        if name and not h["name"]:
            h["name"] = name
        if src and src not in h["sources"]:
            h["sources"].append(src)

    # 1) avahi / mDNS
    try:
        out = subprocess.run(["avahi-browse", "-atrp"], capture_output=True, text=True, timeout=8).stdout
        for line in out.splitlines():
            f = line.split(";")
            if len(f) > 8 and f[0] == "=" and f[2] == "IPv4":
                add(f[7], f[6].removesuffix(".local"), "mdns")
    except Exception:
        pass

    # 2) barrido TCP:22 de la subred principal
    net = None
    try:
        dev = re.search(r"\bdev (\S+)", subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"],
                                                       capture_output=True, text=True).stdout).group(1)
        cidr = re.search(r"inet (\S+)", subprocess.run(["ip", "-4", "-o", "addr", "show", dev],
                                                       capture_output=True, text=True).stdout).group(1)
        net = ipaddress.ip_network(cidr, strict=False)
        if net.num_addresses > 1024:
            net = ipaddress.ip_network(f"{local_ip()}/24", strict=False)
    except Exception:
        pass

    def probe(ip):
        try:
            with socket.create_connection((str(ip), 22), timeout=0.6):
                return str(ip)
        except OSError:
            return None

    macs = {}
    if net:
        with cf.ThreadPoolExecutor(128) as ex:
            for ip in ex.map(probe, net.hosts()):
                if ip:
                    add(ip, src="ssh")
                    found[ip]["ssh"] = True
        # 3) vecinos ARP de esa red
        out = subprocess.run(["ip", "-4", "neigh"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            ip = line.split()[0]
            m = re.search(r"lladdr (\S+)", line)
            # Solo vecinos con MAC real; el barrido deja muchas entradas INCOMPLETE/FAILED.
            if m and ipaddress.ip_address(ip) in net:
                macs[ip] = m.group(1).lower()
                add(ip, src="arp")

    me = local_ip()
    total = sum(1 for ip in found if ip != me)
    pending = [h for h in found.values() if not h["ssh"]]
    with cf.ThreadPoolExecutor(64) as ex:
        for h, ip in zip(pending, ex.map(probe, [h["address"] for h in pending])):
            h["ssh"] = ip is not None

    # Solo interesan los equipos con SSH: son los únicos que LanShare puede gestionar.
    res = [h for h in found.values() if h["ssh"] and h["address"] != me]
    for h in res:
        h["mac"] = macs.get(h["address"])
        h["vendor"] = mac_vendor(h["mac"])

    def enrich(h):
        if not h["name"]:
            try:
                h["name"] = socket.gethostbyaddr(h["address"])[0].removesuffix(".local")
            except OSError:
                pass
        h["banner"] = ssh_banner(h["address"])
        h["os"] = os_from_banner(h["banner"], h["vendor"])
        h["services"] = [n for n, p in (("Samba", 445), ("NFS", 2049)) if port_open(h["address"], p)]
        h["user"] = known_users.get(h["address"], getpass.getuser())
        h.update(ssh_info(h["address"], h["user"]))

    with cf.ThreadPoolExecutor(32) as ex:
        list(ex.map(enrich, res))

    # Misma MAC = mismo equipo con varias IP (p. ej. cable + wifi o IP fija + DHCP).
    by_mac, out = {}, []
    for h in sorted(res, key=lambda h: (h["address"] not in known_users, not h.get("access"),
                                        tuple(int(x) for x in h["address"].split(".")))):
        first = by_mac.get(h["mac"]) if h["mac"] else None
        if first:
            first["aliases"].append(h["address"])
            continue
        h["aliases"] = []
        if h["mac"]:
            by_mac[h["mac"]] = h
        out.append(h)
    out.sort(key=lambda h: tuple(int(x) for x in h["address"].split(".")))
    return out, total - len(res)


OUI = {
    "b8:27:eb": "Raspberry Pi", "dc:a6:32": "Raspberry Pi", "e4:5f:01": "Raspberry Pi",
    "d8:3a:dd": "Raspberry Pi", "28:cd:c1": "Raspberry Pi", "2c:cf:67": "Raspberry Pi",
    "08:00:27": "VirtualBox", "52:54:00": "QEMU/KVM", "00:0c:29": "VMware", "00:50:56": "VMware",
    "00:11:32": "Synology", "00:08:9b": "QNAP", "24:5e:be": "QNAP",
}


def mac_vendor(mac):
    if not mac:
        return None
    if mac[:8] in OUI:
        return OUI[mac[:8]]
    if int(mac[:2], 16) & 0x02:
        return "MAC aleatoria/virtual"
    return None


UBUNTU_BY_OPENSSH = {"7.6": "18.04", "8.2": "20.04", "8.9": "22.04", "9.0": "22.10", "9.3": "23.10",
                     "9.6": "24.04", "9.7": "24.10", "9.9": "25.04", "10.0": "25.10"}


def ssh_banner(ip):
    try:
        with socket.create_connection((ip, 22), timeout=1.5) as s:
            s.settimeout(2)
            return s.recv(256).decode(errors="replace").strip()
    except OSError:
        return ""


def os_from_banner(banner, vendor=None):
    """Deduce el sistema a partir del banner SSH (sin autenticarse)."""
    if m := re.search(r"OpenSSH_(\d+\.\d+)\S* Ubuntu", banner):
        rel = UBUNTU_BY_OPENSSH.get(m.group(1))
        return f"Ubuntu {rel}" if rel else "Ubuntu"
    if m := re.search(r"Debian.*deb(\d+)", banner):
        base = f"Debian {m.group(1)}"
        return f"Raspberry Pi OS ({base})" if vendor == "Raspberry Pi" else base
    if "Debian" in banner:
        return "Debian"
    if "dropbear" in banner.lower():
        return "Router / sistema embebido"
    if m := re.search(r"OpenSSH_([\d.]+)", banner):
        return f"OpenSSH {m.group(1)} (sistema desconocido)"
    return banner[:40] or None


def port_open(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=0.8):
            return True
    except OSError:
        return False


def ssh_info(ip, user):
    """Si nuestra clave ya entra, obtiene datos reales del equipo. Sin tocar known_hosts."""
    script = ('hostname; . /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-}"; '
              'sudo -n true 2>/dev/null && echo SUDO_NOPASS || echo SUDO_PASS')
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", f"{user}@{ip}",
           f"bash -c 'S=$(echo {b64(script)} | base64 -d); exec bash -c \"$S\"'"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return {"access": False}
    lines = p.stdout.strip().splitlines()
    if p.returncode != 0 or len(lines) < 3:
        return {"access": False}
    return {"access": True, "hostname": lines[0].strip(), "os_exact": lines[1].strip() or None,
            "sudo_nopass": lines[-1].strip() == "SUDO_NOPASS"}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "LanShare/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        # Protección frente a DNS rebinding: solo se acepta acceso por localhost.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def do_GET(self):
        if not self.host_ok():
            return self.send_error(403)
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = (APP_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        return self.dispatch("GET", u)

    def do_POST(self):
        self.dispatch("POST", urlparse(self.path))

    def do_PUT(self):
        self.dispatch("PUT", urlparse(self.path))

    def do_DELETE(self):
        self.dispatch("DELETE", urlparse(self.path))

    def dispatch(self, method, u):
        # La cabecera propia obliga a un preflight CORS que nunca aceptamos: bloquea CSRF.
        if not self.host_ok() or self.headers.get("X-LanShare") != "1":
            return self.send_error(403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}") if n else {}
            parts = [p for p in u.path.split("/") if p][1:]  # sin "api"
            qs = parse_qs(u.query)
            self.send_json(route(method, parts, data, qs))
        except ApiError as e:
            self.send_json({"error": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": f"Error interno: {e}"}, 500)


def public_state(state):
    return {
        "hosts": [{**h, "sudo_pw": h["id"] in SUDO_PW} for h in state["hosts"]],
        "shares": [{k: v for k, v in s.items() if k != "smb_password"} for s in state["shares"]],
        "me": {"user": getpass.getuser(), "has_key": any((Path.home() / ".ssh" / k).exists()
                                                         for k in ("id_ed25519", "id_rsa", "id_ecdsa"))},
    }


def route(method, parts, data, qs):
    with LOCK:
        state = load_state()
        ensure_local_host(state)

    match (method, parts):
        case ("GET", ["state"]):
            return public_state(state)

        case ("POST", ["discover"]):
            known = {h["address"]: h["user"] for h in state["hosts"] if not h.get("local")}
            hosts, hidden = discover(known)
            for h in hosts:
                h["known"] = any(ip in known for ip in [h["address"], *h["aliases"]])
            return {"found": hosts, "hidden": hidden}

        case ("POST", ["hosts"]):
            addr = (data.get("address") or "").strip()
            if not re.match(r"^[A-Za-z0-9.-]{1,253}$", addr):
                raise ApiError("Dirección no válida")
            try:
                port = int(data.get("port") or 22)
            except ValueError:
                port = 0
            if not 1 <= port <= 65535:
                raise ApiError("Puerto no válido")
            h = {
                "id": check_word(data.get("id") or uuid.uuid4().hex[:8], "Identificador"),
                "name": (data.get("name") or addr).strip()[:60],
                "address": addr,
                "user": check_word(data.get("user"), "Usuario", r"^[a-z_][a-z0-9_-]{0,31}$"),
                "port": port,
                "local": False,
            }
            with LOCK:
                state = load_state()
                ensure_local_host(state)
                old = next((x for x in state["hosts"] if x["id"] == h["id"]), None)
                if old and old.get("local"):
                    old["name"] = h["name"]
                elif old:
                    old.update(h)
                else:
                    if any(x["address"] == addr for x in state["hosts"]):
                        raise ApiError("Ese equipo ya está añadido")
                    state["hosts"].append(h)
                save_state(state)
            return public_state(state)

        case ("DELETE", ["hosts", hid]):
            with LOCK:
                state = load_state()
                used = [s["name"] for s in state["shares"] if s["server"] == hid or hid in s["clients"]]
                if used:
                    raise ApiError("Lo usan estas comparticiones: " + ", ".join(used))
                state["hosts"] = [h for h in state["hosts"] if h["id"] != hid or h.get("local")]
                SUDO_PW.pop(hid, None)
                save_state(state)
            return public_state(state)

        case ("POST", ["hosts", hid, "sudo"]):
            get_host(state, hid)
            if data.get("password"):
                SUDO_PW[hid] = data["password"]
            else:
                SUDO_PW.pop(hid, None)
            return public_state(state)

        case ("POST", ["sudo-check"]):
            return {"sudo": sudo_status(state, data.get("ids") or [])}

        case ("POST", ["hosts", hid, "test"]):
            h = get_host(state, hid)
            ok, out = run_user(h, "echo \"$(hostname) · $(lsb_release -ds 2>/dev/null || uname -sr)\"", 15)
            if not ok:
                return {"ok": False, "log": out or "Sin respuesta"}
            sok, sout = run_root(h, "echo sudo OK", 20)
            return {"ok": sok, "log": out + "\n" + (sout if sok else "sudo FALLA: " + sout)}

        case ("GET", ["hosts", hid, "browse"]):
            h = get_host(state, hid)
            path = check_path((qs.get("path") or ["/"])[0], "Ruta")
            p = shlex.quote(path)
            ok, out = run_user(h, f"cd {p} && find . -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f\\n' | sort", 15)
            if not ok:
                raise ApiError(out or "No se pudo leer la carpeta")
            return {"path": path, "dirs": [d for d in out.splitlines() if d]}

        case ("POST", ["shares"]):
            with LOCK:
                state = load_state()
                ensure_local_host(state)
                share = validate_share(state, data)
                preflight(state, share)
                log = apply_share(state, share)
                share["log"] = log
                state["shares"].append(share)
                save_state(state)
            return {"share_status": share["status"], "log": log, **public_state(state)}

        case ("PUT", ["shares", sid]):
            with LOCK:
                state = load_state()
                ensure_local_host(state)
                old = get_share(state, sid)
                new = validate_share(state, data, old)
                preflight(state, new)
                _, log1 = teardown_share(state, old)
                log2 = apply_share(state, new)
                new["log"] = log1 + "\n\n— aplicando cambios —\n\n" + log2
                state["shares"] = [new if s["id"] == sid else s for s in state["shares"]]
                save_state(state)
            return {"share_status": new["status"], "log": new["log"], **public_state(state)}

        case ("POST", ["shares", sid, "apply"]):
            with LOCK:
                state = load_state()
                share = get_share(state, sid)
                preflight(state, share)
                share["log"] = apply_share(state, share)
                save_state(state)
            return {"share_status": share["status"], "log": share["log"], **public_state(state)}

        case ("POST", ["shares", sid, "check"]):
            return {"result": check_share(state, get_share(state, sid))}

        case ("DELETE", ["shares", sid]):
            force = (qs.get("force") or ["0"])[0] == "1"
            with LOCK:
                state = load_state()
                share = get_share(state, sid)
                ok, log = teardown_share(state, share)
                if not ok and not force:
                    return {"deleted": False, "log": log, **public_state(state)}
                state["shares"] = [s for s in state["shares"] if s["id"] != sid]
                save_state(state)
            return {"deleted": True, "log": log, **public_state(state)}

    raise ApiError("Ruta no encontrada", 404)


def main():
    ap = argparse.ArgumentParser(description="LanShare: gestor de carpetas compartidas en la LAN")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"LanShare escuchando en http://127.0.0.1:{args.port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
