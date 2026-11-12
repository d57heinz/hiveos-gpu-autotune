# hiveos-gpu-autotune

An unattended tuner that finds the most **power-efficient** settings for NVIDIA
mining GPUs on **HiveOS**: the core clock offset, a core clock cap (an effective
undervolt), and optionally the power limit. It tunes one card at a time while the
rest of the rig keeps mining. It recovers from crashes by itself, carries on after
reboots, and ends with a table of every test plus the best settings to put into
HiveOS or your miner.

Developed with OneZeroMiner (Pearlhash) on RTX 3090, RTX 5070 Ti and CMP 170HX
(cmpunlocker) cards. It works with any miner whose hashrate it can read.

> **Use at your own risk.** This software changes GPU clocks and power limits,
> deliberately pushes cards until they crash, and can reboot or power-cycle your rig
> by itself. It changes only the card you select, but a crash can still stop the
> miner for every card in the rig for a short time.

---

## What it does

For each power-limit level (or just the current one), it runs two phases:

1. **Offset phase.** It raises the core clock offset 15 MHz per test, starting from
   `+0` or a value you choose. It stops when a setting **crashes twice in a row**
   (one retry rules out flukes) or when **3 tests in a row bring no efficiency
   gain**. The offset it carries on with stays 30 MHz below the lowest offset that
   kept crashing.
2. **Clock-cap phase.** At that offset, it lowers a core clock cap (the same as
   `nvidia-smi -lgc 0,<MHz>`) in steps of about **1% of the card's own clock**:
   15 MHz on a 3090 or 170HX, 30 MHz on a 5070 Ti. With a positive offset, a cap
   makes the card run the same clock at a lower voltage, so it works as an
   undervolt. The same stop rules apply.

**Power-limit sweep (default).** The whole search repeats at every power limit from
a minimum to a maximum in 10 W steps. **The miner is stopped for every power-limit
change**, because changing it under load caused an Xid 62 crash (GPU power
controller halt) on an RTX 5070 Ti. Each new level starts its offset search 2 steps
below the previous level's result.

**Each test** runs a 30 s warm-up and a 180 s sample. The first test, and the first
test after any restart or power-limit change, gets a 120 s warm-up. A test **fails**
if:

- the kernel logs an **Xid** error for that card
- the miner stops hashing
- there are any hardware/invalid shares
- there are more than 2 rejected shares (stale shares happen)
- no shares are accepted

**At the end** it writes:

- `best.json`: the most efficient setting that never crashed, plus the best setting
  without a clock cap (power limit and offset only, which HiveOS can set directly)
- `summary.txt`: the best result at each power limit, and every test
- `results.csv`: every test, for spreadsheets

### Safety and recovery

- **Crash recovery.** After a failure it restores safe settings and restarts the
  miner. If the miner doesn't recover, it reboots the rig and a systemd service
  continues the run after boot. It gives up after 3 reboots in a row.
- **CMP cards (e.g. 170HX with cmpunlocker)** need a *cold* power cycle, because a
  warm reboot leaves them locked. If **any** CMP card is in the rig, every recovery
  reboot is a cold cycle: `sreboot wakealarm 30` (power off, the BIOS wakes the rig
  30 s later). With `--power-cycle manual` it stops and waits for you to switch the
  power off and on instead. After every boot it checks that each CMP card still
  shows its unlocked memory size.
- **"GPU requires reset" (Xid 62/154).** It reboots or power-cycles instead of
  counting the setting as a stability result.
- **It always targets the right card.** The card is saved by **PCI slot**, so a
  resume after a crash still tunes the same card even if another GPU dropped out
  and the numbering changed. Hashrate is also matched to the card by PCI slot in
  the miner API.
- **It only touches the chosen card**, and only its core offset, clock cap and
  (during a sweep) power limit. Memory clocks and fans are left to HiveOS or your
  miner.
- **One service per card.** Runs and apply services are named after the card's
  slot (e.g. `gpu-autotune-01-00-0.service`), so different cards never clash.

---

## Requirements

