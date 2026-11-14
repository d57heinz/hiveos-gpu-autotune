#!/usr/bin/env python3
"""
autotune.py - unattended efficiency tuner for NVIDIA mining GPUs on HiveOS.

Tunes ONE card at a time while the rest of the rig keeps mining. For each power
limit level it runs two phases and records hashrate, power and efficiency:

  Phase 1 (offset):  raises the core clock offset 15 MHz per test (from +0 by
                     default) until a setting crashes twice in a row (one retry
                     rules out flukes) or 3 tests in a row bring no efficiency
                     gain. The most efficient offset, kept 30 MHz below the lowest
                     offset that kept crashing, is carried on.
  Phase 2 (cap):     at that offset, lowers a core clock cap (like
                     `nvidia-smi -lgc 0,<MHz>`) in steps of ~1% of the card's own
                     clock, which pushes the card to a lower voltage. Same stop
                     rules. The most efficient setting that never crashed wins.

  Power limits:      by default the whole search repeats at every power limit from
                     a chosen minimum to maximum in 10 W steps. The miner is
                     STOPPED for every power-limit change (a live change under
                     load caused an Xid 62 on an RTX 5070 Ti). Each new level
                     starts its offset search 2 steps below the previous result.

Each test: 30 s warmup + 180 s sample (120 s warmup for the first test and after
any restart or power-limit change). A test fails on an Xid in the kernel log, the
miner stopping, any HW/invalid share, too many rejects, or no accepted shares.

Recovery: restore safe settings and restart the miner; if that fails, reboot and
resume via a systemd service. Rigs with a CMP card (e.g. 170HX + cmpunlocker)
always get a COLD power cycle instead: `sreboot wakealarm 30` (HiveOS), or with
--power-cycle manual it waits for you to cycle the PSU. After every boot it
checks that CMP cards still show their unlocked memory.

The card is remembered by PCI slot (state.json), so resuming after a reboot always
targets the same card even if GPU numbering changes. It only ever changes the
chosen card's core offset, clock cap and (during a sweep) power limit.

Requires: root, python3, `pip3 install nvidia-ml-py`.

Commands
  probe                     show what the miner API / logs report
  start  [options]          guided setup, then tuning runs in the background
  status                    every test so far + the current level
  report                    best result per power level + every test (summary.txt)
  stop                      stop tuning, restore starting offset and power limit
  apply [--watch]           apply best.json now (--watch keeps re-applying)
  install-apply             apply best.json automatically at every boot
  uninstall                 remove this folder's services
"""
import argparse
import csv
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

# systemd services get a minimal PATH without /hive/bin, where HiveOS keeps `miner`
# and `sreboot`. Without this the background service could not restart the miner,
# pause it for power-limit changes, or power-cycle the rig.
_TOOL_DIRS = ["/hive/bin", "/hive/sbin", "/usr/local/sbin", "/usr/local/bin",
              "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
_path = [d for d in os.environ.get("PATH", "").split(":") if d]
os.environ["PATH"] = ":".join(_path + [d for d in _TOOL_DIRS if d not in _path])

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state.json")
BEST = os.path.join(BASE, "best.json")
CSV_FILE = os.path.join(BASE, "results.csv")
LOG = os.path.join(BASE, "autotune.log")
UNIT_DIR = "/etc/systemd/system"
LEGACY_UNITS = ("gpu-autotune.service", "gpu-autotune-apply.service")


def _slug(bus):
    return bus.split(":", 1)[-1].replace(":", "-").replace(".", "-")     # 05-00-0


def run_unit(bus):
    """One tuning service per card slot, so runs on different cards never clash."""
    return f"gpu-autotune-{_slug(bus)}.service"


def apply_unit(bus):
    return f"gpu-autotune-apply-{_slug(bus)}.service"
SCRIPT = os.path.abspath(__file__)


# ---------------------------------------------------------------- utilities
def log(msg):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return None


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


def sh(cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        log(f"command failed {cmd}: {e}")
        return None


def systemctl(*a):
    if shutil.which("systemctl"):
        sh(["systemctl", *a])


# ---------------------------------------------------------------- GPU (NVML)
class GPU:
    def __init__(self, index=None, bus=None):
        import pynvml as N
        self.N = N
        N.nvmlInit()
        if bus:
            try:
                self.h = N.nvmlDeviceGetHandleByPciBusId(bus)
            except (TypeError, AttributeError):
                self.h = N.nvmlDeviceGetHandleByPciBusId(bus.encode())
        else:
            self.h = N.nvmlDeviceGetHandleByIndex(index)
        self.index = N.nvmlDeviceGetIndex(self.h)
        name = N.nvmlDeviceGetName(self.h)
        self.name = name.decode() if isinstance(name, bytes) else name
        bus = N.nvmlDeviceGetPciInfo(self.h).busId
        bus = bus.decode() if isinstance(bus, bytes) else bus
        self.bus = bus                      # 00000000:03:00.0
        self.short_bus = bus[-7:-2]         # 03:00  (as printed in Xid lines)

    def offset_range(self):
        try:
            lo, hi = self.N.nvmlDeviceGetGpcClkMinMaxVfOffset(self.h)
            return int(lo), int(hi)
        except Exception:  # noqa: BLE001
            return None

    def get_offset(self):
        return int(self.N.nvmlDeviceGetGpcClkVfOffset(self.h))

    def set_offset(self, mhz):
        self.N.nvmlDeviceSetGpcClkVfOffset(self.h, int(mhz))

    def set_cap(self, mhz):
        if mhz is None:
            self.N.nvmlDeviceResetGpuLockedClocks(self.h)
            return
        try:
            self.N.nvmlDeviceSetGpuLockedClocks(self.h, 0, int(mhz))
        except Exception:  # noqa: BLE001
            self.N.nvmlDeviceSetGpuLockedClocks(self.h, 210, int(mhz))

    def set_pl(self, watts):
        self.N.nvmlDeviceSetPowerManagementLimit(self.h, int(watts * 1000))

    def set_mem_lock(self, mhz):
        self.N.nvmlDeviceSetMemoryLockedClocks(self.h, int(mhz), int(mhz))

    def get_pl(self):
        return self.N.nvmlDeviceGetPowerManagementLimit(self.h) / 1000.0

    def pl_range(self):
        """(min, default, max) power limit in W from the card, or None."""
        try:
            lo, hi = self.N.nvmlDeviceGetPowerManagementLimitConstraints(self.h)
        except Exception:  # noqa: BLE001
            return None
        try:
            d = self.N.nvmlDeviceGetPowerManagementDefaultLimit(self.h) / 1000.0
        except Exception:  # noqa: BLE001
            d = None
        return lo / 1000.0, d, hi / 1000.0

    def mem_clock(self):
        return self.N.nvmlDeviceGetClockInfo(self.h, self.N.NVML_CLOCK_MEM)

    def power(self):
        return self.N.nvmlDeviceGetPowerUsage(self.h) / 1000.0

    def clock(self):
        return self.N.nvmlDeviceGetClockInfo(self.h, self.N.NVML_CLOCK_SM)

    def temp(self):
        return self.N.nvmlDeviceGetTemperature(self.h, self.N.NVML_TEMPERATURE_GPU)

    def clock_report(self):
        """Clocks the card itself reports (VBIOS/driver tables) - like
        `nvidia-smi -q -d CLOCK,SUPPORTED_CLOCKS`. Every field is best-effort."""
        N, h, rep = self.N, self.h, {}

        def grab(key, fn):
            try:
                rep[key] = fn()
            except Exception:  # noqa: BLE001
                pass
        G = getattr(N, "NVML_CLOCK_GRAPHICS", 0)
        grab("base_mhz", lambda: N.nvmlDeviceGetDefaultApplicationsClock(h, G))
        grab("boost_mhz", lambda: N.nvmlDeviceGetMaxCustomerBoostClock(h, G))
        grab("max_mhz", lambda: N.nvmlDeviceGetMaxClockInfo(h, N.NVML_CLOCK_SM))
        grab("mem_now_mhz", lambda: self.mem_clock())
        grab("mem_supported", lambda: sorted(N.nvmlDeviceGetSupportedMemoryClocks(h), reverse=True))
        if "mem_now_mhz" in rep:
            mems = rep.get("mem_supported") or [rep["mem_now_mhz"]]
            near = min(mems, key=lambda m: abs(m - rep["mem_now_mhz"]))
            grab("graphics_supported",
                 lambda: sorted(set(N.nvmlDeviceGetSupportedGraphicsClocks(h, near)), reverse=True))
        return rep

    def mem_total_mib(self):
        return int(self.N.nvmlDeviceGetMemoryInfo(self.h).total / 1048576)

    def bus_number(self):
        return int(self.bus.split(":")[1], 16)       # 00000000:05:00.0 -> 5


def list_gpus():
    import pynvml as N
    N.nvmlInit()
    out = []
    for i in range(N.nvmlDeviceGetCount()):
        g = GPU(index=i)
        out.append(g)
    return out


def open_gpu(bus, wait=300):
    """Open the card by PCI slot, so GPU renumbering after a crash can't pick the wrong one."""
    end = time.time() + wait
    while True:
        try:
            return GPU(bus=bus)
        except Exception as e:  # noqa: BLE001
            if time.time() > end:
                raise
            log(f"waiting for NVIDIA driver ({e})")
            time.sleep(15)


def apply_settings(gpu, offset, cap):
    """Only the two tuned values. Power limit and memory lock are left to HiveOS:
    changing them live under load triggered an Xid 62 (PMU halt) on a 5070 Ti."""
    gpu.set_offset(offset)
    time.sleep(2)
    gpu.set_cap(cap)


def enforce(gpu, offset, cap):
    """If HiveOS or the miner reset the offset (e.g. on miner restart), put ours back.
    Only touches the GPU when something actually changed."""
    if gpu.get_offset() != offset:
        apply_settings(gpu, offset, cap)
        return [f"offset->{offset:+d}" + (f", cap->{cap}" if cap else "")]
    if cap and gpu.clock() > cap + 30:          # someone reset the clock cap
        gpu.set_cap(cap)
        return [f"cap->{cap}"]
    return []


def needs_reset(err):
    t = str(err).lower()
    return "requires restart" in t or "reset required" in t or "reset_required" in t


def xid_count(short_bus):
    r = sh(["dmesg"], timeout=15)
    if not r:
        return 0
    return sum(1 for l in r.stdout.splitlines() if "Xid" in l and short_bus in l)


# ---------------------------------------------------------------- miner stats
UNITS = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}
# a per-GPU stats row: index followed by 2+ spaces, then the first "<num> xH/s"
ROW_RE = re.compile(r"(?:^|\s)#?(\d{1,2})\s{2,}\S.*?\s([\d.]+)\s*([KMGTP]?)H/s", re.I)


def fetch_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_path(obj, path):
    cur = obj
    for part in path.split("."):
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    return float(cur)


def flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from flatten(v, f"{prefix}{i}.")
    else:
        yield prefix[:-1], obj


def log_hashrate(path, gpu_id, max_age):
    try:
        if time.time() - os.path.getmtime(path) > max_age:
            return 0.0          # miner stopped writing -> treat as dead
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)   # strip colours
    hr = None
    for line in text.splitlines():
        m = ROW_RE.search(line)
        if m and int(m.group(1)) == gpu_id:
            hr = float(m.group(2)) * UNITS[m.group(3).upper()]
    return hr


