"""Общая подготовка тестов: сервер читает конфигурацию при импорте, поэтому сначала подставляем временную."""
import json
import os
import sys
import tempfile
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
TMP = tempfile.mkdtemp(prefix="luxmon-test-")
CONFIG = os.path.join(TMP, "server.json")
with open(CONFIG, "w") as f:
    json.dump({"token": "test-token", "db": os.path.join(TMP, "db", "luxmon.db")}, f)
os.environ["LUXMON_CONFIG"] = CONFIG
os.environ["LUXMON_TG_ENV"] = os.path.join(TMP, "telegram.env")
sys.path.insert(0, SERVER)

from luxmon import analytics, ingest as ingest_mod  # noqa: E402
from luxmon.alerts import ALERTS  # noqa: E402
from luxmon.store import S  # noqa: E402

START = 1793000000.0  # среда, 12:00 по UTC


def reset():
    """Чистая база и состояние алертов перед каждым тестом. Возвращает список отправленных уведомлений."""
    for table in ("hosts", "raw", "roll", "cluster", "events", "meta", "alert_log", "silence_log", "logins"):
        S.q(f"DELETE FROM {table}")
    ALERTS.active, ALERTS.pending, ALERTS.clearing, ALERTS.silences = {}, {}, {}, []
    ingest_mod.SERIES_LAST.clear()
    ingest_mod.CLOCK_SKEW.clear()
    ingest_mod.SSH_FAIL_SENT.clear()
    analytics.FORECAST.update(ts=0, items=[])
    analytics.ANOMALY.update(ts=0, items=[])
    sent = []
    ALERTS.notify = sent.append
    return sent


class Clock:
    """Управляемое время вместо time.time()."""

    def __init__(self, now=START):
        self.now = now
        self.patch = mock.patch.object(time, "time", lambda: self.now)

    def __enter__(self):
        self.patch.start()
        return self

    def __exit__(self, *a):
        self.patch.stop()

    def advance(self, sec):
        self.now += sec


def payload(host="n1", now=START, **kw):
    p = {"host": host, "ts": int(now), "metrics": {"cpu": 5, "mem": 10}, "info": {"cpus": 2},
         "disks": [{"mount": "/", "used": 10, "size": 100}]}
    p.update(kw)
    return p
