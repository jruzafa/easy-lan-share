# LanShare

Gestor web para crear y administrar carpetas compartidas entre equipos Ubuntu de la red local.

Corre en un único equipo y controla el resto por SSH: exporta la carpeta en el equipo servidor y la monta en los clientes, todo desde una interfaz web sencilla.

![Listado de comparticiones](docs/screenshots/comparticiones.png)

## Capturas

**Equipos gestionados:** el propio equipo y los remotos añadidos tras buscarlos en la red.

![Equipos](docs/screenshots/equipos.png)

**Crear o editar una compartición:** el tipo, el servidor y la carpeta, los clientes y el punto de montaje.

<img src="docs/screenshots/editar-comparticion.png" alt="Editar compartición" width="480">

## Funcionalidades

- **Descubrimiento de equipos** en la LAN (mDNS, barrido del puerto 22 y tabla ARP).
- **Tres tipos de compartición:**
  - **NFS**: nativo de Linux y rápido. Opción para escribir siempre como el propietario de la carpeta y evitar problemas de UID.
  - **Samba**: con usuario y contraseña, compatible con Windows.
  - **SSHFS**: cifrado por SSH, sin servicios extra en el servidor.
- **Gestión completa:** crear, editar, reaplicar, comprobar el montaje, ver el registro y borrar.
- **Montajes persistentes** en `/etc/fstab` con `x-systemd.automount` y `nofail`: sobreviven a reinicios y no bloquean el arranque si el servidor está apagado.
- **Comprobación previa** de SSH y `sudo` en todos los equipos antes de cambiar nada.
- Instala automáticamente los paquetes que falten (`nfs-kernel-server`, `nfs-common`, `samba`, `cifs-utils`, `sshfs`).

## Requisitos

- Ubuntu (probado en 22.04) con Python 3.10 o superior. Solo usa la librería estándar.
- Una clave SSH en el equipo donde corre LanShare (`ssh-keygen -t ed25519`).
- En cada equipo gestionado:
  - `openssh-server` instalado.
  - Tu clave autorizada: `ssh-copy-id usuario@ip`.
  - Un usuario con permiso de `sudo`.

## Instalación

```bash
git clone <repo> ~/lanshare
mkdir -p ~/.config/systemd/user
cp ~/lanshare/lanshare.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lanshare
```

Abre <http://127.0.0.1:8765>.

Para ejecutarlo a mano: `python3 lanshare.py --port 8765`.

## Uso

1. **Equipos → Buscar en la red** y añade los equipos que tengan SSH.
2. **Probar** comprueba la conexión SSH y `sudo`. Si `sudo` pide contraseña, indícala con **Sudo**. Solo se guarda en memoria.
3. **Comparticiones → Nueva compartición:** elige el tipo, el servidor, la carpeta, los clientes y el punto de montaje.

Al borrar una compartición se desmonta en los clientes y se deja de compartir. **Los archivos no se borran.**

## Dónde se guarda la configuración

| Qué | Dónde |
|---|---|
| Equipos y comparticiones | `~/.config/lanshare/state.json` (permisos 600) |
| Exportación NFS | `/etc/exports.d/lanshare-<id>.exports` en el servidor |
| Recurso Samba | `/etc/samba/lanshare.conf` en el servidor (incluido desde `smb.conf`) |
| Montaje | `/etc/fstab` en el cliente, entre `# BEGIN lanshare:<id>` y `# END lanshare:<id>` |
| Credenciales y claves de cliente | `/etc/lanshare/` en el cliente |

Las comparticiones funcionan aunque LanShare esté parado; la app solo hace falta para gestionarlas.

## Seguridad

- La web escucha **solo en `127.0.0.1`**. Quien accede a ella puede ejecutar comandos como root en todos los equipos gestionados.
- Para evitar peticiones desde otras webs (CSRF) y ataques de DNS rebinding, la API exige la cabecera `X-LanShare: 1` y que el `Host` sea localhost.
- Las contraseñas de `sudo` nunca se escriben en disco.
- Las contraseñas Samba se guardan en `state.json` porque hacen falta para poder editar la compartición.
- Si un equipo tiene `ufw` activo, abre los puertos a mano: NFS 2049, Samba 445.

## Estructura

```
lanshare.py       servidor HTTP, API y generación de los scripts que se ejecutan en los equipos
index.html        interfaz web (HTML, CSS y JS sin dependencias)
lanshare.service  unidad de systemd --user
```
