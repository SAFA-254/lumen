"""Перенос с Lumen (deploy/luxmon-migrate.py) на дереве файлов во временном каталоге; systemd и процессы подменены."""
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("luxmon_migrate", os.path.join(ROOT, "deploy", "luxmon-migrate.py"))
mig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mig)


class Fake(mig.Migration):
    STOP_WAIT = 0

    def __init__(self, dry, root, units=None, procs=()):
        super().__init__(dry, root)
        self.units = units if units is not None else {}
        self.procs = list(procs)
        self.calls = []

    def unit_state(self, unit):
        return dict(self.units.get(unit, {"LoadState": "not-found", "ActiveState": "inactive"}))

    def systemctl(self, *args):
        self.calls.append(args)
        action, unit = args
        if action == "disable":
            self.units[unit]["UnitFileState"] = "disabled"
        if action == "stop":
            self.units[unit]["ActiveState"] = "inactive"
            self.procs = []
        if action == "enable":
            self.units[unit]["UnitFileState"] = "enabled"
        if action == "start":
            self.units[unit]["ActiveState"] = "active"

    def old_processes(self):
        return self.procs


def running():
    return {"LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled"}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="luxmon-migrate-")
        self.addCleanup(shutil.rmtree, self.root, True)
        # вывод переноса не нужен в выводе тестов, а консоль Windows не печатает «→»
        self.out = io.StringIO()
        for name in ("stdout", "stderr"):
            p = mock.patch("sys." + name, io.StringIO() if name == "stderr" else self.out)
            p.start()
            self.addCleanup(p.stop)

    def path(self, p):
        return self.root + p

    def write(self, p, data):
        os.makedirs(os.path.dirname(self.path(p)), exist_ok=True)
        with open(self.path(p), "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def read(self, p):
        with open(self.path(p), encoding="utf-8") as f:
            return f.read()

    def make_db(self, p="/var/lib/lumen/lumen.db", rows=500):
        """База как после остановки сервера: часть записей ещё только в -wal."""
        db = self.path(p)
        os.makedirs(os.path.dirname(db), exist_ok=True)
        c = sqlite3.connect(db)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE raw(host TEXT, v REAL)")
        c.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.executemany("INSERT INTO raw VALUES(?,?)", [("n1", i) for i in range(rows)])
        c.execute("INSERT INTO meta VALUES('settings','{}')")
        c.commit()
        if hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE"):
            c.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        c.close()
        return db

    def tree(self):
        out = {}
        for d, _, files in os.walk(self.root):
            for f in files:
                st = os.stat(os.path.join(d, f))
                out[os.path.relpath(os.path.join(d, f), self.root)] = (st.st_size, st.st_mtime_ns)
        return out

    def old_install(self):
        self.write("/etc/lumen/server.json", {"token": "secret", "tls_cert": "/etc/lumen/server.crt",
                                              "tls_key": "/etc/lumen/server.key", "notify_cmd": "/opt/lumen/tg-notify.py",
                                              "users": {"admin": "pbkdf2$x"}})
        self.write("/etc/lumen/agent.json", {"server": "https://s", "token": "secret", "ca_file": "/etc/lumen/server.crt"})
        self.write("/etc/lumen/telegram.env", "TELEGRAM_TOKEN=a\nTELEGRAM_CHAT=1\nLUMEN_BOT_TOKEN=b\nLUMEN_BOT_CHATS=1,2\n")
        self.write("/etc/lumen/server.crt", "CERT")
        self.write("/var/lib/lumen-agent/journal.cursor", "s=abc")
        self.write("/var/lib/lumen-watchdog/state.json", {"down_since": 0})
        return {"lumen-server.service": running(), "lumen-agent.service": running(),
                "lumen-watchdog.timer": running(), "lumen-watchdog.service": {"LoadState": "loaded",
                                                                              "ActiveState": "inactive",
                                                                              "UnitFileState": "static"}}


class FullMigration(Base):
    def test_everything_moved_and_old_kept(self):
        units = self.old_install()
        src = self.make_db()
        # до Python 3.12 нет setconfig: при закрытии журнал вливается в базу, и -wal может не остаться
        has_wal = os.path.exists(src + "-wal") and os.path.getsize(src + "-wal") > 0
        m = Fake(False, self.root, units, procs=["1 python3 /opt/lumen/lumen-server.py"])
        m.run()

        # старые службы остановлены и отключены, таймер сторожа — первым
        self.assertEqual(m.calls[0], ("disable", "lumen-watchdog.timer"))
        for u in ("lumen-server.service", "lumen-agent.service", "lumen-watchdog.timer"):
            self.assertIn(("disable", u), m.calls)
            self.assertIn(("stop", u), m.calls)

        # база: все строки, включая лежавшие только в -wal
        c = sqlite3.connect(self.path(mig.NEW_DB))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM raw").fetchone()[0], 500)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM meta").fetchone()[0], 1)
        c.close()
        self.assertFalse(os.path.exists(self.path(mig.NEW_DB) + "-wal"))
        # резервная копия как есть, вместе с -wal
        backups = [d for d in os.listdir(self.path("/var/lib/lumen")) if d.startswith("backup-")]
        self.assertEqual(len(backups), 1)
        files = os.listdir(self.path("/var/lib/lumen/" + backups[0]))
        self.assertIn("lumen.db", files)
        if has_wal:
            self.assertIn("lumen.db-wal", files)
        b = sqlite3.connect(self.path("/var/lib/lumen/" + backups[0] + "/lumen.db"))
        self.assertEqual(b.execute("SELECT COUNT(*) FROM raw").fetchone()[0], 500)
        b.close()
        # старая база на месте и с теми же данными — для отката
        o = sqlite3.connect(src)
        self.assertEqual(o.execute("SELECT COUNT(*) FROM raw").fetchone()[0], 500)
        o.close()

        # конфиги: пути и имена переменных новые, остальное без изменений
        srv = json.loads(self.read("/etc/luxmon/server.json"))
        self.assertEqual(srv["tls_cert"], "/etc/luxmon/server.crt")
        self.assertEqual(srv["notify_cmd"], "/opt/luxmon/tg-notify.py")
        self.assertEqual(srv["users"], {"admin": "pbkdf2$x"})
        self.assertNotIn("db", srv)
        self.assertEqual(json.loads(self.read("/etc/luxmon/agent.json"))["ca_file"], "/etc/luxmon/server.crt")
        env = self.read("/etc/luxmon/telegram.env")
        self.assertIn("LUXMON_BOT_TOKEN=b\n", env)
        self.assertIn("LUXMON_BOT_CHATS=1,2\n", env)
        self.assertIn("TELEGRAM_TOKEN=a\n", env)
        self.assertNotIn("LUMEN_", env)
        self.assertEqual(self.read("/etc/luxmon/server.crt"), "CERT")
        self.assertEqual(self.read("/var/lib/luxmon-agent/journal.cursor"), "s=abc")
        self.assertTrue(os.path.exists(self.path("/var/lib/luxmon-watchdog/state.json")))
        self.assertTrue(os.path.exists(self.path("/etc/lumen/server.json")))

    def test_secrets_not_printed(self):
        units = self.old_install()
        Fake(True, self.root, units).run()
        self.assertNotIn("secret", self.out.getvalue())
        self.assertNotIn("=b", self.out.getvalue())
        self.assertIn("LUMEN_BOT_TOKEN → LUXMON_BOT_TOKEN", self.out.getvalue())

    def test_custom_db_path(self):
        self.write("/etc/lumen/server.json", {"token": "t", "db": "/srv/lumen/l.db"})
        self.make_db("/srv/lumen/l.db", rows=7)
        Fake(False, self.root, {"lumen-server.service": running()}).run()
        self.assertEqual(json.loads(self.read("/etc/luxmon/server.json"))["db"], mig.NEW_DB)
        c = sqlite3.connect(self.path(mig.NEW_DB))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM raw").fetchone()[0], 7)
        c.close()


