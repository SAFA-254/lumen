"""Команды Telegram-бота."""
import json
import sys
import time

from .config import SILENCE_SCOPES
from .store import S
from .fmt import fmt_dur, fmt_minutes, parse_minutes, scope_label
from .telegram import tg_call, tg_env
from .alerts import ALERTS
from .reports import morning_summary, status_text, weekly_report


HELP = """Команды Luxmon:
/status — состояние кластера
/alerts — активные алерты
/mute <что> <на сколько> [причина] — обслуживание. Что: all, ceph, patroni, checks, backups, ssh или имя хоста. Время: 30m, 2h, 1d
/unmute [all] — снять обслуживание
/summary — сводка за сутки
/week — итоги недели"""


def bot_command(text, who):
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return status_text()
    if cmd == "/summary":
        return morning_summary()
    if cmd == "/week":
        return weekly_report()
    if cmd == "/alerts":
        act = sorted(ALERTS.active.items(), key=lambda kv: kv[1]["since"])
        if not act:
            return "Активных алертов нет"
        now = time.time()
        return "\n".join(f"{'🔕 ' if ALERTS.silenced(k) else ''}{'🔴' if a['level'] == 'crit' else '🟡'} {a['msg']} "
                         f"— {fmt_dur(now - a['since'])}" for k, a in act)
    if cmd == "/mute":
        if len(parts) < 3:
            return "Пример: /mute pve2 1h обновление ядра"
        target, minutes = parts[1], parse_minutes(parts[2])
        hosts = {r[0] for r in S.q("SELECT name FROM hosts")}
        scope = target if target in SILENCE_SCOPES else f"host:{target}" if target in hosts else None
        if not scope:
            return f"Не знаю «{target}». Можно: {', '.join(SILENCE_SCOPES)} или хост: {', '.join(sorted(hosts))}"
        if not minutes or not 1 <= minutes <= 7 * 1440:
            return "Время: например 30m, 2h, 1d (не больше 7 дней)"
        ALERTS.add_silence(scope, minutes, " ".join(parts[3:]), f"telegram:{who}")
        return f"Готово: {scope_label(scope)} на {fmt_minutes(minutes)}"
    if cmd == "/unmute":
        act = [s for s in ALERTS.silences if s["until"] > time.time()]
        if not act:
            return "Обслуживание не включено"
        if len(act) > 1 and (len(parts) < 2 or parts[1] != "all"):
            return "Включено несколько окон:\n" + "\n".join(f"  {s['label'] if 'label' in s else scope_label(s['scope'])}"
                                                            for s in act) + "\nСнять все: /unmute all"
        for s in act:
            ALERTS.remove_silence(s["id"], f"снято из Telegram")
        return "Обслуживание снято"
    return "Не понял команду. /help — список"


def bot_loop():
    """Принимает команды через отдельного бота (LUXMON_BOT_TOKEN) только из разрешённых чатов."""
    offset = None
    while True:
        env = tg_env()
        token = env.get("LUXMON_BOT_TOKEN")
        allowed = {c.strip() for c in env.get("LUXMON_BOT_CHATS", env.get("TELEGRAM_CHAT", "")).split(",") if c.strip()}
        if not token or not allowed:
            time.sleep(60)
            continue
        try:
            params = {"timeout": 30, "allowed_updates": json.dumps(["message"])}
            if offset:
                params["offset"] = offset
            res = tg_call(token, "getUpdates", params, timeout=45)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id"))
                text = msg.get("text") or ""
                if chat not in allowed or not text.startswith("/"):
                    continue
                who = (msg.get("from") or {}).get("username") or chat
                try:
                    reply = bot_command(text, who)
                except Exception as e:
                    reply = f"Ошибка: {e}"
                for i in range(0, len(reply), 4000):
                    tg_call(token, "sendMessage", {"chat_id": chat, "text": reply[i:i + 4000]}, timeout=20)
        except Exception as e:
            print("bot error:", repr(e)[:300], file=sys.stderr, flush=True)
            time.sleep(15)
