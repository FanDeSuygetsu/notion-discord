#!/usr/bin/env python3
"""Envoie un message dans un channel Discord quand une page Notion est créée ou validée.

Usage :
    py notion_discord.py           # tourne en boucle
    py notion_discord.py --test    # envoie la dernière page concernée de chaque base pour tester
    py notion_discord.py --once    # une seule vérification (pour une tâche planifiée)
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
LOOKBACK = timedelta(minutes=3)      # les dates de Notion sont arrondies à la minute
TITLE_GRACE = timedelta(minutes=2)   # laisse le temps de taper le titre avant d'envoyer
SEEN_TTL = timedelta(days=2)

log = logging.getLogger("notion-discord")


# ---------- utilitaires ----------

def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def plain(rich):
    return "".join(t.get("plain_text", "") for t in rich or [])


def normalize_id(raw):
    """Accepte un ID brut ou l'URL complète de la base Notion."""
    raw = raw.split("?")[0]
    m = (re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw)
         or re.search(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])", raw))
    if not m:
        raise ValueError(f"ID de base Notion invalide : {raw}")
    return m.group(0).replace("-", "").lower()


def fmt_date(s):
    try:
        d = parse_ts(s)
    except ValueError:
        return s
    # Timestamp Discord : s'affiche dans le fuseau de chaque lecteur
    return f"<t:{int(d.timestamp())}:{'f' if 'T' in s else 'D'}>"


def file_url(f):
    return (f.get(f["type"]) or {}).get("url")


def prop_value(p):
    t = p["type"]
    v = p.get(t)
    if v is None or v == [] or v == "":
        return None
    if t == "rich_text":
        return plain(v)
    if t in ("select", "status"):
        return v["name"]
    if t == "multi_select":
        return ", ".join(o["name"] for o in v)
    if t == "date":
        start = fmt_date(v["start"])
        return f"{start} → {fmt_date(v['end'])}" if v.get("end") else start
    if t == "people":
        return ", ".join(u.get("name") or "?" for u in v)
    if t == "checkbox":
        return "✅" if v else "❌"
    if t in ("number", "url", "email", "phone_number"):
        return str(v)
    if t == "files":
        return ", ".join(f"[{f.get('name') or 'fichier'}]({file_url(f)})" for f in v)
    if t == "formula":
        inner = v.get(v["type"])
        if isinstance(inner, dict):
            inner = inner.get("start")
        return None if inner in (None, "") else str(inner)
    if t == "relation":
        return f"{len(v)} lien(s)"
    if t in ("created_by", "last_edited_by"):
        return v.get("name")
    return None


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def first_image(props):
    for p in props.values():
        if p["type"] == "files":
            for f in p["files"]:
                url = file_url(f) or ""
                if (f.get("name") or url.split("?")[0]).lower().endswith(IMAGE_EXT):
                    return url
    return None


# ---------- déclencheur "validé" ----------

def trigger_filter(trigger, schema):
    """Transforme {"property": ..., "value": ...} en filtre de l'API Notion.

    - case à cocher : cochée (pas besoin de "value")
    - sélection / état : égal à "value"
    - sélection multiple : contient "value" (ou toutes les valeurs si c'est une liste)
    """
    name = trigger["property"]
    if name not in schema:
        raise ValueError(f"La colonne « {name} » n'existe pas dans la base (colonnes : {', '.join(schema)})")
    ptype = schema[name]["type"]
    value = trigger.get("value")
    if ptype == "checkbox":
        return {"property": name, "checkbox": {"equals": value if isinstance(value, bool) else True}}
    if value is None:
        raise ValueError(f"Il faut une \"value\" pour la colonne « {name} » ({ptype})")
    if ptype in ("select", "status"):
        return {"property": name, ptype: {"equals": value}}
    if ptype == "multi_select":
        values = value if isinstance(value, list) else [value]
        conds = [{"property": name, "multi_select": {"contains": v}} for v in values]
        return conds[0] if len(conds) == 1 else {"and": conds}
    raise ValueError(f"Type de colonne non géré pour le déclencheur : {ptype}")


# ---------- Notion ----------

