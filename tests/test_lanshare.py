"""Pruebas de LanShare sin tocar ningún equipo: python3 -m unittest -v"""
import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lanshare as L  # noqa: E402

SERVER = {"id": "srv", "name": 'evil$(id)"name', "address": "10.0.0.1", "user": "jose", "port": 22}
CLIENT = {"id": "cli", "name": "cliente", "address": "10.0.0.2", "user": "ana", "port": 22,
          "_pubkey": "ssh-ed25519 AAAAC3Nz lanshare@cli"}


def share(t, **kw):
    return {"id": "abc123", "type": t, "name": "Fotos", "server": "srv", "clients": ["cli"],
            "path": "/srv/mis fotos", "mountpoint": "/mnt/fotos", "readonly": False, "map_owner": True,
            "smb_name": "fotos", "smb_user": "jose", "smb_password": "p'w$`x", "created": 0, **kw}


def bash_ok(script):
    return subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True).returncode == 0


class TempState(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(L, "CONFIG_DIR", d), mock.patch.object(L, "STATE_FILE", d / "state.json")]
        for p in self.patches:
            p.start()
        L.save_state({"hosts": [dict(SERVER), {k: v for k, v in CLIENT.items() if k != "_pubkey"}], "shares": []})

    def tearDown(self):
        for p in self.patches:
            p.stop()


class Scripts(unittest.TestCase):
    def test_generated_scripts_are_valid_bash(self):
        for t in ("nfs", "samba", "sshfs"):
            for ro in (False, True):
                s = share(t, readonly=ro)
                for script in (L.server_setup_script(s, SERVER, [CLIENT]), L.client_setup_script(s, SERVER, CLIENT),
                               L.server_teardown_script(s), L.client_teardown_script(s), L.sshfs_key_script(CLIENT, SERVER)):
                    with self.subTest(type=t, readonly=ro):
                        self.assertTrue(bash_ok(script), script)

    def test_free_text_never_reaches_bash(self):
        for t in ("nfs", "samba", "sshfs"):
            s = share(t)
            joined = L.server_setup_script(s, SERVER, [CLIENT]) + L.client_setup_script(s, SERVER, CLIENT)
            self.assertNotIn("evil", joined)
            self.assertNotIn(s["smb_password"], joined)  # los secretos van en base64

    def test_fstab_escapes_spaces(self):
        self.assertIn("/srv/mis\\040fotos", L.client_setup_script(share("nfs"), SERVER, CLIENT))


class Validation(TempState):
    def test_check_path(self):
        for bad in ("relativa", "/a/../b", "/a;b", "/a$b", '/a"b', "/a`b", "/a\\b"):
            with self.subTest(bad=bad), self.assertRaises(L.ApiError):
                L.check_path(bad, "Ruta")
        self.assertEqual(L.check_path("/srv/datos/", "Ruta"), "/srv/datos")

    def test_check_word(self):
        with self.assertRaises(L.ApiError):
            L.check_word("a b", "x")
        self.assertEqual(L.check_word("fotos_1", "x"), "fotos_1")

    def test_share_validation(self):
        st = L.fresh_state()
        base = {"type": "samba", "server": "srv", "clients": ["cli"], "path": "/srv/a", "mountpoint": "/mnt/a"}
        with self.assertRaises(L.ApiError):
            L.validate_share(st, {**base, "smb_password": "uno\ndos"})
        with self.assertRaises(L.ApiError):
            L.validate_share(st, {**base, "smb_password": "x", "mountpoint": "/home"})
        with self.assertRaises(L.ApiError):
            L.validate_share(st, {**base, "type": "nfs", "clients": []})
        ok = L.validate_share(st, {**base, "smb_password": "secreta"})
        self.assertEqual(ok["smb_name"], "a")

    def test_os_from_banner(self):
        self.assertEqual(L.os_from_banner("SSH-2.0-OpenSSH_10.2p1 Ubuntu-2ubuntu3"), "Ubuntu 26.04")
        self.assertEqual(L.os_from_banner("SSH-2.0-OpenSSH_9.2p1 Debian-2+deb12u3", "Raspberry Pi"),
                         "Raspberry Pi OS (Debian 12)")


