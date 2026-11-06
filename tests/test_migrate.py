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
        if len(args) == 1:  # daemon-reload
            return
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
        self.out, self.err = io.StringIO(), io.StringIO()
        for name in ("stdout", "stderr"):
            p = mock.patch("sys." + name, self.err if name == "stderr" else self.out)
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
        c = sqlite3.connect(src)
        c.execute("INSERT INTO meta VALUES('после','отката')")
        c.commit()
        c.close()
        with self.assertRaisesRegex(mig.MigrateError, "менялась после переноса"):
            Fake(False, self.root, units).run()
        self.assertEqual(self.tree()[os.path.relpath(self.path(mig.NEW_DB), self.root)], new_before)

    def test_content_changed_with_same_size_and_mtime(self):
        """Время изменения можно вернуть прежним (копия из резерва, rsync -t) — сверка идёт по содержимому."""
        units = self.old_install()
        src = self.make_db()
        Fake(False, self.root, units).run()
        st = os.stat(src)
        with open(src, "r+b") as f:
            f.seek(st.st_size - 100)
            b = f.read(1)
            f.seek(st.st_size - 100)
            f.write(bytes([b[0] ^ 1]))
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(os.path.getsize(src), st.st_size)
        with self.assertRaisesRegex(mig.MigrateError, "менялась после переноса"):
            Fake(False, self.root, units).run()

    def test_touch_without_changes_is_not_a_change(self):
        units = self.old_install()
        src = self.make_db()
        Fake(False, self.root, units).run()
        later = os.stat(src).st_mtime + 60
        os.utime(src, (later, later))
        Fake(False, self.root, units).run()

    def test_warns_when_host_left_unmonitored(self):
        """Прошлый запуск оборвался без отката (kill -9): Lumen выключен, Luxmon не стоит — об этом надо сказать."""
        units = self.old_install()
        self.make_db()
        Fake(False, self.root, units).run()
        m = Fake(False, self.root, units)
        m.run()
        self.assertIn("без мониторинга", self.err.getvalue())
        self.assertEqual(m.calls, [])  # старые службы сами не включаются: сейчас install.sh поставит Luxmon

    def test_no_warning_when_luxmon_runs(self):
        units = self.old_install()
        Fake(False, self.root, units).run()
        units["luxmon-agent.service"] = running()
        Fake(False, self.root, units).run()
        self.assertNotIn("без мониторинга", self.err.getvalue())

    def test_nothing_to_migrate(self):
        m = Fake(False, self.root)
        m.run()
        self.assertEqual(m.calls, [])
        self.assertEqual(self.tree(), {})