class Notion:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def _req(self, method, path, **kw):
        for attempt in range(5):
            r = self.s.request(method, NOTION_API + path, timeout=30, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if not r.ok:
                raise RuntimeError(f"Notion {r.status_code} : {r.text[:300]}")
            return r.json()
        raise RuntimeError("Notion : trop de tentatives échouées")

    def database(self, db_id):
        data = self._req("GET", f"/databases/{db_id}")
        return plain(data.get("title")) or "Notion", data["properties"]

    def query(self, db_id, body):
        body = dict(body, page_size=100)
        while True:
            data = self._req("POST", f"/databases/{db_id}/query", json=body)
            yield from data["results"]
            if not data.get("has_more"):
                return
            body["start_cursor"] = data["next_cursor"]

    def latest(self, db_id, filt=None):
        body = {"sorts": [{"timestamp": "last_edited_time", "direction": "descending"}], "page_size": 1}
        if filt:
            body["filter"] = filt
        results = self._req("POST", f"/databases/{db_id}/query", json=body)["results"]
        return results[0] if results else None


# ---------- Discord ----------

def page_title(page):
    for p in page["properties"].values():
        if p["type"] == "title":
            return plain(p["title"]).strip()
    return ""


def shown(page):
    # Les logs GitHub d'un dépôt public sont visibles par tous : on n'y écrit pas les titres
    if os.environ.get("GITHUB_ACTIONS"):
        return "1 page"
    return page_title(page) or "Sans titre"


def build_payload(page, db_name, mapping):
    props = page["properties"]
    wanted = mapping.get("properties")  # liste optionnelle : quelles colonnes afficher, dans quel ordre
    hidden = set(mapping.get("hide_properties", []))
    names = wanted if wanted is not None else [n for n, p in props.items() if p["type"] != "title" and n not in hidden]

    fields = []
    for name in names:
        if name in props and props[name]["type"] != "title":
            val = prop_value(props[name])
            if val:
                fields.append({"name": name[:256], "value": val[:1024], "inline": len(val) <= 40})

    icon = page.get("icon") or {}
    prefix = icon["emoji"] + " " if icon.get("type") == "emoji" else ""
    embed = {
        "title": (prefix + (page_title(page) or "Sans titre"))[:256],
        "url": page["url"],
        "color": int(mapping.get("color", "#5865F2").lstrip("#"), 16),
        "fields": fields[:25],
        "footer": {"text": f"📒 {db_name}"},
        "timestamp": page["last_edited_time"] if mapping.get("trigger") else page["created_time"],
    }
    image = first_image(props)
    if image:
        embed["image"] = {"url": image}
    payload = {"embeds": [embed], "username": mapping.get("username", "Notion")}
    if mapping.get("message"):
        payload["content"] = mapping["message"]
    if mapping.get("avatar_url"):
        payload["avatar_url"] = mapping["avatar_url"]
    return payload


def send_discord(webhook, payload):
    for attempt in range(5):
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)))
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if not r.ok:
            raise RuntimeError(f"Discord {r.status_code} : {r.text[:300]}")
        return
    raise RuntimeError("Discord : trop de tentatives échouées")


# ---------- état ----------

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---------- logique ----------

