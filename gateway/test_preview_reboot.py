"""Previews survive a reboot: every preview, file share and Cua share is a systemd --user unit file, enabled and started.

Nothing here runs systemctl or tailscale. The recording FakeSystem from test_preview_files keeps unit files in a temp
folder and answers `systemctl --user` the way the user manager would; it fails the test if anything calls systemd-run.
"""

import contextlib
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_preview_cua as tpc
import test_preview_files as tpf

pv = tpf.pv
TS_IP, TS_NAME = tpf.TS_IP, tpf.TS_NAME
TRANSIENT = "/run/user/1000/systemd/transient/"


def unit_text(fake, name):
    return (fake.unit_dir() / name).read_text()


def values(text, key):
    return [line[len(key) + 1:] for line in text.splitlines() if line.startswith(key + "=")]


def opt(cmd, name):
    return cmd[cmd.index(name) + 1]


def systemctl(fake, verb):
    return [a[3:] for a in fake.argvs() if a[:3] == ["systemctl", "--user", verb]]


def unit_files(fake):
    return sorted(p.name for p in fake.unit_dir().glob("*.service")) if fake.unit_dir().exists() else []


def quick_wait(sysm, host, port, seconds=4.0):
    """wait_listening without its four-second wall clock: the stand-in either listens at once or never."""
    return sysm.can_connect(host, port)


def age(fake):
    """Turn every preview into what an older version left behind: transient units (systemd-run), no unit files."""
    for path in list(fake.unit_dir().glob("*.service")):
        unit = next(u for u in list(fake.active) + [path.stem, path.name] if fake.unit_path(u) == path)
        if unit in fake.active:
            fake.transient[unit] = TRANSIENT + path.name
        fake.enabled.discard(unit)
        path.unlink()
    for entry in pv.all_states():
        entry.pop("persistent", None)
        pv.save_state(entry)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.home = base / "home"
        self.folder = self.home / "reports"
        self.folder.mkdir(parents=True)
        (self.folder / "notes.md").write_text("# Hi\n")
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home), "PREVIEW_STATE_DIR": str(base / "state"),
                                             "PREVIEW_BACKEND": ""})
        patch.start()
        self.addCleanup(patch.stop)

    def main(self, argv, sysm):
        return tpf.quiet(pv.main, argv, sysm=sysm)

    def links(self):
        return {e["port"]: (e["link"], e["listen"], e["unit"], e.get("file_unit"), e.get("path")) for e in pv.all_states()}