class Cleanup(Base):
    def migrated(self, db=True):
        """Хост после переноса и установки Luxmon: старые юниты отключены, новый агент работает."""
        units = self.old_install()
        for u in mig.UNITS:
            self.write(mig.UNIT_DIR + "/" + u, "[Unit]")
        for f in ("lumen-server.py", "lumen-agent.py", "tg-notify.py", "web/index.html", "web/login.html", "__pycache__/lumen-agent.cpython-311.pyc"):
            self.write("/opt/lumen/" + f, "код")
        self.write("/etc/lumen/server.json", {"token": "t"})
        if db:
            self.make_db()
        Fake(False, self.root, units).run()
        units["luxmon-agent.service"] = running()
        return units

    def test_removes_lumen_and_keeps_luxmon(self):
        units = self.migrated()
        m = Fake(False, self.root, units)
        m.cleanup()
        for d in mig.OLD_DIRS:
            self.assertFalse(os.path.exists(self.path(d)), d)
        for u in mig.UNITS:
            self.assertFalse(os.path.exists(self.path(mig.UNIT_DIR + "/" + u)), u)
        self.assertIn(("daemon-reload",), m.calls)
        c = sqlite3.connect(self.path(mig.NEW_DB))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM raw").fetchone()[0], 500)
        c.close()
        self.assertTrue(os.path.exists(self.path("/etc/luxmon/server.json")))
        self.assertTrue(os.path.exists(self.path("/var/lib/luxmon-agent/journal.cursor")))
        Fake(False, self.root, {}).cleanup()  # повторно — «удалять нечего»
        Fake(False, self.root, {}).run()      # и перенос после очистки ничего не делает

    def test_dry_run_changes_nothing(self):
        units = self.migrated()
        before = self.tree()
        m = Fake(True, self.root, units)
        m.cleanup()
        self.assertEqual(self.tree(), before)
        self.assertEqual(m.calls, [])
        self.assertIn("[dry-run] будет: удалить", self.out.getvalue())

    def refused(self, units, pattern, procs=()):
        before = self.tree()
        m = Fake(False, self.root, units, procs=procs)
        with self.assertRaisesRegex(mig.MigrateError, pattern):
            m.cleanup()
        self.assertEqual(self.tree(), before)
        self.assertEqual(m.calls, [])

    def test_refuses_while_luxmon_not_running(self):
        units = self.migrated()
        del units["luxmon-agent.service"]
        self.refused(units, "Luxmon на этом хосте не запущен")

    def test_refuses_while_lumen_enabled(self):
        units = self.migrated()
        units["lumen-agent.service"] = running()
        self.refused(units, "Lumen ещё включён")

    def test_refuses_when_old_process_left(self):
        self.refused(self.migrated(), "процесс", procs=["7 python3 /opt/lumen/lumen-agent.py"])

    def test_refuses_when_something_not_migrated(self):
        units = self.migrated()
        self.write("/etc/lumen/extra.json", {"a": 1})
        self.refused(units, "не всё перенесено")

    def test_refuses_when_db_not_migrated(self):
        units = self.migrated(db=False)
        self.make_db()
        self.refused(units, "не перенесена")

    def test_refuses_when_old_db_changed_after_migration(self):
        units = self.migrated()
        c = sqlite3.connect(self.path(mig.OLD_DB))
        c.execute("INSERT INTO meta VALUES('после','отката')")
        c.commit()
        c.close()
        self.refused(units, "менялась после переноса")

    def test_refuses_on_unknown_paths(self):
        """Каталоги удаляются целиком — всё незнакомое в них останавливает удаление."""
        for extra in ("/etc/lumen/certs/ca.pem", "/opt/lumen/notes.txt", "/opt/lumen/web/sub/x.js",
                      "/opt/lumen/web/notes.txt", "/opt/lumen/web/report.js", "/opt/lumen/__pycache__/dump.sql",
                      "/opt/lumen/__pycache__/other.cpython-311.pyc",
                      "/var/lib/lumen/other.db", "/var/lib/lumen/backup-old/lumen.db", "/var/lib/lumen/lumen.db-journal",
                      "/var/lib/lumen/backup-20260101-000000/lumen.db/x", "/var/lib/lumen-agent/sub/x"):
            with self.subTest(extra=extra):
                self.clear_root()
                units = self.migrated()
                self.write(extra, "нужное")
                self.refused(units, "чего перенос не знает")

    def test_refuses_when_marker_backup_points_elsewhere(self):
        """Путь копии берётся из отметки — испорченная отметка не должна направить удаление в чужой каталог."""
        units = self.migrated()
        self.write("/srv/data/lumen.db", "чужая база")
        marker = json.loads(self.read(mig.MARKER))
        marker["backup"] = self.path("/srv/data")
        self.write(mig.MARKER, marker)
        self.refused(units, "не копия переноса")
        self.assertTrue(os.path.exists(self.path("/srv/data/lumen.db")))

    def test_refuses_on_subdir_even_if_luxmon_has_same_name(self):
        units = self.migrated()
        self.write("/var/lib/lumen-agent/custom/data", "нужное")
        os.makedirs(self.path("/var/lib/luxmon-agent/custom"))
        self.refused(units, "custom")

    def test_refuses_when_old_dir_is_symlink(self):
        units = self.migrated()
        os.makedirs(self.path("/srv/state"))
        self.write("/srv/state/data", "нужное")
        try:
            os.symlink(self.path("/srv/state"), self.path("/var/lib/lumen-watchdog-link"))
        except (OSError, NotImplementedError):
            self.skipTest("нет прав на символические ссылки")
        shutil.rmtree(self.path("/var/lib/lumen-watchdog"))
        os.rename(self.path("/var/lib/lumen-watchdog-link"), self.path("/var/lib/lumen-watchdog"))
        self.refused(units, "lumen-watchdog")
        self.assertTrue(os.path.exists(self.path("/srv/state/data")))

    def test_refuses_on_foreign_file_in_backup(self):
        units = self.migrated()
        backup = [d for d in os.listdir(self.path("/var/lib/lumen")) if d.startswith("backup-")][0]
        self.write(f"/var/lib/lumen/{backup}/my.sql", "дамп")
        self.refused(units, "my.sql")

    def clear_root(self):
        for name in os.listdir(self.root):
            shutil.rmtree(os.path.join(self.root, name))

    def test_custom_db_path_removed_with_its_backup(self):
        units = self.old_install()
        self.write("/etc/lumen/server.json", {"token": "t", "db": "/srv/lumen/l.db"})
        self.make_db("/srv/lumen/l.db", rows=3)
        self.write("/srv/lumen/other.txt", "чужой файл")
        Fake(False, self.root, units).run()
        units["luxmon-agent.service"] = running()
        Fake(False, self.root, units).cleanup()
        # рядом с базой вне каталогов Lumen удаляются только её файлы и копия переноса
        self.assertEqual(os.listdir(self.path("/srv/lumen")), ["other.txt"])

    def test_custom_db_backup_with_foreign_or_replaced_file(self):
        """Вне каталогов Lumen файлы копии удаляются, только если их sha256 совпадает с записанным при переносе."""
        for name, data in (("чужой.txt", "x"), ("l.db", "подменили")):
            with self.subTest(name=name):
                self.clear_root()
                units = self.old_install()
                self.write("/etc/lumen/server.json", {"token": "t", "db": "/srv/lumen/l.db"})
                self.make_db("/srv/lumen/l.db", rows=3)
                Fake(False, self.root, units).run()
                units["luxmon-agent.service"] = running()
                backup = [d for d in os.listdir(self.path("/srv/lumen")) if d.startswith("backup-")][0]
                self.write(f"/srv/lumen/{backup}/{name}", data)
                self.refused(units, "не тот, что сделал перенос")

    def test_refuses_when_luxmon_file_existed_before(self):
        """Перенос не трогает уже существующий файл Luxmon — значит, старый не скопирован, и он единственная копия."""
        units = self.old_install()
        self.write("/etc/luxmon/agent.json", {"server": "https://new", "token": "own"})
        self.write("/etc/lumen/server.json", {"token": "t"})
        Fake(False, self.root, units).run()
        units["luxmon-agent.service"] = running()
        self.refused(units, "agent.json")

    def test_luxmon_config_edited_after_migration_is_fine(self):
        units = self.migrated()
        self.write("/etc/luxmon/server.json", {"token": "t", "users": {"admin": "новый пароль"}})
        self.write("/var/lib/luxmon-agent/journal.cursor", "s=новый")  # агент Luxmon двигает позицию журнала
        Fake(False, self.root, units).cleanup()
        self.assertFalse(os.path.exists(self.path("/etc/lumen")))

    def test_refuses_when_old_config_edited_after_migration(self):
        units = self.migrated()
        self.write("/etc/lumen/server.json", {"token": "поменяли после переноса"})
        self.refused(units, "менялись после переноса")

    def test_unit_drop_ins_kept(self):
        units = self.migrated()
        self.write(mig.UNIT_DIR + "/lumen-agent.service.d/override.conf", "[Service]\nEnvironment=X=1\n")
        Fake(False, self.root, units).cleanup()
        self.assertFalse(os.path.exists(self.path(mig.UNIT_DIR + "/lumen-agent.service")))
        self.assertTrue(os.path.exists(self.path(mig.UNIT_DIR + "/lumen-agent.service.d/override.conf")))
        self.assertIn("оставлен", self.err.getvalue())

    def test_foreign_unit_file_kept(self):
        units = self.migrated()
        units["lumen-agent.service"]["FragmentPath"] = self.path("/usr/lib/systemd/system/lumen-agent.service")
        self.write("/usr/lib/systemd/system/lumen-agent.service", "[Unit]")
        Fake(False, self.root, units).cleanup()
        self.assertTrue(os.path.exists(self.path("/usr/lib/systemd/system/lumen-agent.service")))
        self.assertIn("пропущен lumen-agent.service", self.err.getvalue())


class Args(Base):
    def test_bad_arguments(self):
        for args in (["--force"], ["--dry-run", "--dry-run"], ["x", "--cleanup"]):
            self.assertEqual(mig.main(args), 2, args)


if __name__ == "__main__":
    unittest.main()
