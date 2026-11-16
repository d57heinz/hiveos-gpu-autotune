#!/usr/bin/env python3
"""
monitor.py - a web dashboard for a HiveOS rig running autotune.py.

Shows, on one page that refreshes itself:
  - rig totals (hashrate, power, efficiency) with history charts
  - every GPU: hashrate, power vs limit, temperature, clocks, voltage, shares
  - each tuning run: what it is testing now, results per power limit, and a
    chart of every test (efficiency by offset / clock cap, crashes marked)
  - three live log panes side by side: tuner log, miner output, syslog

Usage (as root on the rig):
  python3 dashboard/monitor.py                 run in this terminal on port 8088
  python3 dashboard/monitor.py --port 9000     another port
  python3 dashboard/monitor.py install         run at every boot (systemd service)
  python3 dashboard/monitor.py uninstall       remove the service
`autotune.py start` installs and starts it for you.

Then open  http://<rig-ip>:8088  from any device on your network.

It reads nvidia-smi, the miner's local API (OneZeroMiner), the autotune folders,
the miner log and syslog. For a card whose tuning has finished, the page can also
(1) turn that card's apply service on or off and (2) apply another tested, stable
setting from its results. It can do nothing else to the rig; --read-only removes
those two controls. There is no login - anyone on your network who can reach the
rig can use the page, so don't forward the port to the internet.
"""
import argparse
import collections
import glob
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))      # .../<clone>/dashboard
ROOT = os.path.dirname(BASE)                           # the clone (holds autotune.py)
PARENT = os.path.dirname(ROOT)                         # where the per-card folders live
UNIT = "/etc/systemd/system/gpu-autotune-monitor.service"
SAMPLE_SECS = 10
HISTORY = collections.deque(maxlen=6 * 360)          # 6 hours of 10 s samples
LATEST = {"gpus": [], "miner": None, "sys": {}, "t": 0}
LOCK = threading.Lock()
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|[\x00-\x08\x0b-\x1f]")
CFG = {"api": "http://127.0.0.1:63005", "controls": True}
UNIT_DIR = "/etc/systemd/system"

os.environ["PATH"] = os.environ.get("PATH", "") + ":/hive/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ""


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ collectors
def read_gpus():
    fields = ("index,name,pci.bus_id,temperature.gpu,fan.speed,power.draw,power.limit,"
              "clocks.sm,clocks.mem,memory.total,utilization.gpu,memory.used")
    out = run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 12:
            continue
        gpus.append({"index": int(p[0]), "name": p[1].replace("NVIDIA ", "").replace("GeForce ", ""),
                     "bus": p[2], "temp": num(p[3]), "fan": num(p[4]), "power": num(p[5]),
                     "pl": num(p[6]), "clock": num(p[7]), "mclock": num(p[8]),
                     "mem": num(p[9]), "util": num(p[10]), "mem_used": num(p[11])})
    return gpus


def read_miner():
    """OneZeroMiner's local API. Returns per-bus stats or None when it isn't reachable."""
    try:
        with urllib.request.urlopen(CFG["api"], timeout=4) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        algo = d["algos"][0]
        devs = {}
        for i, dev in enumerate(d.get("devices", [])):
            def at(key):
                v = algo.get(key)
                return v[i] if isinstance(v, list) and i < len(v) else None
            devs[int(dev.get("bus_id", -1))] = {
                "hr": at("hashrates"), "acc": at("devices_accepted_shares"),
                "rej": at("devices_rejected_shares"), "inv": at("devices_invalid_shares"),
                "volt": dev.get("core_voltage_mv"), "coff": dev.get("coff"),
                "mtemp": dev.get("mem_temp")}
        return {"devs": devs, "uptime": d.get("uptime_seconds"), "total": algo.get("total_hashrate")}
    except Exception:  # noqa: BLE001
        return None


def read_sys():
    s = {"host": socket.gethostname()}
    try:
        s["uptime"] = float(open("/proc/uptime").read().split()[0])
        s["load"] = open("/proc/loadavg").read().split()[:3]
    except OSError:
        pass
    return s


def bus_no(bus):
    try:
        return int(bus.split(":")[1], 16)
    except (IndexError, ValueError):
        return -1