class Watcher:
    """Surveille une base. Deux modes :
    - sans "trigger" : envoie chaque nouvelle page créée
    - avec "trigger" : envoie une page quand elle devient validée (une seule fois par page)
    """

    def __init__(self, notion, mapping):
        self.notion = notion
        self.m = mapping
        self.db_id = mapping["database_id"]
        title, schema = notion.database(self.db_id)
        self.name = mapping.get("name") or title
        self.filter = trigger_filter(mapping["trigger"], schema) if mapping.get("trigger") else None
        self.key = self.db_id + (":" + json.dumps(mapping["trigger"], sort_keys=True) if self.filter else "")

    def _pages(self, since=None):
        ts = "last_edited_time" if self.filter else "created_time"
        conds = [self.filter] if self.filter else []
        if since:
            conds.append({"timestamp": ts, ts: {"on_or_after": iso(since)}})
        body = {"sorts": [{"timestamp": ts, "direction": "ascending"}]}
        if conds:
            body["filter"] = conds[0] if len(conds) == 1 else {"and": conds}
        return self.notion.query(self.db_id, body)

    def init_state(self, state):
        """Premier lancement : ce qui existe déjà est considéré comme envoyé (pas de spam)."""
        now = datetime.now(timezone.utc)
        pages = self._pages(None if self.filter else now - LOOKBACK)
        state[self.key] = {"since": iso(now), "seen": {p["id"]: p["created_time"] for p in pages}}

    def check(self, st):
        now = datetime.now(timezone.utc)
        for page in self._pages(parse_ts(st["since"]) - LOOKBACK):
            if page["id"] in st["seen"] or page.get("archived") or page.get("in_trash"):
                continue
            if not page_title(page) and now - parse_ts(page["created_time"]) < TITLE_GRACE:
                continue  # page encore vide : on réessaie au prochain tour
            send_discord(self.m["webhook_url"], build_payload(page, self.name, self.m))
            st["seen"][page["id"]] = page["created_time"]
            log.info("[%s] envoyé : %s", self.name, shown(page))
        st["since"] = iso(now)
        if not self.filter:  # en mode validation on garde tout pour ne jamais renvoyer deux fois
            cutoff = now - SEEN_TTL
            st["seen"] = {k: v for k, v in st["seen"].items() if parse_ts(v) > cutoff}

    def test(self):
        page = self.notion.latest(self.db_id, self.filter)
        if not page:
            log.warning("[%s] aucune page concernée, rien à envoyer", self.name)
            return
        send_discord(self.m["webhook_url"], build_payload(page, self.name, self.m))
        log.info("[%s] test envoyé : %s", self.name, shown(page))


def load_config():
    # Sur GitHub, toute la config est dans le secret CONFIG_JSON
    raw = os.environ.get("CONFIG_JSON")
    if not raw:
        if not CONFIG_PATH.exists():
            sys.exit("config.json introuvable : copie config.example.json en config.json et remplis-le.")
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"La config n'est pas un JSON valide (ligne {e.lineno}, colonne {e.colno}) : "
                 "vérifie les guillemets, virgules et accolades.")
    cfg["notion_token"] = os.environ.get("NOTION_TOKEN") or cfg.get("notion_token")
    if not cfg["notion_token"] or "xxx" in cfg["notion_token"]:
        sys.exit("notion_token manquant dans config.json (ou variable d'environnement NOTION_TOKEN).")
    if not cfg.get("databases"):
        sys.exit("Aucune base dans config.json -> 'databases'.")
    # "defaults" s'applique à toutes les bases, chaque base peut le surcharger
    defaults = cfg.get("defaults", {})
    cfg["databases"] = [{**defaults, **m} for m in cfg["databases"]]
    for m in cfg["databases"]:
        if not m.get("webhook_url") or "XXXX" in m["webhook_url"]:
            sys.exit(f"webhook_url manquant pour la base {m.get('name') or m.get('database_id')}")
        m["database_id"] = normalize_id(m["database_id"])
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="envoie la dernière page concernée de chaque base")
    ap.add_argument("--once", action="store_true", help="une seule vérification puis quitte")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config()
    notion = Notion(cfg["notion_token"])

    watchers = []
    for m in cfg["databases"]:
        try:
            w = Watcher(notion, m)
        except Exception as e:  # une base mal configurée ne bloque pas les autres
            log.error("[%s] ignorée : %s", m.get("name") or m["database_id"], e)
            continue
        watchers.append(w)
        what = f"quand « {m['trigger']['property']} » est validé" if w.filter else "à chaque nouvelle page"
        log.info("Base surveillée : %s (%s)", w.name, what)

    if not watchers:
        sys.exit("Aucune base utilisable, voir les erreurs ci-dessus.")

    if args.test:
        for w in watchers:
            try:
                w.test()
            except Exception as e:
                log.error("[%s] %s", w.name, e)
        return

    state = load_state()
    for w in watchers:
        if w.key not in state:
            w.init_state(state)
    save_state(state)

    interval = max(10, int(cfg.get("poll_interval_seconds", 30)))
    log.info("C'est parti, vérification toutes les %ss (Ctrl+C pour arrêter)", interval)
    while True:
        for w in watchers:
            try:
                w.check(state[w.key])
                save_state(state)
            except Exception as e:  # une base en erreur ne bloque pas les autres
                log.error("[%s] %s", w.name, e)
        if args.once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
