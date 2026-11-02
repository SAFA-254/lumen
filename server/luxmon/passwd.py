"""Хеши паролей, команды passwd и role. Без базы: команду запускают от root, а база принадлежит серверу."""
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys

from .config import CONFIG_PATH


def hash_password(pw, salt=None, iters=310000):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), iters)
    return f"pbkdf2_sha256${iters}${salt}${dk.hex()}"


def check_password(pw, stored):
    try:
        _, iters, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(pw, salt, int(iters)), stored)
    except ValueError:
        return False


def cmd_passwd(user):
    if not re.fullmatch(r"[\w.\-]{1,32}", user):
        sys.exit("имя пользователя: буквы, цифры, точка, дефис")
    pw = getpass.getpass(f"Новый пароль для {user}: ")
    if len(pw) < 10:
        sys.exit("пароль должен быть не короче 10 символов")
    if getpass.getpass("Повторите: ") != pw:
        sys.exit("пароли не совпадают")
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault("users", {})[user] = hash_password(pw)
    save_config(cfg)
    print(f"Пароль для {user} сохранён. Перезапуск не нужен.")


def cmd_role(user, role):
    """admin — всё, viewer — только смотреть: без настроек, обслуживания и токенов."""
    if role not in ("admin", "viewer"):
        sys.exit("роль: admin или viewer")
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    if user not in (cfg.get("users") or {}):
        sys.exit(f"пользователя {user} нет: сначала задайте пароль командой passwd")
    roles = cfg.setdefault("roles", {})
    if role == "admin":
        roles.pop(user, None)
    else:
        roles[user] = role
    save_config(cfg)
    print(f"{user}: {'только просмотр' if role == 'viewer' else 'полный доступ'}. Перезапуск не нужен.")


def save_config(cfg):
    tmp = CONFIG_PATH + ".new"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)
    if os.geteuid() == 0:
        st = os.stat(CONFIG_PATH)  # файл остаётся у пользователя, от которого работает сервер
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, CONFIG_PATH)