class Miner:
    def __init__(self, cfg):
        self.c = cfg

    def _get(self, key):
        data = fetch_json(self.c["api_url"])
        path = self.c[key]
        if "{pos}" in path:                    # find our card by PCI slot each time
            pos = next(i for i, d in enumerate(data["devices"])
                       if d.get("bus_id") == self.c["bus_number"])
            path = path.format(pos=pos)
        return get_path(data, path)

    def hashrate(self):
        try:
            if self.c["source"] == "log":
                return log_hashrate(self.c["log_file"], self.c["miner_gpu"], self.c["dead_secs"])
            return self._get("hr_path")
        except Exception:  # noqa: BLE001
            return None

    def counter(self, key):
        if not self.c.get(key) or self.c["source"] != "api":
            return None
        try:
            return self._get(key)
        except Exception:  # noqa: BLE001
            return None


def wait_hashing(miner, secs):
    end = time.time() + secs
    while time.time() < end:
        hr = miner.hashrate()
        if hr and hr > 0:
            return True
        time.sleep(10)
    return False


# ---------------------------------------------------------------- power limit
def cur_pl(st, cfg=None):
    levels = (cfg or st["cfg"]).get("pl_levels") or [None]
    return levels[min(st.get("pl_i", 0), len(levels) - 1)]


def set_pl_paused(gpu, miner, st, watts):
    """Change the power limit with the miner STOPPED. A live PL change under load
    triggered an Xid 62 (PMU halt) on a 5070 Ti, so never do it while hashing."""
    if not shutil.which("miner"):
        raise RuntimeError("HiveOS `miner` command not found - refusing to change the power "
                           "limit while the miner is running")
    log(f"POWER LIMIT -> {watts:g} W (pausing the miner while it changes)")
    sh(["miner", "stop"], timeout=120)
    time.sleep(10)
    gpu.set_pl(watts)
    time.sleep(3)
    log(f"  power limit is now {gpu.get_pl():g} W")
    sh(["miner", "start"], timeout=120)
    st["applied_pl"] = watts
    st["fresh"] = True
    save_state(st)
    return wait_hashing(miner, 300)


# ---------------------------------------------------------------- one test
def run_test(gpu, miner, cfg, st, offset, cap, pl=None):
    warm = cfg.get("first_warmup", cfg["warmup"]) if st.get("fresh", True) else cfg["warmup"]
    st["in_progress"] = {"phase": st["phase"], "offset": offset, "cap": cap, "pl": pl,
                         "started": time.time()}
    save_state(st)
    label = (f"PL {pl:g} W, " if pl else "") + f"offset {offset:+d}, cap {cap or 'none'}"
    log(f"TEST {label}  ({warm}s warmup + {cfg['sample']}s sample)")

    def fail(reason):
        log(f"  UNSTABLE: {reason}")
        st["fresh"] = True
        return {"phase": st["phase"], "offset": offset, "cap": cap, "pl": pl,
                "stable": False, "reason": reason}

    try:
        apply_settings(gpu, offset, cap)
        log(f"  applied: offset now {gpu.get_offset():+d}, cap {cap or 'none'}")
    except Exception as e:  # noqa: BLE001
        if needs_reset(e):
            log(f"  driver says the GPU needs a reset ({e}) - not a stability result")
            return {"reboot_needed": True}
        return fail(f"could not apply settings: {e}")

    xid0 = xid_count(gpu.short_bus)
    hw0, rej0, acc0 = miner.counter("hw_path"), miner.counter("rej_path"), miner.counter("acc_path")
    samples, dead_since, t0 = [], None, time.time()
    while time.time() - t0 < warm + cfg["sample"]:
        time.sleep(cfg["tick"])
        try:  # HiveOS / the miner may reset clocks when the miner restarts
            fixed = enforce(gpu, offset, cap)
            if fixed:
                log(f"  settings were reset by something else - re-applied {', '.join(fixed)}")
            if pl and abs(gpu.get_pl() - pl) > 1:
                log(f"  power limit was changed to {gpu.get_pl():g} W by something else - "
                    f"re-applying (miner paused) and redoing this test")
                st["in_progress"] = None
                return {"retest": True}
        except Exception as e:  # noqa: BLE001
            return fail(f"GPU stopped responding: {e}")
        if xid_count(gpu.short_bus) > xid0:
            return fail("Xid error in kernel log")
        hw = miner.counter("hw_path")
        if hw0 is not None and hw is not None and hw > hw0:
            return fail(f"hardware/invalid shares: {int(hw - hw0)}")
        rej = miner.counter("rej_path")
        if rej0 is not None and rej is not None and rej - rej0 > cfg["max_rejects"]:
            return fail(f"too many rejected shares: {int(rej - rej0)}")
        hr = miner.hashrate()
        if not hr or hr <= 0:
            dead_since = dead_since or time.time()
            if time.time() - dead_since > cfg["dead_secs"]:
                return fail("miner stopped hashing")
            continue
        dead_since = None
        if time.time() - t0 >= warm:
            samples.append((hr, gpu.power(), gpu.clock(), gpu.temp(), miner.counter("volt_path")))

    acc = miner.counter("acc_path")
    if acc0 is not None and acc is not None and acc <= acc0:
        return fail("no accepted shares during the test")
    if len(samples) < 3:
        return fail("not enough samples")
    n = len(samples)
    hr = sum(s[0] for s in samples) / n
    pw = sum(s[1] for s in samples) / n
    st["fresh"] = False
    res = {"phase": st["phase"], "offset": offset, "cap": cap, "pl": pl, "stable": True,
           "hashrate": hr, "power": round(pw, 1), "eff": hr / pw,
           "clock": round(sum(s[2] for s in samples) / n),
           "temp": round(sum(s[3] for s in samples) / n),
           "volt": (round(sum(s[4] for s in samples) / n) if all(s[4] for s in samples) else None),
           "rejects": None if rej0 is None or miner.counter("rej_path") is None
           else int(miner.counter("rej_path") - rej0)}
    log(f"  OK  {fmt_hr(hr)}  {pw:.1f} W  eff {fmt_eff(hr / pw)}  "
        f"clk {res['clock']} MHz  {res['temp']}C" + (f"  {res['volt']} mV" if res['volt'] else ""))
    return res


