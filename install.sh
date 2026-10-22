#!/usr/bin/env bash
# Instalador de Easy LAN Share (LanShare).
#   ./install.sh                 instala o actualiza y arranca el servicio
#   ./install.sh --port 9000     usa otro puerto (por defecto 8765)
#   ./install.sh --uninstall     quita el servicio y la app (conserva la configuración)
#   ./install.sh --purge         como --uninstall y además borra ~/.config/lanshare
set -euo pipefail

APP_NAME="lanshare"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/easy-lan-share"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/lanshare"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/$APP_NAME.service"
PORT=8765
ACTION=install

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B= G= Y= R= N=; fi
info() { echo "${B}==>${N} $*"; }
ok()   { echo "${G}✔${N} $*"; }
warn() { echo "${Y}!${N} $*"; }
die()  { echo "${R}✘ $*${N}" >&2; exit 1; }
ask()  { [ -t 0 ] || return 1; local r; read -r -p "$1 [s/N] " r; [[ "$r" =~ ^[sSyY]$ ]]; }

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:-}"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --uninstall) ACTION=uninstall; shift ;;
    --purge) ACTION=purge; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Opción desconocida: $1" ;;
  esac
done
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || die "Puerto no válido: $PORT (1024-65535)"
[ "$(id -u)" -ne 0 ] || die "No lo ejecutes como root ni con sudo: se instala para tu usuario."

uninstall() {
  info "Parando y quitando el servicio"
  systemctl --user disable --now "$APP_NAME.service" 2>/dev/null || true
  rm -f "$UNIT_FILE"
  systemctl --user daemon-reload
  rm -rf "$INSTALL_DIR"
  ok "LanShare desinstalado"
  if [ "$ACTION" = purge ]; then
    rm -rf "$CONFIG_DIR"
    ok "Configuración borrada ($CONFIG_DIR)"
  else
    echo "  La configuración se conserva en $CONFIG_DIR (usa --purge para borrarla)."
  fi
  warn "Las comparticiones ya creadas siguen activas en los equipos. Si querías quitarlas, bórralas desde la web antes de desinstalar."
}

if [ "$ACTION" != install ]; then uninstall; exit 0; fi

# ---------------------------------------------------------------- comprobaciones
info "Comprobando requisitos"
[ -f "$SRC_DIR/lanshare.py" ] && [ -f "$SRC_DIR/index.html" ] || die "Ejecuta el instalador desde la carpeta del repositorio."
command -v systemctl >/dev/null || die "Hace falta systemd."
systemctl --user show-environment >/dev/null 2>&1 || die "No hay sesión de systemd de usuario (¿estás por SSH sin sesión gráfica? prueba: loginctl enable-linger $USER)."

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "Hace falta python3."
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Hace falta Python 3.10 o superior ($("$PY" -V))."
ok "$("$PY" -V)"

if . /etc/os-release 2>/dev/null && [ "${ID:-}" = ubuntu ]; then ok "$PRETTY_NAME"; else warn "No es Ubuntu: puede funcionar, pero no está probado."; fi

missing=()
command -v ssh >/dev/null || missing+=(openssh-client)
command -v avahi-browse >/dev/null || missing+=(avahi-utils)
if [ ${#missing[@]} -gt 0 ]; then
  warn "Faltan paquetes: ${missing[*]} (ssh es imprescindible; avahi-utils mejora la búsqueda de equipos)"
  if ask "¿Instalarlos con sudo apt?"; then
    sudo apt-get update -qq && sudo apt-get install -y -qq "${missing[@]}"
  fi
fi
command -v ssh >/dev/null || die "Hace falta el cliente ssh (sudo apt install openssh-client)."
ok "Cliente SSH"

has_key=false
for k in id_ed25519 id_rsa id_ecdsa; do [ -f "$HOME/.ssh/$k" ] && has_key=true; done
if ! $has_key; then
  warn "No tienes clave SSH; LanShare la necesita para entrar en los equipos."
  if ask "¿Crear una ahora (ed25519, sin contraseña)?"; then
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    ssh-keygen -q -t ed25519 -N '' -f "$HOME/.ssh/id_ed25519"
    ok "Clave creada en ~/.ssh/id_ed25519"
  fi
else
  ok "Clave SSH encontrada"
fi

# ---------------------------------------------------------------- instalación
if systemctl --user is-active --quiet "$APP_NAME.service"; then
  info "Actualizando instalación existente"
  systemctl --user stop "$APP_NAME.service"
fi
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  die "El puerto $PORT ya está en uso. Elige otro con --port."
fi

info "Copiando la app a $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
install -m 755 "$SRC_DIR/lanshare.py" "$INSTALL_DIR/lanshare.py"
install -m 644 "$SRC_DIR/index.html" "$INSTALL_DIR/index.html"

info "Creando el servicio $UNIT_FILE"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=LanShare - gestor web de carpetas compartidas en la LAN

[Service]
ExecStart=$PY $INSTALL_DIR/lanshare.py --port $PORT
Restart=on-failure

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now "$APP_NAME.service" >/dev/null 2>&1

URL="http://127.0.0.1:$PORT/"
for _ in $(seq 20); do
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
  sleep 0.25
done
systemctl --user is-active --quiet "$APP_NAME.service" || {
  journalctl --user -u "$APP_NAME" -n 20 --no-pager
  die "El servicio no ha arrancado."
}

echo
ok "${B}LanShare instalado y en marcha:${N} $URL"
cat <<EOF

  Siguientes pasos:
   1. Abre $URL → Equipos → Buscar en la red.
   2. Autoriza tu clave en cada equipo:  ssh-copy-id usuario@ip
   3. Si sudo pide contraseña en algún equipo, indícala con el botón «Sudo».
      Se guarda solo en memoria: se pierde al reiniciar el servicio, el PC
      o al volver a ejecutar este instalador.

  Servicio:      systemctl --user status|restart|stop $APP_NAME
  Registro:      journalctl --user -u $APP_NAME -f
  Desinstalar:   $SRC_DIR/install.sh --uninstall
EOF