class Operations(TempState):
    """Lógica de route() con la ejecución remota simulada."""

    def setUp(self):
        super().setUp()
        for name in ("preflight",):
            p = mock.patch.object(L, name, lambda *a: None)
            p.start()
            self.patches.append(p)

    def fake_apply(self, fail_mount=None):
        def apply(state, s):
            s["status"] = "error" if s["mountpoint"] == fail_mount else "ok"
            return f"aplicado {s['mountpoint']}"
        return apply

    def create(self):
        body = {"type": "nfs", "server": "srv", "clients": ["cli"], "path": "/srv/a", "mountpoint": "/mnt/a"}
        with mock.patch.object(L, "apply_share", self.fake_apply()):
            return L.route("POST", ["shares"], body, {})

    def test_create_saves_share(self):
        r = self.create()
        self.assertEqual(r["share_status"], "ok")
        self.assertEqual([s["mountpoint"] for s in L.load_state()["shares"]], ["/mnt/a"])
        self.assertNotIn("smb_password", json.dumps(r["shares"]))

    def test_failed_edit_restores_previous_version(self):
        sid = self.create()["shares"][0]["id"]
        calls = []
        body = {"type": "nfs", "server": "srv", "clients": ["cli"], "path": "/srv/a", "mountpoint": "/mnt/b"}
        with mock.patch.object(L, "apply_share", self.fake_apply(fail_mount="/mnt/b")), \
             mock.patch.object(L, "teardown_share", lambda st, s, c=None: (calls.append(s["mountpoint"]), (True, ""))[1]):
            r = L.route("PUT", ["shares", sid], body, {})
        self.assertTrue(r["rolled_back"])
        self.assertEqual(r["restored_status"], "ok")
        self.assertEqual(calls, ["/mnt/a", "/mnt/b"])  # quita la vieja, aplica la nueva, la deshace
        self.assertEqual(L.load_state()["shares"][0]["mountpoint"], "/mnt/a")

    def test_successful_edit_keeps_new_version(self):
        sid = self.create()["shares"][0]["id"]
        body = {"type": "nfs", "server": "srv", "clients": ["cli"], "path": "/srv/a", "mountpoint": "/mnt/b"}
        with mock.patch.object(L, "apply_share", self.fake_apply()), \
             mock.patch.object(L, "teardown_share", lambda *a: (True, "")):
            r = L.route("PUT", ["shares", sid], body, {})
        self.assertFalse(r["rolled_back"])
        self.assertEqual(L.load_state()["shares"][0]["mountpoint"], "/mnt/b")

    def test_store_share_keeps_concurrent_host_changes(self):
        st = L.fresh_state()
        s = L.validate_share(st, {"type": "nfs", "server": "srv", "clients": ["cli"], "path": "/srv/a", "mountpoint": "/mnt/a"})
        L.route("POST", ["hosts"], {"address": "10.0.0.9", "user": "jose"}, {})  # cambio mientras dura la operación
        L.store_share(s)
        st = L.load_state()
        self.assertIn("10.0.0.9", [h["address"] for h in st["hosts"]])
        self.assertEqual(len(st["shares"]), 1)

    def test_cannot_remove_host_during_operation(self):
        with L.OPS, self.assertRaises(L.ApiError):
            L.route("DELETE", ["hosts", "cli"], {}, {})

    def test_host_validation(self):
        for body in ({"address": "10.0.0.9", "user": "jose", "port": "abc"},
                     {"address": "10.0.0.9", "user": "jose", "id": "x'y"},
                     {"address": "a b", "user": "jose"}):
            with self.subTest(body=body), self.assertRaises(L.ApiError):
                L.route("POST", ["hosts"], body, {})


class HttpSecurity(TempState):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), L.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def get(self, path, headers):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=5)
        c.request("GET", path, headers=headers)
        return c.getresponse().status

    def test_api_requires_header_and_localhost(self):
        self.assertEqual(self.get("/api/state", {"X-LanShare": "1"}), 200)
        self.assertEqual(self.get("/api/state", {}), 403)
        self.assertEqual(self.get("/api/state", {"X-LanShare": "1", "Host": "evil.example:8765"}), 403)
        self.assertEqual(self.get("/", {"Host": "evil.example"}), 403)


if __name__ == "__main__":
    unittest.main()
