# LanShare

Gestor web de carpetas compartidas (NFS, Samba, SSHFS) entre equipos Ubuntu de la LAN. Ver `README.md` para el uso.

## Arquitectura

- `lanshare.py`: un solo fichero, **solo librería estándar** de Python 3.10. No añadir dependencias.
  - `ThreadingHTTPServer` sirve `index.html` y la API JSON bajo `/api/` (función `route`, con `match`).
  - El estado está en `~/.config/lanshare/state.json` y se protege con `LOCK`, que solo se retiene para leer (`fresh_state`) o guardar (`store_share`, `save_state`). Nunca lo retengas durante SSH.
  - `OPS` serializa las operaciones que cambian los equipos (crear, editar, reaplicar, borrar), que pueden tardar minutos. Las lecturas no esperan por él.
  - Editar = `teardown_share` de la versión anterior + `apply_share` de la nueva. Si la nueva falla, se deshace y se reaplica la anterior (`rolled_back` en la respuesta).
  - Las contraseñas de `sudo` van en `SUDO_PW` y se guardan **solo en memoria**. Nunca deben persistirse (ni en `state.json`, ni en logs, ni en respuestas de la API, que solo exponen `sudo_pw: bool`). Cualquier reinicio del proceso las borra: `systemctl --user restart`, `install.sh`, reiniciar el PC. Si se cambia este comportamiento, actualiza la sección «Contraseñas de sudo» del README.
  - `run_root(host, script)` ejecuta un script bash como root: en local con `sudo`; en remoto por `ssh`, con el script en base64 para que funcione con cualquier shell de login.
  - `run_user(host, cmd)` ejecuta sin `sudo`.
  - Los `*_setup_script` / `*_teardown_script` generan el bash de servidor y cliente. Todo empieza por `PRELUDE` (`ensure_pkg`, `remove_block`).
  - `preflight()` comprueba SSH y `sudo` en todos los equipos antes de cambiar nada.
  - `sudo_status()` (`POST /api/sudo-check`) hace la misma comprobación sin lanzar error; el diálogo de compartición la usa para avisar antes de crear si falta la contraseña de sudo.
  - No interpoles en bash campos de texto libre, como el nombre del equipo, que puede venir de mDNS. Solo van datos validados.
- `index.html`: interfaz en un solo fichero con JS vanilla. Todas las llamadas pasan por `api()`; si reciben un mensaje, muestran la capa de carga (`busyOn`/`busyOff`, un `<dialog>` modal para quedar encima de los demás). Cualquier acción que cambie algo debe pasar un mensaje.
- `install.sh`: copia `lanshare.py` e `index.html` a `~/.local/share/easy-lan-share` y genera la unidad `~/.config/systemd/user/lanshare.service`. También tiene `--port`, `--uninstall` y `--purge`. Si se añade un fichero a la app, hay que añadirlo también a la copia del instalador.

## Convenciones

- Los ficheros del sistema se modifican con bloques marcados `# BEGIN lanshare:<id>` / `# END lanshare:<id>`, o con ficheros propios (`lanshare-<id>.exports`). Así editar o borrar es quitar el bloque y reescribirlo.
- Los datos del usuario que acaban en scripts bash se validan con `check_path` / `check_word`, que rechazan comillas, `$`, `` ` ``, `\`, `;`, etc. Los secretos (contraseñas) se pasan siempre en base64, nunca interpolados.
- La API exige la cabecera `X-LanShare: 1` y un `Host` localhost (anti-CSRF y DNS rebinding). El servidor escucha solo en `127.0.0.1`. No cambiar esto.
- Interfaz y mensajes en español.
- Commits con [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`…).

## Probar

```bash
python3 -m py_compile lanshare.py && bash -n install.sh
python3 -m unittest discover -s tests   # sin tocar ningún equipo: valida los scripts con bash -n, la API y la vuelta atrás
python3 lanshare.py --port 8766      # desarrollo: desde el repo, sin tocar el servicio instalado
./install.sh                         # desplegar al servicio (borra las contraseñas sudo en memoria)
curl -s -H 'X-LanShare: 1' localhost:8765/api/state
```

Para validar los scripts generados sin ejecutarlos, pásalos por `bash -n`.

Cuidado con `pkill -f lanshare`: el patrón coincide con la propia shell que lo lanza. Usa `systemctl --user` o `fuser -k <puerto>/tcp`.
