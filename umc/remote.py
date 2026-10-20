"""SSH zu Master und Satelliten – optional mit Reverse-Tunnel zum Repo."""
from .common import dim, expand, run


class Remote:
    def __init__(self, cfg, log, dry_run=False):
        self.cfg, self.log, self.dry = cfg, log, dry_run

    def ssh_args(self, t, tunnel=False):
        s, r = self.cfg["ssh"], self.cfg["repo"]
        a = ["ssh", "-n", "-i", expand(s["key"]),
             "-o", "BatchMode=yes",
             "-o", f"ConnectTimeout={s['connect_timeout']}",
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
             "-o", "LogLevel=ERROR",
             "-p", str(t.get("ssh_port") or 22)]
        jump = t.get("jumphost") or s.get("jumphost")
        if jump:
            a += ["-J", jump]
        if tunnel:
            # Satellit:127.0.0.1:<tunnel_port>  ->  Update-Server:<listen_host>:<listen_port>
            a += ["-o", "ExitOnForwardFailure=yes",
                  "-R", f"{r['tunnel_port']}:{r['listen_host']}:{r['listen_port']}"]
        a.append(f"{s['user']}@{t['host']}")
        return a

    def run(self, t, cmd, *, tunnel=False, mutating=True, quiet=False, timeout=None):
        """Befehl auf dem Zielsystem ausfuehren (Ausgabe wird mitprotokolliert)."""
        if self.dry and mutating:
            self.log.info(dim(f"[DRY-RUN] {t['name']}{' (Tunnel)' if tunnel else ''}: {cmd}"))
            return 0, ""
        return run(self.ssh_args(t, tunnel) + [cmd], self.log, mutating=False, quiet=quiet,
                   prefix=f"  [{t['name']}] ", timeout=timeout)

    def out(self, t, cmd, tunnel=False, timeout=60):
        """Nur lesender Befehl, Ausgabe wird zurueckgegeben (auch im Dry-Run ausgefuehrt)."""
        rc, o = self.run(t, cmd, tunnel=tunnel, mutating=False, quiet=True, timeout=timeout)
        return rc, o.strip()

    def scp(self, t, files, dest_dir):
        """Dateien per scp in ein Verzeichnis auf dem Zielsystem kopieren."""
        if self.dry:
            self.log.info(dim(f"[DRY-RUN] scp {' '.join(str(f) for f in files)} -> {t['name']}:{dest_dir}/"))
            return 0, ""
        a = self.ssh_args(t)
        target = a[-1]
        opts = [x for x in a[1:-1] if x != "-n"]
        i = opts.index("-p")                      # ssh -p PORT  ->  scp -P PORT
        opts[i] = "-P"
        return run(["scp", "-q", *opts, *[str(f) for f in files], f"{target}:{dest_dir}/"], self.log,
                   mutating=False, quiet=True, timeout=600)

    def reachable(self, t):
        return self.out(t, "true", timeout=30)[0] == 0