def sampler():
    while True:
        try:
            gpus, miner = read_gpus(), read_miner()
            for g in gpus:
                m = (miner or {}).get("devs", {}).get(bus_no(g["bus"])) or {}
                g.update(hr=m.get("hr"), acc=m.get("acc"), rej=m.get("rej"), inv=m.get("inv"),
                         volt=m.get("volt"), coff=m.get("coff"), mtemp=m.get("mtemp"))
            snap = {"t": time.time(), "gpus": gpus, "sys": read_sys(),
                    "miner": None if miner is None else {"uptime": miner["uptime"]}}
            point = {"t": snap["t"],
                     "hr": sum(g["hr"] or 0 for g in gpus) if miner else None,
                     "pw": sum(g["power"] or 0 for g in gpus),
                     "g": {g["bus"]: [g["hr"], g["power"], g["temp"], g["util"]] for g in gpus}}
            with LOCK:
                LATEST.update(snap)
                HISTORY.append(point)
        except Exception as e:  # noqa: BLE001
            print("sampler error:", e, flush=True)
        time.sleep(SAMPLE_SECS)


def tuner_dirs():
    cands = sorted(set(glob.glob(os.path.join(PARENT, "autotune*")) + [ROOT]))
    return [d for d in cands if os.path.isfile(os.path.join(d, "state.json"))]


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def apply_units():
    """Map tuner folder -> its apply service (by reading which script each unit runs)."""
    found = {}
    for path in glob.glob(os.path.join(UNIT_DIR, "gpu-autotune-apply*.service")):
        try:
            m = re.search(r"ExecStart=\S+ (\S+) apply", open(path).read())
        except OSError:
            continue
        if m:
            found[os.path.dirname(os.path.abspath(m.group(1)))] = os.path.basename(path)
    return found


_APPLY_CACHE = {"t": 0, "v": {}}


def apply_status():
    if time.time() - _APPLY_CACHE["t"] > 8:
        v = {}
        for d, unit in apply_units().items():
            v[d] = {"unit": unit,
                    "enabled": run(["systemctl", "is-enabled", unit]).strip() == "enabled",
                    "active": run(["systemctl", "is-active", unit]).strip() == "active"}
        _APPLY_CACHE.update(t=time.time(), v=v)
    return _APPLY_CACHE["v"]


def read_tuners():
    out = []
    applied = apply_status()
    for d in tuner_dirs():
        st = load_json(os.path.join(d, "state.json"))
        if not st:
            continue
        cfg = st.get("cfg", {})
        levels = cfg.get("pl_levels") or [None]
        keep = ("pl", "phase", "offset", "cap", "stable", "hashrate", "power", "eff", "reason",
                "clock", "temp", "volt")
        out.append({
            "dir": os.path.basename(d), "name": (st.get("gpu_name") or "").replace("NVIDIA ", "")
            .replace("GeForce ", ""), "bus": cfg.get("bus"), "phase": st.get("phase"),
            "started": st.get("started"), "levels_all": levels,
            "pl_now": levels[min(st.get("pl_i", 0), len(levels) - 1)],
            "pl_i": st.get("pl_i", 0), "in_progress": st.get("in_progress"),
            "waiting": st.get("waiting_power_cycle"), "abort": st.get("abort"),
            "warmup": cfg.get("warmup"), "first_warmup": cfg.get("first_warmup"),
            "sample": cfg.get("sample"), "safe_offset": st.get("safe_offset"),
            "results": [{k: r.get(k) for k in keep} for r in st.get("results", [])],
            "levels": st.get("levels", []), "best": load_json(os.path.join(d, "best.json")),
            "offset_max": cfg.get("offset_max"),
            "apply": applied.get(d, {"unit": None, "enabled": False, "active": False}),
            "custom": os.path.exists(os.path.join(d, "best.auto.json"))})
    queue = None
    for d in [ROOT] + tuner_dirs():
        q = load_json(os.path.join(d, "queue.json"))
        if q:
            queue = [{"bus": c["bus"], "name": c["name"], "status": c["status"]} for c in q["cards"]]
            break
    return {"tuners": out, "queue": queue, "controls": CFG["controls"]}


# ------------------------------------------------------------------ controls
class Refused(Exception):
    pass


