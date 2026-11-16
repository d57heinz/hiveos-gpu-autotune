#!/usr/bin/env python3
"""
monitor.py - a read-only web dashboard for a HiveOS rig running autotune.py.

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

It only reads: nvidia-smi, the miner's local API (OneZeroMiner), the autotune
folders next to this script, the miner log and syslog. It changes nothing on
the rig. There is no login - anyone on your network who can reach the rig can
see the page, so don't forward the port to the internet.
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
CFG = {"api": "http://127.0.0.1:63005"}

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


def read_tuners():
    out = []
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
            "offset_max": cfg.get("offset_max")})
    queue = None
    for d in [ROOT] + tuner_dirs():
        q = load_json(os.path.join(d, "queue.json"))
        if q:
            queue = [{"bus": c["bus"], "name": c["name"], "status": c["status"]} for c in q["cards"]]
            break
    return {"tuners": out, "queue": queue}


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
ExecStart={sys.executable} {os.path.abspath(__file__)} --port {port} --bind {bind} --api {CFG['api']}
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
    a = ap.parse_args()
    CFG["api"] = a.api
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