class Safety(Base):
    def test_dry_run_changes_nothing(self):
        units = self.old_install()
        self.make_db()
        before = self.tree()
        m = Fake(True, self.root, units, procs=["1 python3 /opt/lumen/lumen-server.py"])
        m.run()
        self.assertEqual(m.calls, [])
        self.assertEqual(self.tree(), before)

    def test_existing_new_db_stops_before_any_change(self):
        units = self.old_install()
        self.make_db()
        self.write(mig.NEW_DB, "")
        before = self.tree()
        m = Fake(False, self.root, units)
        with self.assertRaisesRegex(mig.MigrateError, "уже есть база"):
            m.run()
        self.assertEqual(m.calls, [])  # старый сервер не остановлен
        self.assertEqual(self.tree(), before)

    def test_new_db_wal_alone_counts_as_existing(self):
        self.write("/etc/lumen/server.json", {"token": "t"})
        self.make_db()
        self.write(mig.NEW_DB + "-wal", "x")
        with self.assertRaises(mig.MigrateError):
            Fake(False, self.root, {"lumen-server.service": running()}).run()

    def test_process_still_running_blocks_copy(self):
        units = self.old_install()
        self.make_db()
        m = Fake(False, self.root, units)
        m.procs = ["1 python3 /opt/lumen/lumen-server.py"]
        m.systemctl = lambda *a: m.calls.append(a)  # служба «остановилась», а процесс остался
        with self.assertRaisesRegex(mig.MigrateError, "не остановлен"):
            m.run()
        self.assertFalse(os.path.exists(self.path(mig.NEW_DB)))
        self.assertFalse(os.path.exists(self.path("/etc/luxmon")))

    def test_existing_config_not_overwritten(self):
        units = self.old_install()
        self.write("/etc/luxmon/agent.json", {"server": "https://new", "token": "own"})
        Fake(False, self.root, units).run()
        self.assertEqual(json.loads(self.read("/etc/luxmon/agent.json")), {"server": "https://new", "token": "own"})

    def test_bad_json_stops_before_any_change(self):
        units = self.old_install()
        self.write("/etc/lumen/server.json", "{oops")
        m = Fake(False, self.root, units)
        with self.assertRaisesRegex(mig.MigrateError, "не JSON"):
            m.run()
        self.assertEqual(m.calls, [])