def fmt_hr(h):
    for u, d in (("PH/s", 1e15), ("TH/s", 1e12), ("GH/s", 1e9), ("MH/s", 1e6), ("KH/s", 1e3)):
        if h >= d:
            return f"{h / d:.2f} {u}"
    return f"{h:.2f} H/s"


def fmt_eff(e):
    for u, d in (("TH/W", 1e12), ("GH/W", 1e9), ("MH/W", 1e6), ("KH/W", 1e3)):
        if e >= d:
            return f"{e / d:.3f} {u}"
    return f"{e:.3f} H/W"


def record(st, res):
    st["results"].append(res)
    st["in_progress"] = None
    save_state(st)
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "power_limit_w", "phase", "offset", "cap", "stable", "hashrate",
                        "power_w", "eff_per_w", "clock_mhz", "temp_c", "volt_mv", "rejects",
                        "reason"])
        w.writerow([datetime.datetime.now().isoformat(timespec="seconds"), res.get("pl") or "",
                    res["phase"],
                    res["offset"], res["cap"] or "", res["stable"], res.get("hashrate", ""),
                    res.get("power", ""), res.get("eff", ""), res.get("clock", ""),
                    res.get("temp", ""), res.get("volt", ""), res.get("rejects", ""),
                    res.get("reason", "")])


# ---------------------------------------------------------------- search plan
def phase_results(st, phase):
    """Results of a phase at the CURRENT power-limit level."""
    pl = cur_pl(st)
    return [r for r in st["results"] if r["phase"] == phase and r.get("pl") == pl]


def level_start(st, cfg):
    return st.get("level_offset_start", cfg["offset_start"])


def crashed_offsets(st, phase="offset"):
    """Offsets that crashed at least once (never chosen as the final result)."""
    return {r["offset"] for r in phase_results(st, phase) if not r["stable"]}


def confirmed_crash_offsets(st, cfg):
    """Offsets that kept crashing (failed more than crash_retries times)."""
    res = phase_results(st, "offset")
    return {o for o in crashed_offsets(st)
            if sum(1 for r in res if r["offset"] == o and not r["stable"]) > cfg["crash_retries"]}


def crashed_caps(st, off):
    return {r["cap"] for r in phase_results(st, "cap") if r["offset"] == off and not r["stable"]}


def retry_or_give_up(results, key, value, cfg):
    """A setting only counts as 'keeps crashing' after it fails 1 + crash_retries times."""
    fails = sum(1 for r in results if r[key] == value and not r["stable"])
    return fails <= cfg["crash_retries"]


def stalled(stable, cfg):
    """True when the last `patience` stable tests all failed to beat the best efficiency."""
    if len(stable) <= cfg["patience"]:
        return False
    best_i = max(range(len(stable)), key=lambda i: stable[i]["eff"])
    return len(stable) - 1 - best_i >= cfg["patience"]


def end_offset_phase(st, cfg, why):
    stable = [r for r in phase_results(st, "offset") if r["stable"]]
    if not stable:
        return False
    crashes = confirmed_crash_offsets(st, cfg)
    flukes = crashed_offsets(st) - crashes
    clean = [r for r in stable if r["offset"] not in flukes] or stable
    best = max(clean, key=lambda r: r["eff"])
    safe = best["offset"]
    if crashes:   # keep a margin below the lowest offset that kept crashing
        safe = min(safe, min(crashes) - cfg["margin"])
    usable = [r["offset"] for r in clean if r["offset"] <= safe]
    safe = max(usable) if usable else level_start(st, cfg)
    st["safe_offset"] = safe
    st["phase"] = "cap"
    log(f"Offset phase done ({why}). Most efficient offset {best['offset']:+d}"
        + (f", keeps crashing at {min(crashes):+d}" if crashes else "")
        + (f", one-off crash at {sorted(flukes)} ignored" if flukes else "")
        + f" -> using {safe:+d} for the clock-cap phase")
    save_state(st)
    return True


def cap_step_for(st, cfg, clock):
    """Cap step scaled to the card: ~1% of its clock, in the 15 MHz steps clocks move in.
    e.g. 170HX ~1455 MHz -> 15, RTX 3090 ~1750 -> 15, RTX 5070 Ti ~3080 -> 30."""
    if not st.get("cap_step_used"):
        st["cap_step_used"] = cfg["cap_step"] or max(15, int(round(clock * 0.01 / 15)) * 15)
        log(f"Clock-cap step for this card: {st['cap_step_used']} MHz "
            f"(uncapped clock {clock} MHz)")
    return st["cap_step_used"]


def next_params(st, cfg):
    """Return (offset, cap, pl) for the next test, moving through the power-limit
    levels; None when every level is done."""
    levels = cfg.get("pl_levels") or [None]
    while True:
        p = next_params_level(st, cfg)
        if p is not None:
            return p[0], p[1], cur_pl(st, cfg)
        level_abort = st.pop("abort", None)
        best, no_cap = level_best(st, cfg)
        pl = cur_pl(st, cfg)
        st.setdefault("levels", []).append(
            {"pl": pl, "best": best, "no_cap": no_cap, "note": level_abort})
        tag = f" (PL {pl:g} W)" if pl else ""
        if best:
            log(f"LEVEL DONE{tag}: best offset {best['offset']:+d}, cap {best['cap'] or 'none'}"
                f" -> {fmt_hr(best['hashrate'])} @ {best['power']} W = {fmt_eff(best['eff'])}")
        else:
            log(f"LEVEL DONE{tag}: no stable result ({level_abort})")
        save_state(st)
        write_summary(st, cfg)
        if st.get("pl_i", 0) + 1 >= len(levels):
            return None
        prev = st.get("safe_offset")
        st["pl_i"] = st.get("pl_i", 0) + 1
        st["phase"], st["safe_offset"], st["cap_step_used"] = "offset", None, None
        st["level_offset_start"] = (max(cfg["offset_start"], prev - 2 * cfg["offset_step"])
                                    if cfg.get("pl_reuse_offset") and prev is not None
                                    else cfg["offset_start"])
        save_state(st)