- HiveOS (or another Debian/Ubuntu-based mining OS with systemd), run as **root**
- An NVIDIA driver that allows clock offsets through NVML (any recent driver)
- Python 3 and `nvidia-ml-py`
- A miner whose hashrate it can read:
  - **OneZeroMiner:** detected automatically through its API
    (`http://127.0.0.1:63005`), including shares, invalid shares and core voltage
  - **Other miners:** read from the miner's log under `/var/log/miner/` (hashrate
    and Xid checks only)
- SSH enabled for the rig in the HiveOS dashboard (Settings → Remote Access),
  if you copy the files over with `scp`

---

## Quick start

### 1. Get the script onto the rig

On the rig:
```bash
git clone https://github.com/d57heinz/hiveos-gpu-autotune.git /home/user/autotune
```
Or copy it from your PC:
```bash
ssh user@<rig-ip> "mkdir -p /home/user/autotune"
scp autotune.py user@<rig-ip>:/home/user/autotune/
```

> **Use a separate folder for each card you tune** (e.g. `/home/user/autotune-gpu0`,
> `/home/user/autotune-170hx`). Each folder keeps its own `best.json`, and an apply
> service reads the `best.json` in its own folder.

### 2. Install the dependency
```bash
apt install -y python3-pip
pip3 install nvidia-ml-py
```

### 3. (CMP cards only) test the cold power cycle
```bash
sreboot wakealarm 30
```
The rig should switch off and boot again by itself. Some boards are slow to boot,
so give it a few minutes. If it doesn't come back, press the power button and use
`--power-cycle manual` in step 5.

### 4. Prepare HiveOS
- For the card you're tuning, set the **core clock field to 0**. Keep its memory
  setting.
- Remove any core clock or offset options for that card from the flight sheet's
  **extra config arguments** (e.g. OneZeroMiner `--coff` / `--cclk`).
- Leave the other cards as they are.

### 5. Start
```bash
cd /home/user/autotune
python3 autotune.py start
```
The guided setup:

| Prompt | What to enter |
|---|---|
| Which GPU | The number next to the card (the list shows name, PCI slot, memory and current offset) |
| Continue with this CMP card | `y` (CMP cards only) |
| Lowest / highest power limit | The range to sweep, e.g. `180` and `260` (Enter = the card's own limits) |
| Use these (miner mapping) | Enter, if it found the card in the miner API |
| Miner log file / GPU number | Only if there's no API: the miner's log and the card's number *as the miner shows it* |
| Starting core offset | Enter for `0`, or a value you know is stable to save time |
| Lowest hashrate you'd accept | Enter for `0` (pure maximum efficiency), or e.g. `90` to give up at most 10% |
| Start tuning now | `y` |

The tuning then runs in the background as a systemd service. You can close the
terminal.

---

## Checking on it

```bash
cd /home/user/autotune

python3 autotune.py report      # best result per power level, then every test
python3 autotune.py status      # every test so far, the current level, the test running now
tail -f autotune.log            # live log (Ctrl+C stops watching, not the run)
cat summary.txt                 # same table as `report`, saved after each level
```

What the card is doing right now:
```bash
nvidia-smi -i <pci-slot> --query-gpu=clocks.sm,clocks.mem,power.draw,power.limit,temperature.gpu --format=csv
# e.g. nvidia-smi -i 00000000:01:00.0 --query-gpu=...
```

Is the service running?
```bash
systemctl status gpu-autotune-01-00-0 --no-pager     # name = gpu-autotune-<slot>
```

Log lines to look for:

| Log line | Meaning |
|---|---|
| `TEST PL 200 W, offset +255, cap none` | A test has started |
| `OK  143.91 TH/s  199.2 W  eff 0.723 TH/W ...` | A test passed |
| `UNSTABLE: Xid error in kernel log` | The setting crashed the card (expected near the limit) |
| `Retrying offset +330 once ...` | First crash at this setting, retrying |
| `LEVEL DONE (PL 200 W): best ...` | One power level is finished |
| `NEEDS A COLD POWER CYCLE` | Switch the rig off at the PSU for ~30 s, then power on. The run continues by itself |
| `FINISHED` | Done. See `best.json` and `summary.txt` |

To stop early (restores the starting offset and power limit):
```bash
python3 autotune.py stop
```

---

## Using the result

`best.json` contains the best overall result (power limit, offset and clock cap)
and the best result without a cap. The best settings are applied when the run
finishes, but only until the next reboot. To keep them:

**Option A: HiveOS plus the apply service** (any miner)
1. In HiveOS, set the card's **power limit** and **core offset** to the values in
   `best.json`.
2. If the best result has a clock cap, HiveOS can't hold both an offset and a cap,
   so let the script add the cap at every boot:
   ```bash
   python3 autotune.py install-apply
   ```
   This applies the result 2 minutes after boot, and puts it back if the miner or
   HiveOS resets it.

**Option B: the miner's own overclock options** (OneZeroMiner)
Add to the flight sheet's extra config arguments, e.g.:
```
--coff 315 --cclk 1700 --mclk 405
```
Leave the card's clock fields in HiveOS empty, so HiveOS doesn't reset what the
miner sets. If the miner's settings get reset just after it starts,
`--core-delay 60` applies them a minute later.

**Windows (same card or a similar one)**

Results from one card are only a starting point for another, because every chip's
limit is different. Back the offset off by about 30 MHz on a different card.
- Power limit, from an Administrator command prompt: `nvidia-smi -i 0 -pl 200`
  (put it in a `.bat` file run by Task Scheduler at startup, "Run with highest
  privileges")
- Offset and cap: use the miner's options (`--coff`, `--cclk`), or MSI Afterburner
  for the offset and `nvidia-smi -i 0 -lgc 0,<cap>` for the cap. `nvidia-smi`
  can't set offsets.

---

## Useful options

| Option | Default | What it does |
|---|---|---|
| `--gpu N` / `--bus 0000xxxx:yy:00.0` | prompt | Choose the card without the prompt |
| `--offset-start N` | 0 | First offset to test |
| `--offset-step N` | 15 | MHz per offset step |
| `--margin N` | 30 | MHz to stay below the lowest offset that kept crashing |
| `--cap-step N` | auto (~1%) | MHz per clock-cap step |
| `--min-hr-frac F` | 0 | Never accept less than this fraction of the uncapped hashrate (e.g. 0.9) |
| `--patience N` | 3 | Tests without an efficiency gain before a phase stops |
| `--crash-retries N` | 1 | Retries before a setting counts as "keeps crashing" |
| `--pl-min W` / `--pl-max W` / `--pl-step W` | card limits / 10 | Power-limit sweep range |
| `--no-pl-sweep` | off | Tune at the current power limit only |
| `--pl-full-offset-search` | off | Restart the offset search from `--offset-start` at every power level |
| `--first-warmup S` / `--warmup S` / `--sample S` | 120 / 30 / 180 | Test timing in seconds |
| `--power-cycle wakealarm\|manual` | wakealarm | How to cold power cycle rigs with a CMP card |
| `--api-url URL` | `http://127.0.0.1:63005` | Miner API |
| `--hr-path` etc. / `--source log --log-file F --miner-gpu N` | auto | Manual miner mapping (see `probe`) |
| `--foreground` | off | Run in the terminal instead of as a service |

Run `python3 autotune.py start --help` for the full list, and
`python3 autotune.py probe` to see what your miner reports.

---

## Files

| File | Contents |
|---|---|
| `autotune.py` | The script |
| `state.json` | The current run: settings, card slot, every result. Used to resume after reboots |
| `autotune.log` | Everything the tuner did, kept across reboots |
| `results.csv` | Every test (power limit, offset, cap, hashrate, power, efficiency, clock, temperature, voltage) |
| `summary.txt` | Best result per power level, plus every test |
| `best.json` | The final result |

---

## Tips

- **Don't replace `autotune.py` during a run.** After a reboot, the resume service
  runs whatever file is in the folder.
- **Lower power limits are usually more efficient.** On RTX 3090s, efficiency fell
  with every 10 W above about 200 W, so start sweeps lower rather than higher.
- **Run one card at a time.** Leave the others mining; identical cards make a good
  live comparison.
- **The best efficiency isn't always the best profit.** A big clock cap can save a
  lot of power and still cost too much hashrate. Look at the per-level table in
  `summary.txt` and pick using your electricity price, or set `--min-hr-frac`.
- Expect a few crashes. Finding where the card fails is part of the search.