def tuner_dir(name):
    for d in tuner_dirs():
        if os.path.basename(d) == name:
            return d
    raise Refused("Unknown tuning folder.")


def finished_state(d):
    st = load_json(os.path.join(d, "state.json")) or {}
    bus = st.get("cfg", {}).get("bus")
    for other in tuner_dirs():
        o = load_json(os.path.join(other, "state.json")) or {}
        if o.get("cfg", {}).get("bus") == bus and o.get("phase") not in (None, "done"):
            raise Refused("A tuning run is active on this card. Wait until it finishes, "
                          "or stop it, before changing what is applied.")
    return st


def autotune(d, *args, timeout=90):
    try:
        r = subprocess.run([sys.executable, os.path.join(d, "autotune.py"), *args],
                           capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
    except Exception as e:  # noqa: BLE001
        return False, [str(e)]


def set_apply_service(name, on):
    d = tuner_dir(name)
    finished_state(d)
    if on:
        if not os.path.exists(os.path.join(d, "best.json")):
            raise Refused("This run has no result to apply yet.")
        ok, out = autotune(d, "install-apply")
        if not ok:
            raise Refused("Could not turn it on: " + out[0])
        msg = "On. The settings are applied in about 2 minutes, after every boot, and put back if something resets them."
    else:
        unit = apply_units().get(d)
        if unit:
            run(["systemctl", "disable", "--now", unit])
        msg = ("Off. Nothing re-applies the tuned settings now. The card keeps them until the "
               "miner restarts, HiveOS re-applies its own settings, or the rig reboots.")
    _APPLY_CACHE["t"] = 0
    return msg


def select_setting(name, body):
    d = tuner_dir(name)
    st = finished_state(d)
    best_path, auto_path = os.path.join(d, "best.json"), os.path.join(d, "best.auto.json")
    best = load_json(best_path)
    if not best:
        raise Refused("This run has no result yet.")
    if body.get("restore"):
        if not os.path.exists(auto_path):
            raise Refused("The tuner's own pick is already in use.")
        os.replace(auto_path, best_path)
        what = "the tuner's own pick"
    else:
        want = (body.get("pl"), body.get("offset"), body.get("cap"))
        rows = [r for r in st.get("results", [])
                if (r.get("pl"), r.get("offset"), r.get("cap")) == want]
        if not rows or not all(r.get("stable") for r in rows):
            raise Refused("That setting is not a stable, tested result of this run.")
        r = rows[-1]
        if not os.path.exists(auto_path):
            with open(auto_path, "w") as f:
                json.dump(best, f, indent=2)
        best.update(power_limit_w=r.get("pl"), core_offset=r["offset"], clock_cap_mhz=r.get("cap"),
                    hashrate=r["hashrate"], power_w=r["power"], eff=r["eff"],
                    selected_by="dashboard")
        tmp = best_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(best, f, indent=2)
        os.replace(tmp, best_path)
        what = (f"offset {r['offset']:+d}, " + (f"clock cap {r['cap']} MHz" if r.get("cap")
                                               else "no clock cap"))
    ok, out = autotune(d, "apply")
    unit = apply_units().get(d)
    if unit and run(["systemctl", "is-enabled", unit]).strip() == "enabled":
        run(["systemctl", "restart", unit])
        keep = "It stays applied after reboots."
    else:
        keep = "The apply service is off, so this lasts until the next reboot or settings reset."
    _APPLY_CACHE["t"] = 0
    pl = load_json(best_path).get("power_limit_w")
    note = f" Set the power limit to {pl:g} W in HiveOS to match this result." if pl else ""
    if not ok:
        raise Refused(f"Saved {what}, but applying it failed: {out[0]}")
    return f"Applied {what}. {keep}{note}"


def tail(path, lines):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            text = f.read().decode("utf-8", "replace")
    except OSError as e:
        return f"Cannot read {path}: {e.strerror}"
    return "\n".join(ANSI.sub("", text).splitlines()[-lines:])


def miner_log_path():
    files = glob.glob("/var/log/miner/**/*.log", recursive=True)
    return max(files, key=os.path.getmtime) if files else None


def read_log(src, lines):
    lines = max(20, min(int(lines), 1000))
    if src == "syslog":
        if os.path.exists("/var/log/syslog"):
            return tail("/var/log/syslog", lines)
        return ANSI.sub("", run(["journalctl", "-n", str(lines), "--no-pager", "-o", "short"])) \
            or "No syslog found on this system."
    if src == "miner":
        p = miner_log_path()
        return tail(p, lines) if p else "No miner log found under /var/log/miner."
    if src.startswith("tuner:"):
        name = src[6:]
        for d in tuner_dirs():                                       # only known folders
            if os.path.basename(d) == name:
                return tail(os.path.join(d, "autotune.log"), lines)
    return "Unknown log."


# ------------------------------------------------------------------ http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if (not CFG["controls"] or self.headers.get("X-Rig-Monitor") != "1"
                    or "application/json" not in self.headers.get("Content-Type", "")
                    or (origin and urllib.parse.urlparse(origin).netloc != host)):
                self.send_error(403)
                return
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(n, 4096)) or b"{}")
            try:
                if self.path == "/api/apply-service":
                    msg = set_apply_service(str(body.get("dir")), bool(body.get("on")))
                elif self.path == "/api/select":
                    msg = select_setting(str(body.get("dir")), body)
                else:
                    self.send_error(404)
                    return
                self.send(json.dumps({"ok": True, "message": msg}))
            except Refused as e:
                self.send(json.dumps({"ok": False, "message": str(e)}))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self.send(json.dumps({"ok": False, "message": f"Error: {e}"}))
            except Exception:  # noqa: BLE001
                pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                self.send(PAGE, "text/html")
            elif u.path == "/api/state":
                with LOCK:
                    snap = dict(LATEST)
                    hist = list(HISTORY)
                since = float(q.get("since", ["0"])[0])
                snap["history"] = [p for p in hist if p["t"] > since]
                snap.update(read_tuners())
                self.send(json.dumps(snap))
            elif u.path == "/api/log":
                self.send(read_log(q.get("src", [""])[0], q.get("n", ["300"])[0]), "text/plain")
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self.send_error(500, str(e))
            except Exception:  # noqa: BLE001
                pass


