"""Checkmk REST-API des Masters: Version abfragen, Downtimes setzen/entfernen."""
import datetime
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .common import dim, expand, now

COMMENT = "updatemanager-checkmk"


class CheckmkAPI:
    def __init__(self, master, log, dry_run=False):
        self.m, self.log, self.dry = master, log, dry_run

    @property
    def configured(self):
        return bool(self.m.get("api_url") and self.m.get("api_user"))

    def _secret(self):
        try:
            return open(expand(self.m["api_secret_file"])).read().strip()
        except OSError:
            return ""

    def _req(self, method, path, body=None, timeout=20):
        url = self.m["api_url"].rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.m['api_user']} {self._secret()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        ctx = None
        if url.startswith("https") and not self.m.get("api_verify_tls", True):
            ctx = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            return 0, {"title": str(getattr(e, "reason", e))}
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"title": raw[:200].decode(errors="replace")}
        return status, payload

    @staticmethod
    def _why(st, d):
        return f"HTTP {st}: {d.get('title', '')} {d.get('detail', '')}".strip()

    def version(self):
        """(version, fehler) – z. B. ('2.5.0p12.community', None)."""
        if not self.configured:
            return None, "REST-API nicht konfiguriert"
        st, d = self._req("GET", "/version")
        if st == 200:
            return d.get("versions", {}).get("checkmk"), None
        return None, self._why(st, d)

    def set_downtime(self, host, minutes, comment):
        if not host or not self.configured:
            return False
        if self.dry:
            self.log.info(dim(f"[DRY-RUN] Downtime fuer {host} ({minutes} min): {comment}"))
            return True
        start = now()
        end = start + datetime.timedelta(minutes=minutes)
        st, d = self._req("POST", "/domain-types/downtime/collections/host", {
            "downtime_type": "host",
            "host_name": host,
            "start_time": start.isoformat(timespec="seconds"),
            "end_time": end.isoformat(timespec="seconds"),
            "comment": comment,
        })
        if st in (200, 204):
            self.log.info(f"  Downtime fuer {host} gesetzt ({minutes} min)")
            return True
        self.log.warn(f"  Downtime fuer {host} fehlgeschlagen – {self._why(st, d)}")
        return False

    def remove_downtimes(self, host, prefix=COMMENT):
        """Entfernt nur Downtimes, die von updatemanager-checkmk gesetzt wurden."""
        if not host or not self.configured:
            return 0
        if self.dry:
            self.log.info(dim(f"[DRY-RUN] Downtime fuer {host} entfernen"))
            return 0
        q = urllib.parse.urlencode({"host_name": host})
        st, d = self._req("GET", f"/domain-types/downtime/collections/all?{q}")
        if st != 200:
            self.log.warn(f"  Downtimes von {host} nicht abrufbar ({self._why(st, d)}) – sie laufen von selbst ab")
            return 0
        n = 0
        for v in d.get("value", []):
            ext = v.get("extensions", {})
            if not str(ext.get("comment", "")).startswith(prefix):
                continue
            body = {"delete_type": "by_id", "downtime_id": str(v.get("id"))}
            if ext.get("site_id"):
                body["site_id"] = ext["site_id"]
            st2, d2 = self._req("POST", "/domain-types/downtime/actions/delete/invoke", body)
            if st2 in (200, 204):
                n += 1
            else:
                self.log.warn(f"  Downtime {v.get('id')} nicht entfernt – {self._why(st2, d2)}")
        if n:
            self.log.info(f"  Downtime fuer {host} entfernt")
        return n
