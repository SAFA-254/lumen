#!/usr/bin/env python3
"""Перенос прежней установки Lumen на Luxmon на этом хосте. install.sh запускает его на каждом хосте
до установки нового кода; запускать повторно можно — сделанное второй раз не делается.

Использование:
  luxmon-migrate.py                       перенести
  luxmon-migrate.py --dry-run             только показать, что будет сделано, и проверить, что перенос возможен
  luxmon-migrate.py --cleanup [--dry-run] когда откат уже не нужен: удалить юниты и каталоги Lumen
                                          (только если всё перенесено и Luxmon на хосте работает)

Если на хосте есть следы Lumen:
  1. останавливает и отключает lumen-watchdog.timer, lumen-watchdog, lumen-agent, lumen-server и ждёт,
     пока не останется их процессов;
  2. копирует файлы из /etc/lumen в /etc/luxmon (уже существующие не трогает), меняя в них пути
     /etc/lumen/ и /opt/lumen/, путь к базе и имена переменных LUMEN_* в telegram.env;
  3. копирует состояние агента и сторожа (/var/lib/lumen-agent, /var/lib/lumen-watchdog);
  4. если здесь база сервера: копирует её как есть, вместе с -wal и -shm, в резервную копию с датой,
     затем через SQLite backup API создаёт /var/lib/luxmon/luxmon.db и сверяет число строк в каждой таблице.
     Если в /var/lib/luxmon уже есть база, ничего не меняет и завершается с ошибкой. База всегда переезжает
     в /var/lib/luxmon/luxmon.db, даже если в старом server.json был задан свой "db".
Все проверки выполняются до первого изменения. Старые юниты, каталоги и база остаются для отката.
Только стандартная библиотека Python."""
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

# таймер сторожа первым: иначе он успеет сообщить в Telegram, что сервер пропал
UNITS = ("lumen-watchdog.timer", "lumen-watchdog.service", "lumen-agent.service", "lumen-server.service")
OLD_SCRIPTS = ("/opt/lumen/lumen-server.py", "/opt/lumen/lumen-agent.py", "/opt/lumen/lumen-watchdog.py")
PATH_MAP = (("/etc/lumen/", "/etc/luxmon/"), ("/opt/lumen/", "/opt/luxmon/"))
OLD_ETC, NEW_ETC = "/etc/lumen", "/etc/luxmon"
STATE_DIRS = (("/var/lib/lumen-agent", "/var/lib/luxmon-agent"), ("/var/lib/lumen-watchdog", "/var/lib/luxmon-watchdog"))
OLD_DB = "/var/lib/lumen/lumen.db"   # по умолчанию в прежнем сервере; в server.json мог быть задан свой
NEW_DB = "/var/lib/luxmon/luxmon.db"
MARKER = "/var/lib/luxmon/.migrated-from-lumen"
PENDING = MARKER + ".pending"  # база уже на месте, отметки о завершении ещё нет
DB_EXT = ("", "-wal", "-shm")
ACTIVE = ("active", "activating", "deactivating", "reloading")
ENABLED = ("enabled", "enabled-runtime", "linked", "linked-runtime", "alias")
OLD_DIRS = ("/etc/lumen", "/opt/lumen", "/var/lib/lumen") + tuple(o for o, _ in STATE_DIRS)
LUXMON_UNITS = ("luxmon-server.service", "luxmon-agent.service", "luxmon-watchdog.timer")
UNIT_DIR = "/etc/systemd/system"  # сюда юниты Lumen клала его установка
# точные имена из истории репозитория (server/web до переименования), а не расширения: иначе свой report.js
# в web/ удалился бы вместе с Lumen
CODE_DIRS = {"web": re.compile(r"(index|login)\.html"),
             "__pycache__": re.compile(r"(lumen-[\w-]+|tg-notify)\.cpython-\d+\.pyc")}
MANIFEST = NEW_ETC + "/.migrated-from-lumen.json"  # какие файлы перенесены и их sha256 в момент копирования
BACKUP_NAME = re.compile(r"backup-\d{8}-\d{6}")  # каталог резервной копии, который создаёт copy_db


class MigrateError(Exception):
    pass


def new_path(v):
    for old, new in PATH_MAP:
        if v.startswith(old):
            return new + v[len(old):]
    return v