class DbPath(Base):
    def test_relative_db_stops_before_any_change(self):
        self.write("/etc/lumen/server.json", {"token": "t", "db": "lumen.db"})
        self.make_db()
        m = Fake(False, self.root, {"lumen-server.service": running()})
        with self.assertRaisesRegex(mig.MigrateError, "абсолютным"):
            m.run()
        self.assertEqual(m.calls, [])

    def test_non_string_db_stops(self):
        self.write("/etc/lumen/server.json", {"token": "t", "db": 5})
        with self.assertRaisesRegex(mig.MigrateError, "абсолютным"):
            Fake(False, self.root, {"lumen-server.service": running()}).run()

    def test_existing_new_config_points_elsewhere(self):
        """/etc/luxmon/server.json перенос не перезаписывает — значит, база должна оказаться там, где он её ищет."""
        units = self.old_install()
        self.make_db()
        self.write("/etc/luxmon/server.json", {"token": "t", "db": "/var/lib/luxmon/other.db"})
        before = self.tree()
        m = Fake(False, self.root, units)
        with self.assertRaisesRegex(mig.MigrateError, "other.db"):
            m.run()
        self.assertEqual(m.calls, [])
        self.assertEqual(self.tree(), before)


class Interrupted(Base):
    def test_after_db_published_before_marker(self):
        units = self.old_install()
        self.make_db()
        real_replace = os.replace

        def crash(a, b):
            if b.endswith(".migrated-from-lumen"):
                raise KeyboardInterrupt  # питание пропало между публикацией базы и отметкой
            return real_replace(a, b)
        with mock.patch.object(mig.os, "replace", crash):
            with self.assertRaises(KeyboardInterrupt):
                Fake(False, self.root, units).run()
        self.assertTrue(os.path.exists(self.path(mig.NEW_DB)))
        self.assertFalse(os.path.exists(self.path(mig.MARKER)))

        Fake(False, self.root, units).run()  # повтор не упирается в «уже есть база»
        self.assertTrue(os.path.exists(self.path(mig.MARKER)))
        self.assertFalse(os.path.exists(self.path(mig.PENDING)))
        c = sqlite3.connect(self.path(mig.NEW_DB))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM raw").fetchone()[0], 500)
        c.close()
        Fake(False, self.root, units).run()  # и третий раз — ничего не делает

    def test_published_copy_with_missing_rows_is_not_accepted(self):
        units = self.old_install()
        src = self.make_db()
        self.write(mig.PENDING, {"rows": {"raw": 500}, "source_state": mig.Migration.db_state(src)})
        c = sqlite3.connect(self.path(mig.NEW_DB))
        c.execute("CREATE TABLE raw(host TEXT, v REAL)")
        c.commit()
        c.close()
        with self.assertRaisesRegex(mig.MigrateError, "0 строк"):
            Fake(False, self.root, units).run()
        self.assertFalse(os.path.exists(self.path(mig.MARKER)))

    def test_stale_pending_without_db(self):
        """Прервались до публикации: отметка «в процессе» есть, базы нет — перенос делается заново."""
        units = self.old_install()
        self.make_db()
        self.write(mig.PENDING, {"rows": {}})
        Fake(False, self.root, units).run()
        self.assertTrue(os.path.exists(self.path(mig.MARKER)))
        self.assertFalse(os.path.exists(self.path(mig.PENDING)))


