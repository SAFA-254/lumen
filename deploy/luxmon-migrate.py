#!/usr/bin/env python3
"""Перенос прежней установки Lumen на Luxmon на этом хосте. install.sh запускает его на каждом хосте
до установки нового кода; запускать повторно можно — сделанное второй раз не делается.

Использование:
  luxmon-migrate.py             перенести
  luxmon-migrate.py --dry-run   только показать, что будет сделано, и проверить, что перенос возможен

Если на хосте есть следы Lumen:
  1. останавливает и отключает lumen-watchdog.timer, lumen-watchdog, lumen-agent, lumen-server и ждёт,
     пока не останется их процессов;
  2. копирует файлы из /etc/lumen в /etc/luxmon (уже существующие не трогает), меняя в них пути
     /etc/lumen/ и /opt/lumen/, путь к базе и имена переменных LUMEN_* в telegram.env;
  3. копирует состояние агента и сторожа (/var/lib/lumen-agent, /var/lib/lumen-watchdog);
  4. если здесь база сервера: копирует её как есть, вместе с -wal и -shm, в резервную копию с датой,
     затем через SQLite backup API создаёт /var/lib/luxmon/luxmon.db и сверяет число строк в каждой таблице.
     Если в /var/lib/luxmon уже есть база, ничего не меняет и завершается с ошибкой.
Все проверки выполняются до первого изменения. Старые юниты, каталоги и база остаются для отката.
Только стандартная библиотека Python."""
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
                                  "-p", "UnitFileState"], stdout=subprocess.PIPE, universal_newlines=True).stdout
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
            return
        # копировать можно только у остановленных: агент пишет позицию журнала, сервер — базу
        steps.append(("проверить, что процессы Lumen завершились и службы отключены", self.wait_stopped))
        steps += copies

        for msg, fn in steps:
            print(("[dry-run] будет: " if self.dry else "") + msg, flush=True)
            if not self.dry:
                fn()
        if not self.dry:
            sync()
            print("Готово. Старые юниты и каталоги (/opt/lumen, /etc/lumen, /var/lib/lumen*) оставлены для отката")

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
            steps.append((f"скопировать {src} → {dst}" + (" (" + "; ".join(notes) + ")" if notes else ""),
                          lambda dst=dst, data=data, mode=mode: write_new(dst, data, mode)))
        return steps

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
                if os.path.lexists(dst) or not os.path.isfile(src) or name.endswith((".tmp", ".migrate-tmp")):
                    continue

                def copy(src=src, dst=dst, n=n, dmode=dmode):
                    os.makedirs(n, dmode, exist_ok=True)
                    with open(src, "rb") as f:  # читаем после остановки агента: позиция журнала окончательная
                        write_new(dst, f.read(), os.stat(src).st_mode & 0o777)
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
    def db_mtime(src):
        # -shm меняется и при чтении, поэтому смотрим только на саму базу и журнал
        return max((os.stat(src + e).st_mtime for e in ("", "-wal") if os.path.exists(src + e)), default=0)

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
            if self.db_mtime(src) > m.get("source_mtime", 0) + 1:
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
                    "source_mtime": self.db_mtime(src), "rows": rows}
            pending = self.p(PENDING)
            if os.path.exists(pending):
                os.unlink(pending)  # от запуска, прерванного до публикации базы
            write_new(pending, (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), 0o600)
            try:
                os.link(tmp, dst)  # не затирает: база могла появиться, пока шло копирование
            except FileExistsError:
                os.unlink(pending)
                raise MigrateError(f"{dst} появилась во время переноса — оставлена как есть, копия удалена")
        finally:
            for e in ("", "-journal"):
                if os.path.exists(tmp + e):
                    os.unlink(tmp + e)
        os.replace(pending, self.p(MARKER))
        print(f"  база перенесена: {sum(rows.values())} строк в {len(rows)} таблицах", flush=True)

    def finish_pending(self):
        pending, dst = self.p(PENDING), self.p(NEW_DB)
        with open(pending, encoding="utf-8") as f:
            info = json.load(f)
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
        print(f"  перенос завершён, резервная копия: {info.get('backup')}", flush=True)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        # через ssh, pct exec и qm guest exec локаль бывает любой, а вывод читается в терминале UTF-8
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args not in ([], ["--dry-run"]):
        print(__doc__, file=sys.stderr)
        return 2
    host = socket.gethostname()
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print(f"{host}: ошибка: нужен root", file=sys.stderr)
        return 1
    try:
        Migration(dry=bool(args)).run()
    except MigrateError as e:
        print(f"{host}: ошибка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