def next_params_level(st, cfg):
    """(offset, cap) for the next test at the current power level, or None when done."""
    if st["phase"] == "offset":
        done = phase_results(st, "offset")
        if not done:
            return level_start(st, cfg), None
        last = done[-1]
        if not last["stable"]:
            if retry_or_give_up(done, "offset", last["offset"], cfg):
                log(f"Retrying offset {last['offset']:+d} once to rule out a fluke crash")
                return last["offset"], None
            if not end_offset_phase(st, cfg, f"offset {last['offset']:+d} keeps crashing"):
                st["abort"] = "even the starting offset was unstable"
                return None
            return next_params(st, cfg)
        stable = [r for r in done if r["stable"]]
        if stalled(stable, cfg):
            end_offset_phase(st, cfg, f"no efficiency gain for {cfg['patience']} steps")
            return next_params(st, cfg)
        nxt = last["offset"] + cfg["offset_step"]
        if nxt > cfg["offset_max"]:
            end_offset_phase(st, cfg, "reached the maximum offset")
            return next_params(st, cfg)
        return nxt, None

    # phase == "cap"
    off = st["safe_offset"]
    done = [r for r in phase_results(st, "cap") if r["offset"] == off]
    if not done:
        return off, None                       # uncapped baseline at the chosen offset
    base = next((r for r in done if r["cap"] is None and r["stable"]), None)
    if base is None:
        if retry_or_give_up(done, "cap", None, cfg):
            log(f"Retrying offset {off:+d} uncapped once")
            return off, None
        new = max(r["offset"] for r in phase_results(st, "offset")
                  if r["stable"] and r["offset"] < off) if any(
            r["stable"] and r["offset"] < off for r in phase_results(st, "offset")) else None
        if new is None:
            st["abort"] = "could not find a stable offset"
            return None
        log(f"Offset {off:+d} keeps crashing on longer runs - backing off to {new:+d}")
        st["safe_offset"] = new
        return new, None
    last = done[-1]
    if not last["stable"]:
        if retry_or_give_up(done, "cap", last["cap"], cfg):
            log(f"Retrying cap {last['cap']} once to rule out a fluke crash")
            return off, last["cap"]
        log(f"Cap {last['cap']} keeps crashing - stopping the cap phase")
        return None
    stable = [r for r in done if r["stable"]]
    if cfg["min_hr_frac"] and last["hashrate"] < base["hashrate"] * cfg["min_hr_frac"]:
        log(f"Hashrate fell below {cfg['min_hr_frac']:.0%} of uncapped - stopping")
        return None
    if stalled(stable, cfg):
        log(f"No efficiency gain for {cfg['patience']} caps in a row - stopping")
        return None
    step = cap_step_for(st, cfg, base["clock"])
    if last["cap"] is None:
        top = base["clock"]                    # start just under this card's own clock
        nxt = (top // 15) * 15
        if nxt >= top - step // 2:
            nxt -= step
    else:
        nxt = last["cap"] - step
    clocks = cfg.get("clocks") or {}
    table = clocks.get("graphics_supported")
    if table and max(table) < base["clock"] - 50:
        # e.g. memory locked at 405 by the miner: the driver's table for that memory clock
        # only lists low core clocks, but the miner keeps the core high - ignore the table
        if not st.get("table_ignored"):
            log(f"Driver clock table tops out at {max(table)} MHz but the card runs "
                f"{base['clock']} MHz - ignoring the table, using {step} MHz steps")
            st["table_ignored"] = True
        table = None
    if table:                                  # snap to a clock the card actually supports
        below = [c for c in table if c <= nxt and (last["cap"] is None or c < last["cap"])]
        if not below:
            log("No lower supported clock in the card's table - stopping")
            return None
        nxt = below[0]
    cap_min = cfg["cap_min"]
    if not cap_min:
        b = clocks.get("base_mhz")
        cap_min = b if b and b < base["clock"] else int(base["clock"] * 0.5)
    if nxt < cap_min:
        log(f"Reached the lowest cap to try ({cap_min} MHz"
            f"{', the card base clock' if cap_min == clocks.get('base_mhz') else ''}) - stopping")
        return None
    return off, nxt


# ---------------------------------------------------------------- recovery
def recover(gpu, miner, st, cfg):
    log("Recovering: restoring starting settings and restarting the miner")
    try:
        apply_settings(gpu, cfg["offset_start"], None)
    except Exception as e:  # noqa: BLE001
        log(f"  could not reset GPU ({e})")
    if shutil.which("miner"):
        sh(["miner", "restart"], timeout=120)
    else:
        log("  HiveOS `miner` command not found - cannot restart the miner")
    if wait_hashing(miner, 240):
        log("  miner is hashing again")
        return
    reboot(st, cfg, "miner did not recover")


def cold_power_cycle(secs):
    """What HiveOS `sreboot wakealarm` does: set the RTC wake alarm, power off at once."""
    if shutil.which("sreboot"):
        return sh(["sreboot", "wakealarm", str(secs)], timeout=60)
    rtc = "/sys/class/rtc/rtc0/wakealarm"
    if not os.path.exists(rtc):
        log("  this system has no RTC wake alarm")
        return None
    log("  sreboot not found - setting the RTC wake alarm and powering off directly")
    try:
        sh(["sync"])
        with open(rtc, "w") as f:
            f.write("0")
        with open(rtc, "w") as f:
            f.write(f"+{secs}")
        with open("/proc/sys/kernel/sysrq", "w") as f:
            f.write("1")
        with open("/proc/sysrq-trigger", "w") as f:
            f.write("o")                 # immediate power off
    except OSError as e:
        log(f"  direct power-off failed: {e}")
    return None


def need_power_cycle(st, cfg, why):
    """CMP cards (cmpunlocker) must not be warm-rebooted: cold power cycle only."""
    st["waiting_power_cycle"] = why
    save_state(st)
    try:
        apply_settings(open_gpu(cfg["bus"], wait=30), cfg["offset_start"], None)
    except Exception:  # noqa: BLE001
        pass
    if cfg.get("power_cycle", "manual") == "wakealarm":
        st["power_cycles"] = st.get("power_cycles", 0) + 1
        save_state(st)
        if st["power_cycles"] <= cfg["max_reboots"]:
            log(f"Cold power cycle ({why}): sreboot wakealarm {cfg['wake_secs']} "
                f"[{st['power_cycles']}/{cfg['max_reboots']}] - tuning resumes after boot")
            sh(["sync"])
            r = cold_power_cycle(cfg["wake_secs"])
            time.sleep(180)          # normally the rig is off long before this
            out = ((getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")).strip()
            log(f"Power cycle did not happen{(': ' + out) if out else ''} - "
                f"the wake alarm may not be supported. Falling back to manual.")
            cfg["power_cycle"] = "manual"
            save_state(st)
        else:
            log(f"More than {cfg['max_reboots']} power cycles in a row - waiting for you instead.")
    log("=" * 70)
    log(f"NEEDS A COLD POWER CYCLE: {why}")
    log("Shut down, switch the PSU off for ~10 s, then power on. Tuning resumes by itself.")
    log("=" * 70)
    sys.exit(0)


def reboot(st, cfg, why):
    if cfg.get("cmp") or cfg.get("cmp_cards"):   # any CMP card in the rig needs a cold cycle
        need_power_cycle(st, cfg, why)
    st["reboots"] = st.get("reboots", 0) + 1
    save_state(st)
    if st["reboots"] > cfg["max_reboots"]:
        abort(st, cfg, f"more than {cfg['max_reboots']} reboots in a row")
        sys.exit(1)
    log(f"Rebooting ({why}); tuning resumes after boot "
        f"[reboot {st['reboots']}/{cfg['max_reboots']}]")
    sh(["sync"])
    cmd = ["sreboot"] if shutil.which("sreboot") else ["reboot"]
    sh(cmd, timeout=30)
    time.sleep(900)
    sys.exit(0)


def restore_pl(gpu, st, cfg):
    orig = cfg.get("pl_original")
    if orig and cfg.get("pl_levels") and abs(gpu.get_pl() - orig) > 1:
        try:
            set_pl_paused(gpu, Miner(cfg), st, orig)
        except RuntimeError as e:
            log(f"Could not restore the power limit: {e}. Set {orig:g} W in HiveOS.")


def abort(st, cfg, why):
    log(f"ABORTED: {why}")
    st["abort"] = why
    st["phase"] = "done"
    save_state(st)
    write_summary(st, cfg)
    try:
        gpu = open_gpu(cfg["bus"], wait=60)
        apply_settings(gpu, cfg["offset_start"], None)
        restore_pl(gpu, st, cfg)
    except Exception:  # noqa: BLE001
        pass
    systemctl("disable", run_unit(cfg["bus"]))


# ---------------------------------------------------------------- finish
SUMMARY = os.path.join(BASE, "summary.txt")


def level_best(st, cfg):
    """(best, uncapped) for the current power level, using only settings that never crashed."""
    off = st.get("safe_offset")
    bad_caps = crashed_caps(st, off)
    cap_phase = [r for r in phase_results(st, "cap")
                 if r["stable"] and r["offset"] == off and r["cap"] not in bad_caps]
    if cap_phase:
        no_cap = next((r for r in cap_phase if r["cap"] is None), cap_phase[0])
        floor = no_cap["hashrate"] * (cfg["min_hr_frac"] or 0)
        ok = [r for r in cap_phase if r["hashrate"] >= floor] or [no_cap]
        return max(ok, key=lambda r: r["eff"]), no_cap
    bad = crashed_offsets(st)
    stable = [r for r in phase_results(st, "offset") if r["stable"] and r["offset"] not in bad
              and r["offset"] <= (off if off is not None else 10 ** 6)]
    if stable:
        b = max(stable, key=lambda r: r["eff"])
        return b, b
    return None, None


def _row(r):
    pl = f"{r['pl']:g}" if r.get("pl") else "-"
    if not r["stable"]:
        return (f"{pl:>5} {r['phase']:6} {r['offset']:+6d} {str(r['cap'] or '-'):>5}  "
                f"FAIL: {r.get('reason', '')}")
    return (f"{pl:>5} {r['phase']:6} {r['offset']:+6d} {str(r['cap'] or '-'):>5}  "
            f"{fmt_hr(r['hashrate']):>12} {r['power']:>7}W {fmt_eff(r['eff']):>12} "
            f"{r['clock']:>5} {r['temp']:>4} {str(r.get('volt') or '-'):>4}")


def summary_text(st, cfg):
    out = [f"GPU {st.get('gpu_name')} in slot {cfg.get('bus')} - started {st.get('started')}", ""]
    levels = st.get("levels", [])
    if levels:
        out += ["BEST RESULT AT EACH POWER LIMIT",
                f"{'PL W':>5} {'offset':>6} {'cap':>5} {'hashrate':>12} {'power':>8} {'eff':>12}"
                f"   | uncapped: {'hashrate':>12} {'power':>8} {'eff':>12}"]
        for lv in levels:
            b, n = lv["best"], lv["no_cap"]
            pl = f"{lv['pl']:g}" if lv["pl"] else "-"
            if not b:
                out.append(f"{pl:>5}  no stable result ({lv.get('note')})")
                continue
            out.append(f"{pl:>5} {b['offset']:+6d} {str(b['cap'] or '-'):>5} "
                       f"{fmt_hr(b['hashrate']):>12} {b['power']:>7}W {fmt_eff(b['eff']):>12}"
                       f"   |           {fmt_hr(n['hashrate']):>12} {n['power']:>7}W "
                       f"{fmt_eff(n['eff']):>12}")
        out.append("")
    out += ["EVERY TEST",
            f"{'PL W':>5} {'phase':6} {'offset':>6} {'cap':>5}  {'hashrate':>12} {'power':>8} "
            f"{'eff':>12} {'clk':>5} {'temp':>4} {'mV':>4}"]
    out += [_row(r) for r in st["results"]]
    return "\n".join(out) + "\n"


def write_summary(st, cfg):
    try:
        with open(SUMMARY, "w") as f:
            f.write(summary_text(st, cfg))
    except OSError as e:
        log(f"could not write {SUMMARY}: {e}")


def finish(st, cfg, gpu=None, miner=None):
    st["phase"] = "done"
    save_state(st)
    write_summary(st, cfg)
    lv = [x for x in st.get("levels", []) if x["best"]]
    if not lv:
        abort(st, cfg, "no stable result to report")
        return
    floor = (cfg["min_hr_frac"] or 0) * max(x["no_cap"]["hashrate"] for x in lv)
    best = max([x["best"] for x in lv if x["best"]["hashrate"] >= floor]
               or [x["best"] for x in lv], key=lambda r: r["eff"])
    no_cap = max([x["no_cap"] for x in lv if x["no_cap"]["hashrate"] >= floor]
                 or [x["no_cap"] for x in lv], key=lambda r: r["eff"])

    out = {"gpu_bus": cfg["bus"], "gpu_index": cfg["gpu"], "gpu_name": st.get("gpu_name"),
           "power_limit_w": best.get("pl"), "core_offset": best["offset"],
           "clock_cap_mhz": best["cap"],
           "hashrate": best["hashrate"], "power_w": best["power"], "eff": best["eff"],
           "hiveos_only": {"power_limit_w": no_cap.get("pl"), "core_offset": no_cap["offset"],
                           "hashrate": no_cap["hashrate"], "power_w": no_cap["power"],
                           "eff": no_cap["eff"]}}
    with open(BEST, "w") as f:
        json.dump(out, f, indent=2)

    gpu = gpu or open_gpu(cfg["bus"], wait=60)
    miner = miner or Miner(cfg)
    if best.get("pl") and abs(gpu.get_pl() - best["pl"]) > 1:
        try:
            set_pl_paused(gpu, miner, st, best["pl"])
        except RuntimeError as e:
            log(f"Could not set the power limit: {e}. Set {best['pl']:g} W in HiveOS.")
    apply_settings(gpu, best["offset"], best["cap"])
    systemctl("disable", run_unit(cfg["bus"]))

    def desc(r):
        return ((f"PL {r['pl']:g} W, " if r.get("pl") else "") + f"core offset {r['offset']:+d}"
                + (f", clock cap {r['cap']} MHz" if r["cap"] else "")
                + f"  ->  {fmt_hr(r['hashrate'])} @ {r['power']} W = {fmt_eff(r['eff'])}")
    log("=" * 70)
    log("FINISHED")
    for line in summary_text(st, cfg).split("EVERY TEST")[0].splitlines():
        if line.strip():
            log("  " + line)
    log(f"Best overall:           {desc(best)}")
    log(f"Best without a cap:     {desc(no_cap)}  (all settable in HiveOS)")
    if best["cap"]:
        log("HiveOS can't combine an offset and a clock cap in one field. Set the PL and "
            "offset in HiveOS and run `autotune.py install-apply` to add the cap at boot, "
            f"or use the miner's own options (OneZeroMiner: --coff {best['offset']} "
            f"--cclk {best['cap']}).")
    log(f"Saved to {BEST}; full table in {SUMMARY}. Best settings are applied now.")
    log("=" * 70)


# ---------------------------------------------------------------- commands
def cmd_run(args):
    if getattr(args, "delay", 0):
        time.sleep(args.delay)      # let HiveOS start the miner and apply its OC first
    st = load_state()
    if not st or st.get("phase") == "done":
        log("Nothing to do (no active run).")
        if st:
            systemctl("disable", run_unit(st["cfg"]["bus"]))
        return
    cfg = st["cfg"]
    gpu = open_gpu(cfg["bus"])
    miner = Miner(cfg)
    log(f"Resuming on {gpu.name} at {gpu.bus} (GPU {gpu.index})")
    if st.get("waiting_power_cycle"):
        log(f"Back after power cycle (was: {st['waiting_power_cycle']})")
        st["waiting_power_cycle"] = None
        save_state(st)
    lost = []
    for bus, mem in (cfg.get("cmp_cards") or {}).items():
        try:
            now = GPU(bus=bus).mem_total_mib()
        except Exception:  # noqa: BLE001
            now = 0
        if now < mem * 0.9:
            lost.append(f"CMP card at {bus} shows {now} MiB instead of {mem} MiB")
    if lost and not (cfg.get("mem_total") and gpu.mem_total_mib() < cfg["mem_total"] * 0.9):
        if st.get("in_progress"):
            ip = st["in_progress"]
            record(st, {"phase": ip["phase"], "offset": ip["offset"], "cap": ip["cap"],
                        "pl": ip.get("pl"), "stable": False,
                        "reason": "crash/reboot during test"})
        need_power_cycle(st, cfg, "; ".join(lost) + " - unlock not active")
    if cfg.get("mem_total") and gpu.mem_total_mib() < cfg["mem_total"] * 0.9:
        if st.get("in_progress"):
            ip = st["in_progress"]
            record(st, {"phase": ip["phase"], "offset": ip["offset"], "cap": ip["cap"],
                        "pl": ip.get("pl"), "stable": False,
                        "reason": "crash/reboot during test"})
        need_power_cycle(st, cfg, f"card shows {gpu.mem_total_mib()} MiB instead of "
                                  f"{cfg['mem_total']} MiB - unlock not active")

    if st.get("in_progress"):
        ip = st["in_progress"]
        log(f"Resumed after an interruption during offset {ip['offset']:+d}, "
            f"cap {ip['cap'] or 'none'} - counting that test as unstable")
        record(st, {"phase": ip["phase"], "offset": ip["offset"], "cap": ip["cap"],
                    "pl": ip.get("pl"), "stable": False, "reason": "crash/reboot during test"})
    st["fresh"], st["applied_pl"] = True, None      # long warmup + re-apply PL after any (re)start
    save_state(st)

    try:
        apply_settings(gpu, cfg["offset_start"], None)
    except Exception as e:  # noqa: BLE001
        if needs_reset(e):
            reboot(st, cfg, f"driver says the GPU needs a reset ({e})")
        log(f"could not apply starting settings: {e}")
    log("Waiting for the miner to start hashing...")
    if not wait_hashing(miner, 300):
        reboot(st, cfg, "miner never started hashing")

    while True:
        params = next_params(st, cfg)
        save_state(st)
        if params is None:
            finish(st, cfg, gpu, miner)
            return
        off, cap, pl = params
        if pl and st.get("applied_pl") != pl:
            try:
                apply_settings(gpu, level_start(st, cfg), None)   # safe clocks while switching
                ok = set_pl_paused(gpu, miner, st, pl)
            except RuntimeError as e:
                abort(st, cfg, str(e))
                return
            except Exception as e:  # noqa: BLE001
                if needs_reset(e):
                    reboot(st, cfg, f"driver says the GPU needs a reset ({e})")
                raise
            if not ok:
                recover(gpu, miner, st, cfg)
                st["applied_pl"] = None
                continue
        res = run_test(gpu, miner, cfg, st, off, cap, pl)
        if res.get("retest"):
            st["applied_pl"] = None
            save_state(st)
            continue
        if res.get("reboot_needed"):
            st["in_progress"] = None
            save_state(st)
            reboot(st, cfg, "driver says the GPU needs a reset")
        record(st, res)
        if res["stable"]:
            st["reboots"] = 0
            st["power_cycles"] = 0
            save_state(st)
        else:
            recover(gpu, miner, st, cfg)
            st["applied_pl"] = None          # make sure our PL is back after the restart
            save_state(st)
            try:
                gpu = open_gpu(cfg["bus"], wait=60)
            except Exception:  # noqa: BLE001
                reboot(st, cfg, "GPU not available after miner restart")


def ask(prompt, default=None, cast=str, valid=None):
    """Prompt on the terminal; Enter accepts the default."""
    while True:
        tail = f" [{default}]" if default is not None else ""
        raw = input(f"{prompt}{tail}: ").strip()
        if not raw and default is not None:
            raw = str(default)
        try:
            val = cast(raw)
        except ValueError:
            print("  please enter a valid value")
            continue
        if valid and val not in valid:
            print(f"  choose one of: {', '.join(map(str, valid))}")
            continue
        return val


def yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{d}]: ").strip().lower()
    return default if not raw else raw.startswith("y")


def auto_map_api(api_url, gpu):
    """OneZeroMiner: find this card in the API by PCI bus number and build its paths."""
    try:
        data = fetch_json(api_url)
    except Exception:  # noqa: BLE001
        return None
    devs = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devs, list):
        return None
    for pos, d in enumerate(devs):
        if isinstance(d, dict) and d.get("bus_id") == gpu.bus_number():
            # {pos} is re-resolved by PCI slot on every read, so the paths stay right
            # even if another card drops out and the miner renumbers its devices
            paths = {"hr_path": "algos.0.hashrates.{pos}",
                     "acc_path": "algos.0.devices_accepted_shares.{pos}",
                     "rej_path": "algos.0.devices_rejected_shares.{pos}",
                     "hw_path": "algos.0.devices_invalid_shares.{pos}",
                     "volt_path": "devices.{pos}.core_voltage_mv"}
            for k in list(paths):          # drop any field this miner doesn't have
                try:
                    get_path(data, paths[k].format(pos=pos))
                except Exception:  # noqa: BLE001
                    if k == "hr_path":
                        return None
                    paths[k] = None
            return paths
    return None


def cmd_start(a):
    if os.geteuid() != 0:
        sys.exit("Run as root.")
    st = load_state()
    if st and st.get("phase") not in (None, "done"):
        sys.exit("A run is already active. Use `status`, or `stop` first.")
    interactive = sys.stdin.isatty()

    # ---- 1. which GPU
    if a.bus:
        gpu = GPU(bus=a.bus)
    elif a.gpu is not None:
        gpu = GPU(index=a.gpu)
    elif interactive:
        gpus = list_gpus()
        print("\nGPUs in this rig:")
        for g in gpus:
            print(f"  [{g.index}]  {g.name:32s} slot {g.bus}  {g.mem_total_mib():6d} MiB  "
                  f"offset {g.get_offset():+d}")
        idx = ask("Which GPU do you want to tune? (number in brackets)",
                  cast=int, valid=[g.index for g in gpus])
        gpu = GPU(index=idx)
    else:
        sys.exit("Use --gpu N or --bus 0000xxxx:yy:00.0 (or run in a terminal for prompts).")
    log(f"Selected GPU {gpu.index}: {gpu.name} at {gpu.bus}")

    # one folder per card: this folder's best.json may be another card's saved result
    # (and its apply service reads it from here) - a new run would rename it away
    if os.path.exists(BEST):
        try:
            other = json.load(open(BEST)).get("gpu_bus")
        except (OSError, ValueError):
            other = None
        if other and other != gpu.bus:
            sys.exit(f"This folder holds the tuning result for the card at {other} "
                     f"({BEST}), which its apply service may be using. Tune {gpu.bus} from "
                     f"its own folder, e.g.:\n  mkdir -p {os.path.dirname(BASE)}/autotune-"
                     f"{_slug(gpu.bus)} && cp {SCRIPT} {os.path.dirname(BASE)}/autotune-"
                     f"{_slug(gpu.bus)}/")

    # an apply service from an earlier run would keep re-applying old settings and
    # fight this run - stop it (re-install it with `install-apply` when this run ends)
    au = apply_unit(gpu.bus)
    r = sh(["systemctl", "is-enabled", au], timeout=15) if shutil.which("systemctl") else None
    if r and (r.stdout or "").strip() in ("enabled", "static", "indirect"):
        log(f"{au} is re-applying earlier settings to this card.")
        if interactive and not yes("Stop and disable it for this run?"):
            sys.exit("Cancelled - the apply service would fight the tuning run.")
        systemctl("stop", au)
        systemctl("disable", au)
        log(f"Stopped {au}. Run `install-apply` again after this run to keep the new result.")

    cmp = "CMP" in gpu.name.upper()
    try:
        cmp_cards = {g.bus: g.mem_total_mib() for g in list_gpus()
                     if "CMP" in g.name.upper() and g.bus != gpu.bus}
    except Exception:  # noqa: BLE001
        cmp_cards = {}
    if cmp_cards:
        log("This rig also has CMP card(s) at " + ", ".join(cmp_cards) + ": any reboot "
            "will be a COLD power cycle, and their unlocked memory is checked after every boot.")
    if cmp:
        how = (f"`sreboot wakealarm {a.wake_secs}` (cold power cycle)"
               if a.power_cycle == "wakealarm" else "stopping and asking you to power-cycle it")
        log(f"CMP card: no warm reboots. If a crash needs one it recovers by {how}. "
            f"Unlocked memory: {gpu.mem_total_mib()} MiB (checked after every boot).")
        if interactive and not a.force and not yes("Continue with this CMP card?", False):
            sys.exit("Cancelled.")

    rng = gpu.offset_range()
    if rng:
        log(f"Driver allows core offsets {rng[0]:+d} .. {rng[1]:+d}")
    clocks = gpu.clock_report()
    gs = clocks.get("graphics_supported") or []
    log("Clock reference from the card: "
        + ", ".join(f"{k.replace('_mhz', '')} {v} MHz" for k, v in clocks.items()
                    if k.endswith("_mhz"))
        + (f"; {len(gs)} supported core clocks {gs[-1]}..{gs[0]} MHz at this memory clock"
           if gs else "; supported clock table not available"))

    # ---- power limit range
    plr = gpu.pl_range()
    try:
        pl_now = gpu.get_pl()
    except Exception:  # noqa: BLE001
        pl_now, plr = None, None
    levels = None
    if plr:
        log(f"Power limit: min {plr[0]:g} W, default {plr[1] if plr[1] else '?'} W, "
            f"max {plr[2]:g} W, currently {pl_now:g} W")
        if not a.no_pl_sweep:
            lo = a.pl_min if a.pl_min else plr[0]
            hi = a.pl_max if a.pl_max else plr[2]
            lo, hi = max(lo, plr[0]), min(hi, plr[2])
            levels = list(range(int(round(lo)), int(hi) + 1, a.pl_step))
            if levels and levels[-1] < hi - 0.5:
                levels.append(int(hi))
            if interactive:
                lo = ask("Lowest power limit to test (W)", int(round(lo)), int)
                hi = ask("Highest power limit to test (W)", int(hi), int)
                levels = list(range(lo, hi + 1, a.pl_step))
                if levels and levels[-1] < hi:
                    levels.append(hi)
            log(f"Power-limit sweep: {len(levels)} levels, {levels[0]}..{levels[-1]} W in "
                f"{a.pl_step} W steps (the miner is paused for each change)")
    else:
        log("This card does not report a power-limit range - tuning at the current limit only")

    if levels and not shutil.which("miner"):
        sys.exit("A power-limit sweep needs HiveOS's `miner` command to pause the miner, and it "
                 "was not found. Use --no-pl-sweep, or run this on HiveOS.")

    # ---- 2. where to read this card's hashrate
    if not a.hr_path and not a.log_file:
        paths = auto_map_api(a.api_url, gpu)
        if paths:
            log("Found this card in the miner API (matched by PCI slot; {pos} = its "
                "position, looked up by slot on every read):")
            for k, v in paths.items():
                log(f"    {k:9s} {v}")
            if interactive and not yes("Use these?"):
                paths = None
        if paths:
            a.source = "api"
            for k, v in paths.items():
                setattr(a, k, v)
        else:
            logs = sorted(glob.glob("/var/log/miner/**/*.log", recursive=True),
                          key=lambda x: -os.path.getmtime(x))
            if not logs:
                sys.exit("Could not find the miner API or a miner log. Run `probe` and pass "
                         "--hr-path or --log-file.")
            a.source, a.log_file = "log", logs[0]
            default_id = gpu.index
            if interactive:
                a.log_file = ask("Miner log file", logs[0])
                a.miner_gpu = ask("GPU number as the MINER shows it (e.g. #3 in SRBMiner)",
                                  default_id, int)
            elif a.miner_gpu is None:
                a.miner_gpu = default_id
            log(f"Reading hashrate from {a.log_file}, miner GPU #{a.miner_gpu} "
                f"(no share/HW checks in log mode - Xid and hashrate checks still apply)")
    elif a.log_file:
        a.source = "log"
    if a.miner_gpu is None:
        a.miner_gpu = gpu.index

    a.bus_number = gpu.bus_number()
    miner = Miner(vars(a))
    hr = miner.hashrate()
    if not hr:
        sys.exit("Could not read this card's hashrate - is the miner running? Check `probe`.")
    log(f"Current hashrate reading for this card: {fmt_hr(hr)} - OK")

    # ---- 3. offsets
    if a.offset_start is None:
        a.offset_start = ask("Starting core offset (0 = stock, no clock cap)", 0, int) \
            if interactive else 0
    if interactive and a.min_hr_frac == 0.0:
        pct = ask("Lowest hashrate you'd accept, as % of the uncapped result "
                  "(0 = no limit, pure max efficiency)", 0, int)
        a.min_hr_frac = pct / 100.0
    offset_max = a.offset_max if a.offset_max is not None else (rng[1] if rng else 1000)
    if rng:
        offset_max = min(offset_max, rng[1])

    cfg = {k: getattr(a, k) for k in (
        "source", "api_url", "hr_path", "acc_path", "rej_path", "hw_path", "volt_path",
        "log_file", "miner_gpu", "offset_start", "offset_step",
        "margin", "cap_step", "cap_min", "min_hr_frac", "warmup", "sample", "tick",
        "dead_secs", "max_reboots", "max_rejects", "bus_number", "power_cycle", "wake_secs",
        "patience", "crash_retries")}
    cfg.update(pl_levels=levels, pl_original=pl_now, pl_reuse_offset=not a.pl_full_offset_search,
               first_warmup=a.first_warmup)
    cfg.update(cmp_cards=cmp_cards)
    cfg.update(offset_max=offset_max, bus=gpu.bus, gpu=gpu.index, cmp=cmp, clocks=clocks,
               mem_total=gpu.mem_total_mib() if cmp else None)

    mins = (a.warmup + a.sample) / 60
    if levels:
        hours = len(levels) * 14 * mins / 60
        log(f"Rough time: ~{hours:.0f} h ({len(levels)} power levels x ~14 tests x "
            f"{mins:.1f} min; each new level starts its offset search 2 steps below the "
            f"previous level's result)" if not a.pl_full_offset_search else
            f"Rough time: {len(levels)} levels, each with a full offset search from "
            f"{a.offset_start:+d} - expect a long run")
    log(f"Plan: start at offset {a.offset_start:+d} with no clock cap, raise the offset "
        f"{a.offset_step} MHz per test until a setting crashes twice or {a.patience} tests "
        f"bring no efficiency gain; then lower the clock cap ~1% of the card's clock per test with "
        f"the same rules. ~{mins:.0f} min per test (an offset sweep of ~300 MHz is "
        f"~{300 // a.offset_step} tests).")
    if interactive and not yes("Start tuning now?"):
        sys.exit("Cancelled.")

    for f in (CSV_FILE, BEST):
        if os.path.exists(f):
            os.replace(f, f + ".old")
    st = {"cfg": cfg, "phase": "offset", "results": [], "in_progress": None,
          "reboots": 0, "gpu_name": gpu.name,
          "started": datetime.datetime.now().isoformat(timespec="seconds")}
    save_state(st)
    log(f"Saved run settings (card slot {gpu.bus}) to {STATE}")

    if a.foreground:
        a.delay = 0
        cmd_run(a)
        return
    unit = run_unit(gpu.bus)
    install_unit(os.path.join(UNIT_DIR, unit), f"GPU autotune for {gpu.bus} (resumes after reboot)",
                 f"{sys.executable} {SCRIPT} run", "simple", delay=90)
    systemctl("enable", unit)
    systemctl("start", "--no-block", unit)
    log(f"Service: {unit}")
    log(f"Started in the background. Follow it with:  tail -f {LOG}")


def cmd_status(_a):
    st = load_state()
    if not st:
        print("No run found.")
        return
    print(f"GPU: {st.get('gpu_name')} in slot {st['cfg'].get('bus', '?')}   phase: {st['phase']}   "
          f"safe offset: {st.get('safe_offset', '-')}   reboots: {st.get('reboots', 0)}")
    ck = st["cfg"].get("clocks") or {}
    if ck:
        print("Card clocks: " + ", ".join(f"{k.replace('_mhz', '')} {v}" for k, v in ck.items()
                                          if k.endswith("_mhz"))
              + (f", cap step {st['cap_step_used']} MHz" if st.get("cap_step_used") else ""))
    if st.get("abort"):
        print(f"ABORTED: {st['abort']}")
    if st.get("waiting_power_cycle"):
        print(f"WAITING FOR A COLD POWER CYCLE: {st['waiting_power_cycle']}")
    if st["cfg"].get("pl_levels"):
        print(f"Power levels: {st['cfg']['pl_levels'][0]}..{st['cfg']['pl_levels'][-1]} W, "
              f"now at {cur_pl(st):g} W (level {st.get('pl_i', 0) + 1}/"
              f"{len(st['cfg']['pl_levels'])}). Full table: {SUMMARY}")
    print(f"{'PL W':>5} {'phase':6} {'offset':>6} {'cap':>5}  {'hashrate':>12} {'power':>8} "
          f"{'eff':>12} {'clk':>5} {'temp':>4} {'mV':>4}")
    for r in st["results"]:
        print(_row(r))
    if st.get("in_progress"):
        ip = st["in_progress"]
        print(f"running now: offset {ip['offset']:+d}, cap {ip['cap'] or 'none'}")
    if os.path.exists(BEST):
        print(f"\nbest.json:\n{open(BEST).read()}")


def cmd_report(_a):
    st = load_state()
    if not st:
        sys.exit("No run found.")
    write_summary(st, st["cfg"])
    print(summary_text(st, st["cfg"]), end="")
    print(f"(saved to {SUMMARY})")


def cmd_stop(_a):
    st = load_state()
    if st:
        systemctl("stop", run_unit(st["cfg"]["bus"]))
        systemctl("disable", run_unit(st["cfg"]["bus"]))
        st["phase"] = "done"
        st["in_progress"] = None
        st.setdefault("abort", "stopped by user")
        save_state(st)
        try:
            gpu = GPU(bus=st["cfg"]["bus"])
            apply_settings(gpu, st["cfg"]["offset_start"], None)
            restore_pl(gpu, st, st["cfg"])
            write_summary(st, st["cfg"])
            log(f"Stopped. Restored offset {st['cfg']['offset_start']:+d}, no clock cap"
                + (f", power limit {st['cfg']['pl_original']:g} W"
                   if st["cfg"].get("pl_levels") else "") + ".")
        except Exception as e:  # noqa: BLE001
            log(f"Stopped, but could not reset the GPU: {e}")


def cmd_apply(a):
    if getattr(a, "delay", 0):
        time.sleep(a.delay)
    if not os.path.exists(BEST):
        sys.exit("No best.json yet.")
    b = json.load(open(BEST))
    gpu = open_gpu(b["gpu_bus"])
    apply_settings(gpu, b["core_offset"], b["clock_cap_mhz"])
    log(f"Applied offset {b['core_offset']:+d}, cap {b['clock_cap_mhz'] or 'none'}")
    while a.watch:   # miners/HiveOS can re-apply their own OC on restart
        time.sleep(30)
        try:
            fixed = enforce(gpu, b["core_offset"], b["clock_cap_mhz"])
            if fixed:
                log(f"settings were changed by something else - re-applied {', '.join(fixed)}")
        except Exception as e:  # noqa: BLE001
            log(f"apply watch error: {e}")
            time.sleep(30)
            try:
                gpu = open_gpu(b["gpu_bus"], wait=120)
            except Exception:  # noqa: BLE001
                pass


def install_unit(path, desc, cmd, kind, delay):
    with open(path, "w") as f:
        f.write(f"""[Unit]
Description={desc}
After=network-online.target
Wants=network-online.target

[Service]
Type={kind}
Environment=PATH=/hive/bin:/hive/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart={cmd} --delay {delay}
TimeoutStartSec=infinity
Restart=no

[Install]
WantedBy=multi-user.target
""")
    systemctl("daemon-reload")


def cmd_install_apply(_a):
    if not os.path.exists(BEST):
        sys.exit("No best.json yet - finish a tuning run first.")
    bus = json.load(open(BEST))["gpu_bus"]
    unit = apply_unit(bus)
    install_unit(os.path.join(UNIT_DIR, unit), f"Apply GPU autotune result for {bus}",
                 f"{sys.executable} {SCRIPT} apply --watch", "simple", delay=120)
    systemctl("enable", unit)
    systemctl("start", "--no-block", unit)
    print(f"Installed {unit}: {BEST} (card {bus}) is applied 2 minutes after every boot "
          f"and kept applied.")


def cmd_uninstall(_a):
    names = []
    st = load_state()
    if st:
        names += [run_unit(st["cfg"]["bus"]), apply_unit(st["cfg"]["bus"])]
    if os.path.exists(BEST):
        names.append(apply_unit(json.load(open(BEST))["gpu_bus"]))
    for name in dict.fromkeys(names):
        path = os.path.join(UNIT_DIR, name)
        systemctl("stop", name)
        systemctl("disable", name)
        if os.path.exists(path):
            os.remove(path)
    systemctl("daemon-reload")
    print("Services removed. Results files are kept.")


def cmd_probe(a):
    print("== Miner API ==")
    found = False
    for suffix in ("", "/api", "/summary", "/stats", "/api/v1/status"):
        url = a.api_url.rstrip("/") + suffix
        try:
            data = fetch_json(url)
        except Exception as e:  # noqa: BLE001
            print(f"  {url}: no JSON ({type(e).__name__})")
            continue
        found = True
        print(f"  {url}: JSON OK. Numeric fields:")
        for path, val in flatten(data):
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                hint = ""
                p = path.lower()
                if "hash" in p:
                    hint = "   <-- hashrate?"
                elif "accept" in p:
                    hint = "   <-- accepted shares?"
                elif "reject" in p:
                    hint = "   <-- rejected shares?"
                elif "invalid" in p or "hw" in p or "error" in p:
                    hint = "   <-- hw/invalid shares?"
                print(f"     {path} = {val}{hint}")
        break
    if not found:
        print("  No JSON API found - use the log source instead (see below).")

    print("\n== Miner log files (for --source log) ==")
    files = sorted(glob.glob("/var/log/miner/**/*.log", recursive=True) +
                   glob.glob("/var/log/miner/**/*.txt", recursive=True),
                   key=lambda p: -os.path.getmtime(p))
    for path in files[:10]:
        age = time.time() - os.path.getmtime(path)
        hrs = {}
        for gid in range(16):
            v = log_hashrate(path, gid, 10 ** 9)
            if v:
                hrs[gid] = fmt_hr(v)
        print(f"  {path}  (updated {age:.0f}s ago)  GPU rows: {hrs or 'none found'}")
    if not files:
        print("  none under /var/log/miner")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("probe", help="find hashrate/share fields in the miner API or log")
    pr.add_argument("--api-url", default="http://127.0.0.1:63005")

    s = sub.add_parser("start", help="start a tuning run")
    s.add_argument("--gpu", type=int, help="nvidia-smi index of the card (prompted if omitted)")
    s.add_argument("--bus", help="PCI slot of the card, e.g. 00000000:05:00.0")
    s.add_argument("--source", choices=["api", "log"], default="api")
    s.add_argument("--api-url", default="http://127.0.0.1:63005")
    s.add_argument("--hr-path", help="JSON path of this GPU's hashrate (from probe)")
    s.add_argument("--acc-path", help="JSON path of accepted shares (fail if none in a test)")
    s.add_argument("--rej-path", help="JSON path of rejected shares")
    s.add_argument("--hw-path", help="JSON path of HW errors / invalid shares (fail on any)")
    s.add_argument("--volt-path", help="JSON path of core voltage in mV (recorded only)")
    s.add_argument("--log-file", help="miner log file (for --source log)")
    s.add_argument("--miner-gpu", type=int, help="GPU number as the miner shows it")
    s.add_argument("--offset-start", type=int, help="default 0 (prompted)")
    s.add_argument("--offset-max", type=int, help="default: driver maximum")
    s.add_argument("--offset-step", type=int, default=15)
    s.add_argument("--margin", type=int, default=30, help="MHz to stay below the lowest offset that kept crashing")
    s.add_argument("--cap-step", type=int, default=0,
                   help="MHz per cap step (0 = auto, ~1%% of the card's clock)")
    s.add_argument("--cap-min", type=int, default=0,
                   help="lowest clock cap to try (0 = the card's base clock, else 50%% of uncapped)")
    s.add_argument("--min-hr-frac", type=float, default=0.0,
                   help="optional: never accept less than this fraction of uncapped hashrate (0 = off)")
    s.add_argument("--patience", type=int, default=3,
                   help="stop a phase after this many tests in a row without an efficiency gain")
    s.add_argument("--crash-retries", type=int, default=1,
                   help="retries before a crashing setting counts as 'keeps crashing'")
    s.add_argument("--first-warmup", type=int, default=120,
                   help="warmup for the first test and after any restart / power-limit change")
    s.add_argument("--warmup", type=int, default=30, help="warmup between back-to-back tests")
    s.add_argument("--sample", type=int, default=180, help="seconds to measure each test")
    s.add_argument("--pl-min", type=float, help="lowest power limit to test (default: card min)")
    s.add_argument("--pl-max", type=float, help="highest power limit to test (default: card max)")
    s.add_argument("--pl-step", type=int, default=10, help="W between power-limit levels")
    s.add_argument("--no-pl-sweep", action="store_true", help="tune at the current power limit only")
    s.add_argument("--pl-full-offset-search", action="store_true",
                   help="restart the offset search from --offset-start at every power level")
    s.add_argument("--tick", type=int, default=10)
    s.add_argument("--dead-secs", type=int, default=120, help="seconds without hashrate = crash")
    s.add_argument("--max-rejects", type=int, default=2, help="rejects allowed per test (stales happen)")
    s.add_argument("--max-reboots", type=int, default=3,
                   help="reboots / power cycles in a row before giving up")
    s.add_argument("--power-cycle", choices=["wakealarm", "manual"], default="wakealarm",
                   help="CMP cards: how to cold power cycle (default: sreboot wakealarm)")
    s.add_argument("--wake-secs", type=int, default=30, help="seconds off for wakealarm")
    s.add_argument("--force", action="store_true", help="skip the CMP confirmation prompt")
    s.add_argument("--foreground", action="store_true", help="run in this terminal instead of a service")

    rn = sub.add_parser("run", help="(internal) continue the active run")
    rn.add_argument("--delay", type=int, default=0)
    sub.add_parser("status", help="show results")
    sub.add_parser("report", help="print the summary table (also saved to summary.txt)")
    sub.add_parser("stop", help="stop and restore starting settings")
    ap = sub.add_parser("apply", help="apply best.json")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--delay", type=int, default=0)
    sub.add_parser("install-apply", help="apply best.json at every boot")
    sub.add_parser("uninstall", help="remove services")

    a = p.parse_args()
    {"probe": cmd_probe, "start": cmd_start, "run": cmd_run, "status": cmd_status,
     "report": cmd_report,
     "stop": cmd_stop, "apply": cmd_apply, "install-apply": cmd_install_apply,
     "uninstall": cmd_uninstall}[a.cmd](a)


if __name__ == "__main__":
    main()