class Restore(Base):
    def test_failure_after_stop_brings_lumen_back(self):
        """Копирование упало, когда старые службы уже остановлены: Luxmon здесь ещё не установлен,
        поэтому Lumen запускается снова, а не оставляет хост без мониторинга."""
        units = self.old_install()
        self.make_db()
        m = Fake(False, self.root, units)

        def broken():
            raise OSError("диск отвалился")
        m.copy_db = broken
        with self.assertRaises(OSError):
            m.run()
        for u in ("lumen-server.service", "lumen-agent.service", "lumen-watchdog.timer"):
            self.assertEqual(m.units[u]["ActiveState"], "active", u)
            self.assertEqual(m.units[u]["UnitFileState"], "enabled", u)
        self.assertEqual(m.units["lumen-watchdog.service"]["UnitFileState"], "static")
        self.assertLess(m.calls.index(("start", "lumen-server.service")), m.calls.index(("start", "lumen-agent.service")))

    def test_restarted_lumen_makes_published_copy_stale(self):
        units = self.old_install()
        src = self.make_db()
        real_replace = os.replace

        def crash(a, b):
            if b.endswith(".migrated-from-lumen"):
                raise OSError("сбой")
            return real_replace(a, b)
        m = Fake(False, self.root, units)
        with mock.patch.object(mig.os, "replace", crash):
            with self.assertRaises(OSError):
                m.run()
        self.assertEqual(m.units["lumen-server.service"]["ActiveState"], "active")
        c = sqlite3.connect(src)  # вернувшийся Lumen пишет в свою базу
        c.execute("INSERT INTO meta VALUES('x','y')")
        c.commit()
        c.close()
        with self.assertRaisesRegex(mig.MigrateError, "устарела"):
            Fake(False, self.root, m.units).run()


class Rerun(Base):
    def test_second_run_does_nothing(self):
        units = self.old_install()
        self.make_db()
        Fake(False, self.root, units).run()
        before = self.tree()
        m = Fake(False, self.root, units)
        m.run()
        self.assertEqual(m.calls, [])
        self.assertEqual(self.tree(), before)

    def test_old_db_changed_after_migration(self):
        """Был откат на Lumen, и он писал в старую базу: повторный перенос не должен молча оставить
        устаревшую базу Luxmon или затереть её."""
        units = self.old_install()
        src = self.make_db()
        Fake(False, self.root, units).run()
        new_before = self.tree()[os.path.relpath(self.path(mig.NEW_DB), self.root)]
        later = os.stat(src).st_mtime + 60
        os.utime(src, (later, later))
        with self.assertRaisesRegex(mig.MigrateError, "менялась после переноса"):
            Fake(False, self.root, units).run()
        self.assertEqual(self.tree()[os.path.relpath(self.path(mig.NEW_DB), self.root)], new_before)

    def test_old_db_changed_within_same_second(self):
        units = self.old_install()
        src = self.make_db()
        Fake(False, self.root, units).run()
        st = os.stat(src)
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))
        with self.assertRaisesRegex(mig.MigrateError, "менялась после переноса"):
            Fake(False, self.root, units).run()

    def test_nothing_to_migrate(self):
        m = Fake(False, self.root)
        m.run()
        self.assertEqual(m.calls, [])
        self.assertEqual(self.tree(), {})


if __name__ == "__main__":
    unittest.main()