class UnitFileTests(Base):
    def test_a_port_preview_is_an_enabled_unit_file_running_the_same_proxy(self):
        fake = tpf.FakeSystem(operator=False)
        code, out, err = self.main(["5173", "--name", "app"], fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), f"http://{TS_NAME}:5173")
        self.assertEqual(unit_files(fake), ["preview-5173.service"])
        text = unit_text(fake, "preview-5173.service")
        self.assertEqual(text.splitlines()[0], pv.UNIT_MARKER)
        self.assertEqual(values(text, "WantedBy"), ["default.target"])
        self.assertEqual(values(text, "Description"), ["preview 5173: app"])
        for key, value in (("Restart", "on-failure"), ("RestartSec", "2"), ("RestartPreventExitStatus", "78"),
                           ("StartLimitIntervalSec", "0"), ("Environment", "PYTHONUNBUFFERED=1")):
            self.assertEqual(values(text, key), [value], key)
        cmd = tpf.exec_start(text)
        self.assertEqual(cmd[:4], ["/usr/bin/python3", "-I", pv.self_path(), "_serve"])
        self.assertEqual((opt(cmd, "--listen"), opt(cmd, "--target"), opt(cmd, "--public-url")),
                         (f"{TS_IP}:5173", "5173", f"http://{TS_NAME}:5173"))
        self.assertEqual(fake.active["preview-5173"], (TS_IP, 5173))
        self.assertEqual(fake.enabled, {"preview-5173"})
        commands = fake.argvs()
        reload, enable, start = (commands.index(["systemctl", "--user", verb, *rest]) for verb, *rest in
                                 (["daemon-reload"], ["enable", "preview-5173"], ["start", "preview-5173"]))
        self.assertLess(reload, enable)
        self.assertLess(enable, start)

    def test_the_https_proxy_listens_on_loopback_behind_a_persistent_serve_entry(self):
        fake = tpf.FakeSystem(operator=True, serve_allowed=True)
        self.assertEqual(self.main(["5173"], fake)[0], 0)
        cmd = tpf.exec_start(unit_text(fake, "preview-5173.service"))
        self.assertTrue(opt(cmd, "--listen").startswith("127.0.0.1:"))
        [serve] = [a for a in fake.argvs() if a[:2] == ["tailscale", "serve"] and "--https=5173" in a and a[-1] != "off"]
        self.assertIn("--bg", serve)  # tailscaled keeps --bg serve config across a restart and a reboot
        self.assertEqual(fake.serve[5173], "http://" + opt(cmd, "--listen"))

    def test_a_file_share_has_two_enabled_unit_files_bound_to_loopback(self):
        fake = tpf.FakeSystem(operator=True, serve_allowed=True)
        code, out, err = self.main(["file", str(self.folder)], fake)
        self.assertEqual(code, 0, err)
        port = int(out.strip().rsplit(":", 1)[1])
        self.assertEqual(unit_files(fake), [f"preview-{port}.service", f"preview-file-{port}.service"])
        server = tpf.exec_start(unit_text(fake, f"preview-file-{port}.service"))
        self.assertEqual(server, ["/usr/bin/python3", "-I", pv.self_path(), "_serve-file", "--root", str(self.folder),
                                  "--port", str(port)])
        self.assertEqual(values(unit_text(fake, f"preview-file-{port}.service"), "WantedBy"), ["default.target"])
        self.assertEqual(fake.enabled, {f"preview-{port}", f"preview-file-{port}.service"})
        self.assertTrue(opt(tpf.exec_start(unit_text(fake, f"preview-{port}.service")), "--listen").startswith("127.0.0.1:"))

    def test_cua_shares_are_unit_files_that_keep_the_owner_gate(self):
        fake = tpc.CuaSystem(operator=True, serve_allowed=True)
        code, _, err = self.main(["cua"], fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(unit_files(fake), ["preview-18500.service", "preview-18501.service", "preview-18502.service",
                                            "preview-cua-index.service"])
        index = tpf.exec_start(unit_text(fake, "preview-cua-index.service"))
        self.assertEqual(index[3:5], ["_cua-index", "--port"])
        for port in (18500, 18501, 18502):
            cmd = tpf.exec_start(unit_text(fake, f"preview-{port}.service"))
            self.assertEqual(opt(cmd, "--owner-login"), tpc.OWNER)
            self.assertTrue(opt(cmd, "--listen").startswith("127.0.0.1:"))
        self.assertEqual(opt(tpf.exec_start(unit_text(fake, "preview-18502.service")), "--upstream-host"), "100.116.84.79")
        self.assertEqual(fake.enabled, {"preview-18500", "preview-18501", "preview-18502", "preview-cua-index.service"})

    def test_a_label_cannot_add_lines_or_specifiers_to_the_unit(self):
        fake = tpf.FakeSystem(operator=False)
        code, _, err = self.main(["5173", "--name", "x\n[Service]\nExecStartPre=/bin/sh -c evil %n $HOME\\"], fake)
        self.assertEqual(code, 0, err)
        text = unit_text(fake, "preview-5173.service")
        self.assertEqual(values(text, "ExecStartPre"), [])
        self.assertEqual([l for l in text.splitlines() if l.startswith("[")], ["[Unit]", "[Service]", "[Install]"])
        [description] = values(text, "Description")
        self.assertNotRegex(description.replace("%%", ""), "%")
        self.assertFalse(description.endswith("\\"))

    def test_unit_start_refuses_control_characters_foreign_names_and_foreign_files(self):
        fake = tpf.FakeSystem()
        ok, why = fake.unit_start("preview-5173", "x", ["/usr/bin/python3", "-I", "a\nb"])
        self.assertFalse(ok)
        self.assertIn("control", why)
        for name in ("ssh-agent.service", "preview-abc", "../preview-5173", "preview-5173.timer"):
            self.assertFalse(fake.unit_start(name, "x", ["/usr/bin/python3"])[0], name)
        fake.unit_dir().mkdir(parents=True, exist_ok=True)
        mine = fake.unit_dir() / "preview-5173.service"
        mine.write_text("[Service]\nExecStart=/bin/true\n")
        ok, why = fake.unit_start("preview-5173", "x", ["/usr/bin/python3"])
        self.assertFalse(ok)
        self.assertIn(str(mine), why)
        self.assertEqual(mine.read_text(), "[Service]\nExecStart=/bin/true\n")
        self.assertEqual(fake.started, [])
        self.assertEqual(systemctl(fake, "stop"), [])  # a unit someone else owns is not even stopped

    def test_a_unit_still_loaded_from_another_file_is_a_failure(self):
        class Stuck(tpf.FakeSystem):
            def run(self, argv, timeout=30):
                if argv[:3] == ["systemctl", "--user", "show"]:
                    self.commands.append(list(argv))
                    return pv.subprocess.CompletedProcess(argv, 0, TRANSIENT + "preview-5173.service\n", "")
                return super().run(argv, timeout)

        fake = Stuck(operator=False)
        code, out, err = self.main(["5173"], fake)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(TRANSIENT, err)
        self.assertEqual(unit_files(fake), [])
        self.assertEqual(fake.enabled, set())


class StopTests(Base):
    def test_stop_all_disables_and_deletes_exactly_its_own_unit_files(self):
        fake = tpf.FakeSystem(operator=False)
        self.main(["5173"], fake)
        _, out, _ = self.main(["file", str(self.folder)], fake)
        port = int(out.strip().rsplit(":", 1)[1])
        (fake.unit_dir() / "other.service").write_text(pv.UNIT_MARKER + "\n[Service]\nExecStart=/bin/true\n")
        (fake.unit_dir() / "preview-9999.service").write_text("[Service]\nExecStart=/bin/true\n")
        wants = fake.unit_dir() / "default.target.wants"
        wants.mkdir()
        (wants / "other.service").symlink_to(fake.unit_dir() / "other.service")
        code, _, _ = self.main(["stop", "--all"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(unit_files(fake), ["other.service", "preview-9999.service"])
        self.assertEqual([p.name for p in wants.iterdir()], ["other.service"])
        self.assertEqual(sorted(map(tuple, systemctl(fake, "disable"))),
                         sorted([("preview-5173",), (f"preview-{port}",), (f"preview-file-{port}.service",)]))
        self.assertEqual((fake.enabled, fake.active), (set(), {}))
        self.assertEqual(fake.argvs()[-1], ["systemctl", "--user", "daemon-reload"])

    def test_stopping_one_preview_keeps_the_others(self):
        fake = tpf.FakeSystem(operator=False)
        self.main(["5173"], fake)
        self.main(["8000"], fake)
        self.assertEqual(self.main(["stop", "5173"], fake)[0], 0)
        self.assertEqual(unit_files(fake), ["preview-8000.service"])
        self.assertEqual(fake.enabled, {"preview-8000"})

    def test_stop_cua_removes_the_index_and_every_space_unit(self):
        fake = tpc.CuaSystem(operator=True, serve_allowed=True)
        self.main(["cua"], fake)
        self.main(["5173"], fake)
        self.assertEqual(self.main(["stop", "cua"], fake)[0], 0)
        self.assertEqual(unit_files(fake), ["preview-5173.service"])

    def test_a_proxy_that_never_comes_up_leaves_no_unit_file(self):
        class Dead(tpf.FakeSystem):
            def listen_for(self, unit, cmd):
                return None if "_serve" in cmd else super().listen_for(unit, cmd)

        fake = Dead(operator=False)
        with mock.patch.object(pv, "wait_listening", quick_wait):
            code, _, _ = self.main(["file", str(self.folder)], fake)
        self.assertEqual(code, 1)
        self.assertEqual((unit_files(fake), fake.enabled), ([], set()))


class AdoptTests(Base):
    """Previews an older version started with systemd-run become unit files on the next run of the same command."""

    def test_rerunning_a_port_preview_adopts_it_with_the_same_port_link_and_serve_entry(self):
        fake = tpf.FakeSystem(operator=True, serve_allowed=True)
        _, link, _ = self.main(["5173", "--name", "vite"], fake)
        before, serve = self.links(), dict(fake.serve)
        age(fake)
        started, mark = len(fake.started), len(fake.argvs())
        code, out, err = self.main(["5173"], fake)
        self.assertEqual((code, out), (0, link), err)
        self.assertEqual((self.links(), fake.serve), (before, serve))
        self.assertEqual(len([a for a in fake.argvs() if "--https=5173" in a]), 1)  # serve was set up once, never again
        self.assertEqual(unit_files(fake), ["preview-5173.service"])
        self.assertEqual(len(fake.started), started + 1)
        self.assertEqual(opt(fake.started[-1][1], "--listen"), before[5173][1])
        rerun = fake.argvs()[mark:]
        self.assertLess(rerun.index(["systemctl", "--user", "stop", "preview-5173"]),  # the transient unit goes first
                        rerun.index(["systemctl", "--user", "start", "preview-5173"]))
        self.assertEqual(pv.load_state(5173)["label"], "vite")
        self.assertEqual(self.main(["5173"], fake)[1], link)
        self.assertEqual(len(fake.started), started + 1)  # adopted once; the next run changes nothing

    def test_rerunning_a_file_share_adopts_both_units(self):
        fake = tpf.FakeSystem(operator=False)
        _, link, _ = self.main(["file", str(self.folder)], fake)
        before = self.links()
        age(fake)
        code, out, err = self.main(["file", str(self.folder)], fake)
        self.assertEqual((code, out, self.links()), (0, link, before), err)
        port = int(link.strip().rsplit(":", 1)[1])
        self.assertEqual(unit_files(fake), [f"preview-{port}.service", f"preview-file-{port}.service"])
        self.assertEqual(fake.enabled, {f"preview-{port}", f"preview-file-{port}.service"})

    def test_rerunning_cua_adopts_the_index_and_every_space(self):
        fake = tpc.CuaSystem(operator=True, serve_allowed=True)
        _, link, _ = self.main(["cua"], fake)
        before = self.links()
        age(fake)
        code, out, err = self.main(["cua"], fake)
        self.assertEqual((code, out, self.links()), (0, link, before), err)
        self.assertEqual(len(unit_files(fake)), 4)
        self.assertEqual(len(fake.enabled), 4)
        self.assertEqual(opt(tpf.exec_start(unit_text(fake, "preview-18501.service")), "--owner-login"), tpc.OWNER)


class RestoreTests(Base):
    def prepare(self, fake):
        self.main(["5173", "--name", "board"], fake)
        _, out, _ = self.main(["file", str(self.folder)], fake)
        self.file_port = int(out.strip().rsplit(":", 1)[1])
        before = self.links()
        age(fake)
        return before

    def test_restore_makes_every_recorded_preview_a_unit_file_without_changing_ports_or_links(self):
        fake = tpf.FakeSystem(operator=True, serve_allowed=True)
        before = self.prepare(fake)
        serve, mark = dict(fake.serve), len(fake.argvs())
        code, out, err = self.main(["restore"], fake)
        self.assertEqual(code, 0, err)
        port = self.file_port
        self.assertEqual(unit_files(fake), sorted(["preview-5173.service", f"preview-{port}.service", f"preview-file-{port}.service"]))
        self.assertEqual(fake.enabled, {"preview-5173", f"preview-{port}", f"preview-file-{port}.service"})
        self.assertEqual((self.links(), fake.serve), (before, serve))
        self.assertFalse([a for a in fake.argvs()[mark:] if a[:2] == ["tailscale", "serve"] and a[2] != "status"])
        self.assertIn("5173", out)
        self.assertIn(str(port), out)
        self.assertEqual(opt(tpf.exec_start(unit_text(fake, "preview-5173.service")), "--listen"), before[5173][1])

    def test_restore_is_idempotent(self):
        fake = tpf.FakeSystem(operator=False)
        self.prepare(fake)
        self.assertEqual(self.main(["restore"], fake)[0], 0)
        commands = len(fake.argvs())
        files = {name: unit_text(fake, name) for name in unit_files(fake)}
        code, out, _ = self.main(["restore"], fake)
        self.assertEqual(code, 0)
        self.assertIn("nothing to restore", out)
        self.assertFalse([a for a in fake.argvs()[commands:] if a[:2] == ["systemctl", "--user"]])
        self.assertEqual({name: unit_text(fake, name) for name in unit_files(fake)}, files)

    def test_restore_with_no_previews(self):
        code, out, _ = self.main(["restore"], tpf.FakeSystem())
        self.assertEqual(code, 0)
        self.assertIn("nothing to restore", out)

    def test_restore_brings_back_cua_shares_with_their_gate(self):
        fake = tpc.CuaSystem(operator=True, serve_allowed=True)
        self.main(["cua"], fake)
        age(fake)
        code, _, err = self.main(["restore"], fake)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(unit_files(fake)), 4)
        for port in (18500, 18501, 18502):
            self.assertEqual(opt(tpf.exec_start(unit_text(fake, f"preview-{port}.service")), "--owner-login"), tpc.OWNER)
        self.assertEqual(opt(tpf.exec_start(unit_text(fake, "preview-cua-index.service")), "--cua"), tpc.CUA)

    def test_restore_never_starts_a_damaged_cua_share_without_its_gate(self):
        fake = tpc.CuaSystem(operator=True, serve_allowed=True)
        self.main(["cua"], fake)
        age(fake)
        entry = pv.load_state(18501)
        entry["owner_login"] = ""
        pv.save_state(entry)
        code, _, err = self.main(["restore"], fake)
        self.assertEqual(code, 1)
        self.assertIn("18501", err)
        self.assertNotIn("preview-18501.service", unit_files(fake))
        self.assertIn("preview-18502.service", unit_files(fake))

    def test_restore_reports_a_unit_that_does_not_come_up(self):
        class Dead(tpf.FakeSystem):
            def listen_for(self, unit, cmd):
                return None if "_serve-file" in cmd and self.dead else super().listen_for(unit, cmd)

        fake = Dead(operator=False)
        fake.dead = False
        self.prepare(fake)
        fake.dead = True
        with mock.patch.object(pv, "wait_listening", quick_wait):
            code, out, err = self.main(["restore"], fake)
        self.assertEqual(code, 1)
        self.assertIn(f"preview-file-{self.file_port}.service", err)
        self.assertIn("preview-5173.service", unit_files(fake))


class DoctorTests(Base):
    def doctor_line(self, fake):
        _, out, _ = self.main(["doctor"], fake)
        [line] = [line for line in out.splitlines() if line.startswith("persistent")]
        return line

    def test_doctor_reports_transient_previews_with_the_fix(self):
        fake = tpf.FakeSystem(operator=False)
        self.assertEqual(self.doctor_line(fake).split()[1], "yes")
        self.main(["5173"], fake)
        _, out, _ = self.main(["file", str(self.folder)], fake)
        self.assertEqual(self.doctor_line(fake).split()[1], "yes")
        age(fake)
        line = self.doctor_line(fake)
        self.assertEqual(line.split()[1], "no")
        self.assertIn("5173", line)
        self.assertIn(out.strip().rsplit(":", 1)[1], line)
        self.assertIn("preview restore", line)
        self.main(["restore"], fake)
        self.assertEqual(self.doctor_line(fake).split()[1], "yes")

    def test_one_missing_unit_file_of_a_file_share_counts(self):
        fake = tpf.FakeSystem(operator=False)
        _, out, _ = self.main(["file", str(self.folder)], fake)
        port = out.strip().rsplit(":", 1)[1]
        (fake.unit_dir() / f"preview-file-{port}.service").unlink()
        self.assertIn(port, self.doctor_line(fake))
        self.assertEqual(self.doctor_line(fake).split()[1], "no")


class ServeTests(unittest.TestCase):
    def test_the_http_proxy_retries_while_the_tailnet_address_is_not_up_yet(self):
        address = "100.127.255.254"
        with socket.socket() as probe:
            try:
                probe.bind((address, 0))
            except OSError:
                pass
            else:
                self.skipTest(f"{address} is configured on this machine")
        code, _, err = tpf.quiet(pv.main, ["_serve", "--listen", f"{address}:41999", "--target", "5173"])
        self.assertEqual(code, 1, err)  # after a reboot tailscale0 may come up after the unit: systemd restarts it
        self.assertNotEqual(code, pv.EXIT_CONFIG)

    def test_help_says_previews_survive_a_reboot_and_names_restore(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            pv.parse(["--help"])
        self.assertIn("reboot", out.getvalue())
        self.assertIn("preview restore", out.getvalue())
        self.assertEqual(pv.parse(["restore"])[0], "restore")


class SystemTests(unittest.TestCase):
    """The real System methods, with run() recording instead of running anything."""

    def test_serve_add_uses_background_mode_which_tailscaled_persists(self):
        system = pv.System()
        calls = []
        system.run = lambda argv, timeout=30: calls.append(argv) or pv.subprocess.CompletedProcess(argv, 0, "", "")
        system.serve_add(5173, 40001)
        self.assertEqual(calls, [["tailscale", "serve", "--bg", "--yes", "--https=5173", "http://127.0.0.1:40001"]])

    def test_unit_dir_is_the_user_unit_folder_systemd_reads(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home, "XDG_CONFIG_HOME": "/elsewhere"}):
            self.assertEqual(pv.System().unit_dir(), Path(home) / ".config" / "systemd" / "user")


if __name__ == "__main__":
    unittest.main()