def serve(port, bind):
    threading.Thread(target=sampler, daemon=True).start()
    srv = ThreadingHTTPServer((bind, port), Handler)
    ip = run(["hostname", "-I"]).split()
    print(f"Rig monitor running: http://{ip[0] if ip else 'RIG-IP'}:{port}   (Ctrl+C to stop)",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def install(port, bind):
    if os.geteuid() != 0:
        sys.exit("Run as root.")
    with open(UNIT, "w") as f:
        f.write(f"""[Unit]
Description=GPU autotune rig monitor (web page on port {port})
After=network-online.target

[Service]
ExecStart={sys.executable} {os.path.abspath(__file__)} --port {port} --bind {bind} --api {CFG['api']}{'' if CFG['controls'] else ' --read-only'}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
""")
    for c in (["daemon-reload"], ["enable", "--now", "gpu-autotune-monitor.service"],
              ["restart", "gpu-autotune-monitor.service"]):
        run(["systemctl"] + c)
    ip = run(["hostname", "-I"]).split()
    print(f"Installed. Open http://{ip[0] if ip else 'RIG-IP'}:{port} - it starts at every boot.")


def uninstall():
    run(["systemctl", "disable", "--now", "gpu-autotune-monitor.service"])
    if os.path.exists(UNIT):
        os.remove(UNIT)
    run(["systemctl", "daemon-reload"])
    print("Monitor service removed.")


def main():
    global PAGE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", choices=["run", "install", "uninstall"], default="run")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: all)")
    ap.add_argument("--api", default=CFG["api"], help="miner API (OneZeroMiner)")
    ap.add_argument("--read-only", action="store_true",
                    help="view only: no buttons that change what is applied to a card")
    a = ap.parse_args()
    CFG["api"] = a.api
    CFG["controls"] = not a.read_only
    with open(os.path.join(BASE, "index.html"), encoding="utf-8") as f:
        PAGE = f.read()
    if a.action == "install":
        install(a.port, a.bind)
    elif a.action == "uninstall":
        uninstall()
    else:
        serve(a.port, a.bind)


PAGE = ""

if __name__ == "__main__":
    main()
