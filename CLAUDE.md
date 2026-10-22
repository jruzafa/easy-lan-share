# LanShare

Gestor web de carpetas compartidas (NFS, Samba, SSHFS) entre equipos Ubuntu de la LAN. Ver `README.md` para el uso.

## Arquitectura

- `lanshare.py`: un solo fichero, **solo librería estándar** de Python 3.10. No añadir dependencias.
  - `ThreadingHTTPServer` sirve `index.html` y la API JSON bajo `/api/` (función `route`, con `match`).
  - El estado está en `~/.config/lanshare/state.json` y se protege con `LOCK`. Las contraseñas de `sudo` van en `SUDO_PW` (solo en memoria; nunca persistirlas).
  - `run_root(host, script)` ejecuta un script bash como root: en local con `sudo`; en remoto por `ssh`, con el script en base64 para que funcione con cualquier shell de login.
  - `run_user(host, cmd)` ejecuta sin `sudo`.
  - Los `*_setup_script` / `*_teardown_script` generan el bash de servidor y cliente. Todo empieza por `PRELUDE` (`ensure_pkg`, `remove_block`).
  - `preflight()` comprueba SSH y `sudo` en todos los equipos antes de cambiar nada.
- `index.html`: interfaz en un solo fichero con JS vanilla. Todas las llamadas pasan por `api()`.

## Convenciones

- Los ficheros del sistema se modifican con bloques marcados `# BEGIN lanshare:<id>` / `# END lanshare:<id>`, o con ficheros propios (`lanshare-<id>.exports`). Así editar o borrar es quitar el bloque y reescribirlo.
- Editar una compartición = `teardown_share` del estado anterior + `apply_share` del nuevo.
- Los datos del usuario que acaban en scripts bash se validan con `check_path` / `check_word`, que rechazan comillas, `$`, `` ` ``, `\`, `;`, etc. Los secretos (contraseñas) se pasan siempre en base64, nunca interpolados.
- La API exige la cabecera `X-LanShare: 1` y un `Host` localhost (anti-CSRF y DNS rebinding). El servidor escucha solo en `127.0.0.1`. No cambiar esto.
- Interfaz y mensajes en español.
- Commits con [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`…).

## Probar

```bash
python3 -m py_compile lanshare.py
systemctl --user restart lanshare   # reiniciar borra las contraseñas sudo en memoria
curl -s -H 'X-LanShare: 1' localhost:8765/api/state
```

Para validar los scripts generados sin ejecutarlos, pásalos por `bash -n`.

Cuidado con `pkill -f lanshare`: el patrón coincide con la propia shell que lo lanza. Usa `systemctl --user` o `fuser -k <puerto>/tcp`.