def write_new(path, data, mode):
    """Записать файл, только если его ещё нет: через временный файл и жёсткую ссылку, чтобы не затереть
    файл, появившийся между проверкой и записью, и не оставить наполовину записанный."""
    tmp = path + ".migrate-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    try:
        os.link(tmp, path)
    except FileExistsError:
        raise MigrateError(f"{path} появился во время переноса — оставлен как есть, запустите перенос ещё раз")
    finally:
        os.unlink(tmp)
    fsync_dir(os.path.dirname(path))


def fsync_dir(path):
    """Записать на диск сам каталог: иначе после отключения питания новая ссылка или переименование
    могут не сохраниться, а последующие — сохраниться, и порядок «отметка, база, итог» нарушится."""
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync():
    if hasattr(os, "sync"):
        os.sync()


class Migration:
    STOP_WAIT = 30  # сколько ждать завершения старых процессов, с

    def __init__(self, dry, root=""):
        self.dry = dry
        self.root = root  # для тестов: всё дерево файлов лежит внутри этого каталога

    def p(self, path):
        return self.root + path

    # ---- обращения к системе; в тестах подменяются
    def unit_state(self, unit):
        try:
            out = subprocess.run(["systemctl", "show", unit, "-p", "LoadState", "-p", "ActiveState",
                                  "-p", "UnitFileState", "-p", "FragmentPath"],
                                 stdout=subprocess.PIPE, universal_newlines=True).stdout
        except FileNotFoundError:
            return {}
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def systemctl(self, *args):
        r = subprocess.run(["systemctl", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True)
        if r.returncode:
            raise MigrateError(f"systemctl {' '.join(args)}: {r.stdout.strip()}")

    def old_processes(self):
        found = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv = f.read().decode("utf-8", "replace").split("\0")
            except OSError:
                continue
            if any(a in OLD_SCRIPTS for a in argv):
                found.append(f"{pid} {' '.join(argv).strip()}")
        return found

    # ---- план
    def run(self):
        states = {u: self.unit_state(u) for u in UNITS}
        units = [u for u, s in states.items() if s.get("LoadState") == "loaded" or s.get("ActiveState") in ACTIVE]
        if not units and not any(os.path.exists(self.p(d)) for d in OLD_DIRS + (self.old_db_path(),)):
            print("Lumen на этом хосте нет — переносить нечего")
            return

        # всё, что может не дать перенести, проверяется здесь, до первого изменения
        steps = []
        for u in UNITS:
            s = states[u]
            if s.get("UnitFileState") in ENABLED:
                steps.append((f"отключить {u}", lambda u=u: self.systemctl("disable", u)))
            if s.get("ActiveState") in ACTIVE:
                steps.append((f"остановить {u}", lambda u=u: self.systemctl("stop", u)))
        copies = self.plan_configs() + self.plan_state()
        db = self.plan_db()
        if db:
            copies.append(db)
        if not steps and not copies:
            print("перенос с Lumen уже выполнен, изменений нет")
            self.warn_unmonitored(states)
            return
        # копировать можно только у остановленных: агент пишет позицию журнала, сервер — базу
        steps.append(("проверить, что процессы Lumen завершились и службы отключены", self.wait_stopped))
        steps += copies

        if self.dry:
            for msg, _ in steps:
                print("[dry-run] будет: " + msg, flush=True)
            return
        try:
            for msg, fn in steps:
                print(msg, flush=True)
                fn()
        except BaseException:
            # Luxmon здесь ещё не установлен: без старых служб хост остался бы совсем без мониторинга
            self.restore(states)
            raise
        if not self.dry:
            sync()
            print("Готово. Старые юниты и каталоги (/opt/lumen, /etc/lumen, /var/lib/lumen*) оставлены для отката")

    def warn_unmonitored(self, states):
        """Прошлый запуск мог оборваться так, что откат не успел (kill -9, питание): Lumen уже выключен,
        а Luxmon ещё не поставлен. Сами старые службы не включаем — install.sh сейчас поставит Luxmon,
        и тогда работали бы обе версии сразу, — но говорим, что хост пока без мониторинга."""
        old = [u for u, s in states.items() if s.get("LoadState") == "loaded" and u.endswith(".service")]
        if old and not any(self.unit_state(u).get("ActiveState") in ACTIVE for u in LUXMON_UNITS):
            print("внимание: службы Lumen остановлены, а Luxmon здесь не запущен — хост сейчас без мониторинга. "
                  "Дождитесь установки Luxmon или верните Lumen: systemctl enable --now " + " ".join(old),
                  file=sys.stderr, flush=True)

    def restore(self, states):
        """Вернуть старые службы в то состояние, в котором они были до переноса."""
        for u in reversed(UNITS):  # сервер раньше агента, таймер сторожа последним
            s, now = states[u], self.unit_state(u)
            try:
                if s.get("UnitFileState") in ENABLED and now.get("UnitFileState") not in ENABLED:
                    self.systemctl("enable", u)
                if s.get("ActiveState") in ACTIVE and now.get("ActiveState") not in ACTIVE:
                    self.systemctl("start", u)
            except MigrateError as e:
                print(f"не удалось вернуть {u}: {e}", file=sys.stderr, flush=True)
        print("перенос не закончен, старые службы Lumen снова запущены", file=sys.stderr, flush=True)

    def wait_stopped(self):
        deadline = time.time() + self.STOP_WAIT
        while True:
            bad = [f"{u} ({s.get('ActiveState')}, {s.get('UnitFileState')})"
                   for u, s in ((u, self.unit_state(u)) for u in UNITS)
                   if s.get("ActiveState") in ACTIVE or s.get("UnitFileState") in ENABLED]
            procs = self.old_processes()
            if not bad and not procs:
                return
            if time.time() >= deadline:
                raise MigrateError("старый Lumen не остановлен, переносить данные нельзя: "
                                   + "; ".join(bad + [f"процесс {p}" for p in procs])
                                   + ". Остановите его и запустите перенос снова")
            time.sleep(1)

    def plan_configs(self):
        old, new = self.p(OLD_ETC), self.p(NEW_ETC)
        if not os.path.isdir(old):
            return []
        steps = []
        if not os.path.isdir(new):
            steps.append((f"создать {new} (700)", lambda: os.makedirs(new, 0o700, exist_ok=True)))
        for name in sorted(os.listdir(old)):
            src, dst = os.path.join(old, name), os.path.join(new, name)
            if os.path.lexists(dst) or name.endswith(".migrate-tmp"):
                continue  # уже есть в /etc/luxmon: не трогаем
            if not os.path.isfile(src):
                print(f"пропущен {src}: не обычный файл, перенесите вручную, если нужен", file=sys.stderr)
                continue
            data, notes = self.convert(name, src)
            mode = os.stat(src).st_mode & 0o777
            def copy(src=src, dst=dst, data=data, mode=mode, digest=file_sha(src)):
                write_new(dst, data, mode)
                self.record(src, digest)
            steps.append((f"скопировать {src} → {dst}" + (" (" + "; ".join(notes) + ")" if notes else ""), copy))
        return steps

    def record(self, src, digest):
        """Запомнить, что файл перенесён и каким он был: --cleanup удалит старый, только если он с тех пор
        не менялся. Наличия одноимённого файла в /etc/luxmon мало — он мог быть там до переноса."""
        path = self.p(MANIFEST)
        m = self.manifest()
        m[src[len(self.root):]] = digest
        os.makedirs(os.path.dirname(path), 0o700, exist_ok=True)
        tmp = path + ".migrate-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        fsync_dir(os.path.dirname(path))

    def manifest(self):
        try:
            with open(self.p(MANIFEST), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            return {}
        return m if isinstance(m, dict) else {}

    def convert(self, name, src):
        """Содержимое файла для /etc/luxmon и список изменений. В списке только пути и имена переменных:
        вывод попадает в журнал install.sh, а значения в этих файлах — токены и пароли."""
        with open(src, "rb") as f:
            raw = f.read()
        notes = []
        if name.endswith(".json"):
            cfg = self.read_json(src)

            def walk(v, where):
                if isinstance(v, dict):
                    return {k: walk(x, where + [k]) for k, x in v.items()}
                if isinstance(v, list):
                    return [walk(x, where + [str(i)]) for i, x in enumerate(v)]
                if isinstance(v, str) and new_path(v) != v:
                    notes.append(f"{'.'.join(where)}: {v} → {new_path(v)}")
                    return new_path(v)
                return v
            cfg = walk(cfg, [])
            # новый сервер может писать только в /var/lib/luxmon, база переносится туда (plan_db)
            if name == "server.json" and isinstance(cfg, dict) and "db" in cfg and cfg["db"] != NEW_DB:
                notes.append(f"db: {cfg['db']} → {NEW_DB}")
                cfg["db"] = NEW_DB
            return (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode("utf-8"), notes
        if name.endswith(".env"):
            out = []
            for line in raw.decode("utf-8").splitlines(keepends=True):
                m = re.match(r"(\s*)LUMEN_(\w*)=", line)
                if m:
                    notes.append(f"LUMEN_{m.group(2)} → LUXMON_{m.group(2)}")
                    line = m.group(1) + "LUXMON_" + line[m.end(1) + len("LUMEN_"):]
                for old, new in PATH_MAP:
                    if old in line:
                        notes.append(f"путь {old} → {new}")
                        line = line.replace(old, new)
                out.append(line)
            return "".join(out).encode("utf-8"), notes
        return raw, notes

    def plan_state(self):
        steps = []
        for old, new in STATE_DIRS:
            o, n = self.p(old), self.p(new)
            if not os.path.isdir(o):
                continue
            dmode = os.stat(o).st_mode & 0o777
            for name in sorted(os.listdir(o)):
                src, dst = os.path.join(o, name), os.path.join(n, name)
                if os.path.lexists(dst) or name.endswith((".tmp", ".migrate-tmp")):
                    continue
                if not os.path.isfile(src):
                    print(f"пропущен {src}: не обычный файл, перенесите вручную, если нужен", file=sys.stderr)
                    continue

                def copy(src=src, dst=dst, n=n, dmode=dmode):
                    os.makedirs(n, dmode, exist_ok=True)
                    with open(src, "rb") as f:  # читаем после остановки агента: позиция журнала окончательная
                        data = f.read()
                    write_new(dst, data, os.stat(src).st_mode & 0o777)
                    self.record(src, hashlib.sha256(data).hexdigest())
                steps.append((f"скопировать {src} → {dst}", copy))
        return steps

    # ---- база сервера
    def read_json(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except ValueError as e:
            raise MigrateError(f"{path} — не JSON ({e}), исправьте или перенесите вручную")
        if not isinstance(cfg, dict):
            raise MigrateError(f"{path}: ожидался объект JSON")
        return cfg

    def config_db(self, path, default):
        """Путь к базе из server.json. Не угадываем: при неверном значении перенесли бы не ту базу."""
        if not os.path.exists(path):
            return default
        db = self.read_json(path).get("db", default)
        if not isinstance(db, str) or not db.startswith("/"):
            raise MigrateError(f'{path}: "db" должен быть абсолютным путём, сейчас {db!r} — исправьте и запустите снова')
        return db

    def old_db_path(self):
        return self.p(self.config_db(self.p(OLD_ETC + "/server.json"), OLD_DB))

    @staticmethod
    def db_state(src):
        """Размер и sha256 базы и журнала. По содержимому, а не по времени изменения: время можно вернуть
        прежним (восстановление из копии, rsync -t), и тогда записи, сделанные Lumen после отката, остались бы
        незамеченными. Хеш базы в сотни МБ — секунда-две. -shm не берём — он меняется и при чтении."""
        out = []
        for e in ("", "-wal"):
            if not os.path.exists(src + e):
                continue
            h = hashlib.sha256()
            with open(src + e, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            out.append([e, os.path.getsize(src + e), h.hexdigest()])
        return out

    def plan_db(self):
        src, dst = self.old_db_path(), self.p(NEW_DB)
        if not os.path.exists(src):
            return None
        # если /etc/luxmon/server.json уже есть, перенос его не меняет: сервер будет искать базу там, где сказано в нём
        target = self.config_db(self.p(NEW_ETC + "/server.json"), NEW_DB)
        if target != NEW_DB:
            raise MigrateError(f"в {self.p(NEW_ETC + '/server.json')} db = {target}, а база переносится в {NEW_DB} "
                               f"(сервер Luxmon может писать только в {os.path.dirname(NEW_DB)}). "
                               f'Уберите "db" или укажите {NEW_DB} и запустите снова')
        marker = self.p(MARKER)
        if os.path.exists(marker):
            with open(marker) as f:
                m = json.load(f)
            if self.db_state(src) != m.get("source_state"):
                raise MigrateError(
                    f"база {src} менялась после переноса {m.get('migrated', '')} (был откат на Lumen?). Повторный перенос "
                    f"затёр бы данные, собранные Luxmon, поэтому остановлено. Если нужна именно старая база, "
                    f"отложите базу Luxmon (mv {os.path.dirname(dst)} {os.path.dirname(dst)}.old-ДАТА) и запустите снова")
            return None  # уже перенесена
        existing = [dst + e for e in DB_EXT if os.path.exists(dst + e)]
        if os.path.exists(dst) and os.path.exists(self.p(PENDING)):
            # прошлый запуск положил базу, но не успел записать отметку: это наша копия — проверяем её, а не делаем заново
            return (f"завершить прерванный перенос: проверить {dst} и записать отметку", self.finish_pending)
        if existing:
            raise MigrateError(
                f"в {os.path.dirname(dst)} уже есть база ({', '.join(existing)}), её не затираю. Если Luxmon уже работает "
                f"на ней и старая не нужна — отложите старую (mv {os.path.dirname(src)} {os.path.dirname(src)}.old); "
                f"если нужна старая — отложите новую (mv {os.path.dirname(dst)} {os.path.dirname(dst)}.old) и запустите снова")
        size = sum(os.path.getsize(src + e) for e in DB_EXT if os.path.exists(src + e))
        need = {}  # устройство -> [каталог, байт]: резервная копия и новая база могут быть на одном разделе
        for d in (os.path.dirname(src), self.nearest_dir(os.path.dirname(dst))):
            need.setdefault(os.stat(d).st_dev, [d, 0])[1] += size
        for d, n in need.values():
            free = shutil.disk_usage(d).free
            if free < n + 64 * 1024 * 1024:
                raise MigrateError(f"мало места в {d}: свободно {free >> 20} МБ, для переноса базы нужно {n >> 20} МБ и запас")
        files = ", ".join(os.path.basename(src + e) for e in DB_EXT if os.path.exists(src + e))
        return (f"перенести базу {src} ({size >> 20} МБ): копия как есть ({files}) в "
                f"{os.path.dirname(src)}/backup-<дата>/, затем SQLite backup API → {dst} со сверкой строк", self.copy_db)

    @staticmethod
    def nearest_dir(d):
        while not os.path.isdir(d):
            d = os.path.dirname(d)
        return d

    def copy_db(self):
        src, dst = self.old_db_path(), self.p(NEW_DB)
        backup = os.path.join(os.path.dirname(src), "backup-" + time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(backup, 0o700)
        # резервная копия файлов как есть, до того как SQLite их откроет: при открытии журнал вливается в базу
        for e in DB_EXT:
            if os.path.exists(src + e):
                shutil.copy2(src + e, os.path.join(backup, os.path.basename(src) + e))
        sync()
        print(f"  резервная копия: {backup}", flush=True)

        os.makedirs(os.path.dirname(dst), 0o700, exist_ok=True)
        tmp = dst + ".migrate-tmp"
        for e in ("", "-journal", "-wal", "-shm"):
            if os.path.exists(tmp + e):
                os.unlink(tmp + e)  # осталось от прерванного переноса
        rows = {}
        try:
            s, d = sqlite3.connect(src), sqlite3.connect(tmp)
            try:
                s.backup(d)
                tables = [r[0] for r in s.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                  "AND name NOT LIKE 'sqlite_%'")]
                for t in tables:
                    q = 'SELECT COUNT(*) FROM "' + t.replace('"', '""') + '"'
                    a, b = s.execute(q).fetchone()[0], d.execute(q).fetchone()[0]
                    if a != b:
                        raise MigrateError(f"в копии базы таблица {t}: {b} строк вместо {a}")
                    rows[t] = a
                d.execute("PRAGMA journal_mode=DELETE")  # без -wal рядом; сервер сам включит WAL при запуске
                check = d.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    raise MigrateError(f"копия базы не прошла integrity_check: {check}")
            finally:
                d.close()
                s.close()
            os.chmod(tmp, 0o600)
            # отметка «база опубликована, перенос не закончен» — до публикации: если прервёмся сразу после неё,
            # повторный запуск узнает свою копию и проверит её, а не остановится на «уже есть база»
            info = {"source": src, "backup": backup, "migrated": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "source_state": self.db_state(src), "rows": rows,
                    "backup_files": {f: file_sha(os.path.join(backup, f)) for f in sorted(os.listdir(backup))}}
            pending = self.p(PENDING)
            if os.path.exists(pending):
                os.unlink(pending)  # от запуска, прерванного до публикации базы
            write_new(pending, (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), 0o600)
            with open(tmp, "r+b") as f:  # на запись: в Windows fsync файла, открытого на чтение, не работает
                os.fsync(f.fileno())
            try:
                os.link(tmp, dst)  # не затирает: база могла появиться, пока шло копирование
            except FileExistsError:
                os.unlink(pending)
                raise MigrateError(f"{dst} появилась во время переноса — оставлена как есть, копия удалена")
            fsync_dir(os.path.dirname(dst))
        finally:
            for e in ("", "-journal"):
                if os.path.exists(tmp + e):
                    os.unlink(tmp + e)
        os.replace(pending, self.p(MARKER))
        fsync_dir(os.path.dirname(dst))
        print(f"  база перенесена: {sum(rows.values())} строк в {len(rows)} таблицах", flush=True)

    def finish_pending(self):
        pending, dst = self.p(PENDING), self.p(NEW_DB)
        with open(pending, encoding="utf-8") as f:
            info = json.load(f)
        src = self.old_db_path()
        if self.db_state(src) != info.get("source_state"):
            # после сбоя старый сервер снова запущен и пишет в свою базу: копия устарела
            raise MigrateError(f"{src} менялась после копирования, копия {dst} устарела. Отложите её "
                               f"(mv {dst} {dst}.old; rm {self.p(PENDING)}) и запустите снова")
        d = sqlite3.connect(dst)
        try:
            check = d.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise MigrateError(f"{dst} после прерванного переноса не прошла integrity_check: {check}. "
                                   f"Отложите её (mv {dst} {dst}.bad) и запустите снова — старая база не тронута")
            for t, n in info.get("rows", {}).items():
                q = 'SELECT COUNT(*) FROM "' + t.replace('"', '""') + '"'
                got = d.execute(q).fetchone()[0]
                if got < n:  # больше — можно: сервер мог успеть поработать с базой
                    raise MigrateError(f"{dst}: в таблице {t} {got} строк, а перенесено было {n}. "
                                       f"Отложите её (mv {dst} {dst}.bad) и запустите снова")
        finally:
            d.close()
        os.replace(pending, self.p(MARKER))
        fsync_dir(os.path.dirname(dst))
        print(f"  перенос завершён, резервная копия: {info.get('backup')}", flush=True)

    # ---- очистка, когда откат уже не нужен
    def cleanup(self):
        """Удалить юниты и каталоги Lumen. Необратимо, поэтому сначала проверяется, что удалять нечего
        нужного: Lumen выключен, всё перенесено, старая база после переноса не менялась, Luxmon работает."""
        states = {u: self.unit_state(u) for u in UNITS}
        units = [u for u, s in states.items() if s.get("LoadState") == "loaded"]
        dirs = [self.p(d) for d in OLD_DIRS if os.path.lexists(self.p(d))]
        src = self.old_db_path()  # до удаления /etc/lumen: там мог быть свой путь к базе
        dbfiles = [src + e for e in DB_EXT if os.path.exists(src + e)]
        if not units and not dirs and not dbfiles:
            print("следов Lumen на этом хосте нет — удалять нечего")
            return

        bad = [f"{u} ({s.get('ActiveState')}, {s.get('UnitFileState')})" for u, s in states.items()
               if s.get("ActiveState") in ACTIVE or s.get("UnitFileState") in ENABLED]
        bad += [f"процесс {p}" for p in self.old_processes()]
        if bad:
            raise MigrateError("Lumen ещё включён или работает: " + "; ".join(bad) + ". Сначала перенос (без --cleanup)")
        if not any(self.unit_state(u).get("ActiveState") in ACTIVE for u in LUXMON_UNITS):
            raise MigrateError("Luxmon на этом хосте не запущен — файлы Lumen могут понадобиться для отката. "
                               "Сначала установите Luxmon (install.sh)")
        left = [msg for msg, _ in self.plan_configs() + self.plan_state()]
        if left:
            raise MigrateError("не всё перенесено: " + "; ".join(left) + ". Сначала перенос (без --cleanup)")
        differ = self.not_copied()
        if differ:
            raise MigrateError("эти файлы Lumen не переносились — в Luxmon уже был свой файл с другим содержимым — "
                               "или менялись после переноса: " + ", ".join(differ)
                               + ". Сравните с файлами в /etc/luxmon и /var/lib/luxmon-*, перенесите нужное, "
                               "старые удалите вручную и запустите снова")
        unknown = self.unknown_paths(src)
        if unknown:
            raise MigrateError("в каталогах Lumen есть то, чего перенос не знает, поэтому ничего не удалено: "
                               + ", ".join(unknown[:10]) + (f" и ещё {len(unknown) - 10}" if len(unknown) > 10 else "")
                               + ". Перенесите нужное или удалите вручную и запустите снова")
        backup = backup_files = None
        if dbfiles:
            marker = self.p(MARKER)
            if not os.path.exists(marker):
                raise MigrateError(f"база {src} не перенесена в {NEW_DB}. Сначала перенос (без --cleanup)")
            with open(marker, encoding="utf-8") as f:
                m = json.load(f)
            if os.path.realpath(m.get("source", "")) != os.path.realpath(src):
                raise MigrateError(f"перенесена база {m.get('source')}, а старый сервер настроен на {src} — проверьте вручную")
            if self.db_state(src) != m.get("source_state"):
                raise MigrateError(f"база {src} менялась после переноса {m.get('migrated', '')} (был откат на Lumen?): "
                                   f"в ней могут быть данные, которых нет в Luxmon. Не удаляю")
            backup, backup_files = m.get("backup"), m.get("backup_files")
            # путь берётся из отметки: удаляем по нему, только если это действительно копия рядом со старой базой
            if backup and not (isinstance(backup, str) and BACKUP_NAME.fullmatch(os.path.basename(backup))
                               and os.path.normpath(os.path.dirname(backup)) == os.path.normpath(os.path.dirname(src))
                               and not os.path.islink(backup)):
                raise MigrateError(f"в {marker} резервная копия {backup!r} — не копия переноса рядом с {src}. "
                                   f"Проверьте вручную")

        steps = []
        for u in units:
            frag = states[u].get("FragmentPath") or self.p(f"{UNIT_DIR}/{u}")
            if os.path.normpath(os.path.dirname(frag)) != os.path.normpath(self.p(UNIT_DIR)):
                # юнит не из нашей установки (например, из пакета) — не наше дело его удалять
                print(f"пропущен {u}: файл юнита {frag} не в {UNIT_DIR}, удалите вручную, если нужно", file=sys.stderr)
                continue
            if os.path.lexists(frag):
                steps.append((f"удалить {frag}", lambda path=frag: os.unlink(path)))
            if os.path.lexists(frag + ".d"):
                # дополнения к юниту пишет администратор — возможно, их надо перенести на luxmon-*; не удаляем
                print(f"оставлен {frag}.d: перенесите нужное в юнит Luxmon и удалите вручную", file=sys.stderr)
        if units:
            steps.append(("systemctl daemon-reload", lambda: self.systemctl("daemon-reload")))
        # база с журналами и её резервная копия — даже если «db» указывал вне /var/lib/lumen. Вне каталогов Lumen
        # удаляем только файлы, созданные Lumen и переносом: рядом с базой могут лежать чужие
        if backup and os.path.isdir(backup) and not any(inside(backup, d) for d in dirs):
            # вне каталогов Lumen имени мало: файл удаляется, только если это ровно то, что скопировал перенос
            if not isinstance(backup_files, dict):
                print(f"оставлена {backup}: в отметке переноса нет хешей копии, удалите вручную", file=sys.stderr)
                backup = None
            else:
                for name in sorted(os.listdir(backup)):
                    path = os.path.join(backup, name)
                    if backup_files.get(name) != (file_sha(path) if os.path.isfile(path) and not os.path.islink(path)
                                                  else None):
                        raise MigrateError(f"в резервной копии {backup} файл {name} не тот, что сделал перенос, "
                                           f"поэтому ничего не удалено — проверьте вручную")
                    dbfiles.append(path)
        for path in dbfiles:
            if not any(inside(path, d) for d in dirs):
                steps.append((f"удалить {path}", lambda path=path: os.unlink(path)))
        if backup and os.path.isdir(backup) and not any(inside(backup, d) for d in dirs):
            steps.append((f"удалить {backup}, если в нём больше ничего нет", lambda: rmdir_if_empty(backup)))
        for d in dirs:
            steps.append((f"удалить {d}", lambda d=d: rmpath(d)))

        for msg, fn in steps:
            if self.dry:
                print("[dry-run] будет: " + msg, flush=True)
            else:
                print(msg, flush=True)
                fn()
        if not self.dry:
            sync()
            print("Lumen удалён с этого хоста")

    def not_copied(self):
        """Старые конфиги и файлы состояния, удаление которых может потерять данные: перенос их не копировал
        (в Luxmon уже был свой файл) или они изменились после копирования. Файл, совпадающий с файлом
        Luxmon (для конфигов — после замены путей), тоже считается перенесённым."""
        m, bad = self.manifest(), []
        for old, new in ((OLD_ETC, NEW_ETC),) + STATE_DIRS:
            o, n = self.p(old), self.p(new)
            if not os.path.isdir(o) or os.path.islink(o):
                continue
            for name in sorted(os.listdir(o)):
                src, dst = os.path.join(o, name), os.path.join(n, name)
                if name.endswith((".tmp", ".migrate-tmp")) or not os.path.isfile(src) or os.path.islink(src) \
                        or not os.path.isfile(dst):
                    continue  # это разбирают plan_* и unknown_paths
                if m.get(src[len(self.root):]) == file_sha(src):
                    continue
                with open(dst, "rb") as f:
                    have = f.read()
                if old == OLD_ETC:
                    try:
                        want = self.convert(name, src)[0]
                    except (MigrateError, ValueError):
                        want = None
                else:
                    with open(src, "rb") as f:
                        want = f.read()
                if want != have:
                    bad.append(src)
        return bad

    def unknown_paths(self, src):
        """Всё в каталогах Lumen, что не является его кодом или уже перенесённым файлом. Каталоги удаляются
        целиком, поэтому любой незнакомый файл, подкаталог или ссылка — повод ничего не удалять."""
        def entries(d):
            return sorted(os.listdir(d)) if os.path.isdir(d) and not os.path.islink(d) else []

        def regular(path):
            return os.path.isfile(path) and not os.path.islink(path)
        # каталог-ссылку не разбираем: удалив ссылку, не узнали бы, что было по ней
        bad = [self.p(d) for d in OLD_DIRS if os.path.islink(self.p(d))]
        # конфиги и состояние: только обычные файлы, у которых есть пара в каталоге Luxmon
        for old, new in ((OLD_ETC, NEW_ETC),) + STATE_DIRS:
            o, n = self.p(old), self.p(new)
            for name in entries(o):
                path = os.path.join(o, name)
                if not regular(path) or not (name.endswith((".tmp", ".migrate-tmp"))
                                             or os.path.lexists(os.path.join(n, name))):
                    bad.append(path)
        # код: то, что клала установка Lumen
        o = self.p("/opt/lumen")
        for name in entries(o):
            path = os.path.join(o, name)
            if name in CODE_DIRS and os.path.isdir(path) and not os.path.islink(path):
                bad += [os.path.join(path, f) for f in entries(path)
                        if not regular(os.path.join(path, f)) or not CODE_DIRS[name].fullmatch(f)]
            elif not (regular(path) and (re.fullmatch(r"lumen-[\w-]+\.(py|sh)", name) or name == "tg-notify.py")):
                bad.append(path)
        # данные: проверенная база с журналами и резервные копии переноса из тех же файлов
        v = self.p("/var/lib/lumen")
        known = ({os.path.basename(src) + e for e in DB_EXT}
                 if os.path.normpath(os.path.dirname(src)) == os.path.normpath(v) else set())
        for name in entries(v):
            path = os.path.join(v, name)
            if regular(path) and name in known:
                continue
            if known and BACKUP_NAME.fullmatch(name) and os.path.isdir(path) and not os.path.islink(path):
                bad += [os.path.join(path, f) for f in entries(path)
                        if f not in known or not regular(os.path.join(path, f))]
                continue
            bad.append(path)
        return bad


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inside(path, d):
    path, d = os.path.normpath(path), os.path.normpath(d)
    return os.path.commonpath([path, d]) == d


def rmdir_if_empty(path):
    try:
        os.rmdir(path)
    except OSError:
        print(f"{path} не пуст — оставлен, посмотрите вручную", file=sys.stderr)


def rmpath(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        # через ssh, pct exec и qm guest exec локаль бывает любой, а вывод читается в терминале UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(set(args)) != len(args) or not set(args) <= {"--dry-run", "--cleanup"}:
        print(__doc__, file=sys.stderr)
        return 2
    dry, clean = "--dry-run" in args, "--cleanup" in args
    host = socket.gethostname()
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print(f"{host}: ошибка: нужен root", file=sys.stderr)
        return 1
    try:
        m = Migration(dry=dry)
        m.cleanup() if clean else m.run()
    except MigrateError as e:
        print(f"{host}: ошибка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
