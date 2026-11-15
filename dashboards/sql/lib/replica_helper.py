"""Мини-хелпер для ad-hoc запросов к read-only реплике через локальный SSH-туннель.
URL берётся из ANALYTICS_DB_URL, а хост/порт подменяются на туннель: реплика VPC-only."""
import os, psycopg2, psycopg2.extras
from urllib.parse import urlparse

u = urlparse(os.environ["ANALYTICS_DB_URL"])
TUNNEL_HOST = os.getenv("TUNNEL_HOST", "127.0.0.1")
TUNNEL_PORT = int(os.getenv("TUNNEL_PORT", "5433"))


def conn():
    return psycopg2.connect(host=TUNNEL_HOST, port=TUNNEL_PORT, user=u.username, password=u.password,
                            dbname=u.path.lstrip("/"))


def q(sql, p=None):
    c = conn(); cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor); cur.execute(sql, p); r = cur.fetchall(); c.close(); return r
