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
- no shares are accepted during warm-up and sample, nor in a further 2-minute wait

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
| Which GPU(s) | One card's number, several in order (`0,1,2`), or `all`. More than one card starts a queue, each card in its own folder (see below) |
| Continue with this CMP card | `y` (CMP cards only) |
| Lowest / highest power limit | The range to sweep, e.g. `180` and `260` (Enter = the card's own limits) |
| Use these (miner mapping) | Enter, if it found the card in the miner API |
| Miner log file / GPU number | Only if there's no API: the miner's log and the card's number *as the miner shows it* |
| Starting core offset | Enter for `0`, or a value you know is stable to save time |
| Lowest hashrate you'd accept | Enter for `0` (pure maximum efficiency), or e.g. `90` to give up at most 10% |
| Start tuning now | `y` |

The tuning then runs in the background as a systemd service. You can close the
terminal.

### Tune every card in one go

```bash
cd /home/user/autotune
python3 autotune.py start --all          # or: --cards 0,2,1 for some cards, in that order
```
It asks the questions once (power-limit range, starting offset, lowest acceptable
hashrate), then tunes the cards **one after another**. Each card gets its own folder
next to the one you started from, named after its PCI slot:

```
/home/user/autotune/            <- where you ran it (holds queue.json)
/home/user/autotune-01-00-0/    <- GPU at 00000000:01:00.0: its state, log, best.json
/home/user/autotune-02-00-0/
...
```
so no card's `best.json` can overwrite another's. When a card finishes, its apply
service is installed (unless you pass `--no-auto-apply`) so it keeps its result
while the next card is tuned, and the next card starts by itself. The queue
survives reboots and power cycles.

```bash
python3 autotune.py queue             # every card's status and best result
python3 autotune.py queue --resume    # continue with pending cards after a `stop`
```
To stop, run `python3 autotune.py stop` in the **active card's** folder (shown by
`queue`). The remaining cards stay pending until you `--resume`.

Apply services set the offset and clock cap, not the power limit: enter each card's
power limit from `queue` in HiveOS.

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
| `warmup done - sampling for 180s` | The measurement window has started |
| `OK  143.91 TH/s  199.2 W  eff 0.723 TH/W ... shares 6` | A test passed (accepted shares during the test) |
| `no accepted share in the sample window, but 2 during warmup` | Unlucky stretch; the test still counts |
| `no accepted share yet ... waiting up to 120s more` | No share at all so far; it waits for one before failing the test |
| `UNSTABLE: Xid error in kernel log` | The setting crashed the card (expected near the limit) |
| `Retrying offset +330 once ...` | First crash at this setting, retrying |
| `LEVEL DONE (PL 200 W): best ...` | One power level is finished |
| `NEEDS A COLD POWER CYCLE` | Switch the rig off at the PSU for ~30 s, then power on. The run continues by itself |
| `FINISHED` | Done. See `best.json` and `summary.txt` |

To stop early (restores the starting offset and power limit), and to carry on
later:
```bash
python3 autotune.py stop
python3 autotune.py resume      # continue the stopped run, keeping its results
python3 autotune.py rerun       # or start this card over with the same options
python3 autotune.py conclude    # or close it and keep the best result so far
```
`resume` and `conclude` also work on a run that aborted by itself, for example
after "more than 3 reboots in a row". `conclude` needs at least one completed
power level.

To re-tune a card later (e.g. with a different power-limit range), stop the card's
apply service first so it doesn't keep re-applying the old result. `start` offers
to do this for you:
```bash
python3 autotune.py uninstall                      # this folder's tuning + apply services
# or by name:  systemctl disable --now gpu-autotune-apply-<slot>
systemctl list-units 'gpu-autotune*' --all --no-pager   # list them
```

---

## Fine-tuning a finished result

Once a card's normal tune has finished, an optional second stage can squeeze out
a little more and check that the result holds over a longer run. It only works
on a folder that already has a finished run and a `best.json`.

```bash
cd /home/user/autotune-01-00-0        # the tuned card's folder
python3 autotune.py finetune
```
or press "Fine-tune this result" on the card's panel in the dashboard.

1. **Offset refine.** With the chosen clock cap in place, the offset is raised
   again in 15 MHz steps. At the capped clock the card sits at a different point
   on its voltage curve, so it may tolerate more offset than it did uncapped,
   and every extra step lowers voltage and power at the same hashrate. The same
   rules apply: a crash is retried once, the search stops after two crashes or
   three tests without a gain, and the result stays 30 MHz below a setting that
   kept crashing. Skipped when the result has no clock cap.
2. **Soak test.** The result runs for 30 minutes. If it fails, the offset is
   stepped back 15 MHz and the soak repeats, up to four times.

The new result replaces `best.json` (the previous one is kept as
`best.json.pre-finetune`), and the card's apply service is turned back on if it
was on. Expect about an hour, with a crash or two on the way.

| Option | Default | What it does |
|---|---|---|
| `--no-refine` | off | Only soak-test the current result |
| `--no-soak` | off | Only refine the offset |
| `--soak-mins N` | 30 | Length of the soak test |
| `--max-stepbacks N` | 4 | How many times to step back after a failed soak |
| `--offset-max N` | model or run limit | Highest offset the refine may try |

`stop` during a fine-tune puts the card back on its saved result, and `resume`
continues it.

---

## Web dashboard

`autotune.py start` installs and starts a web page on the rig and prints its
address next to the `tail` command:

```
Dashboard (live stats, tuning progress, logs):  http://<rig-ip>:8088
```

Open it from any device on your network. It shows, refreshing every 5 seconds:

- rig hashrate, GPU power and efficiency, with history charts (up to 6 hours)
- every card: live trends for hashrate, power, temperature and GPU load, plus
  power against its limit, clocks, offset, voltage, memory and shares
- the best settings found for every tuned card, in one table
- each tuning run: the step it is on (power limit, offset search, clock cap
  search), a progress bar for the current test, the last few results, the
  queue, and a chart of every test with crashes marked
- three live log panes side by side: tuner log, miner output and syslog

**Arranging the page.** Drag any panel by its title to move it, or use the
arrow buttons on the panel. The ⤢ button switches a panel between its normal
width and the full page width, and "Logs full width" does that for the three log
panels at once (one per row instead of three). The arrangement is remembered in
your browser; "Reset layout" restores the default.

**Starting and stopping tuning from the page.**

- "Start a tuning run": tick the cards, optionally set the power-limit range,
  starting and highest offset and the lowest hashrate you'd accept, then
  "Start tuning". The cards are tuned one after another, each in its own folder.
- "Stop tuning" on an active run puts the card back on its starting offset and
  power limit.
- "Resume tuning" carries a stopped run on from where it stopped, keeping its
  results. "Run again with the same options" starts that card over.
- "Fine-tune this result" on a finished run starts the refine and soak stage
  described above.

The same actions exist on the command line: `autotune.py stop`, `resume` and
`rerun`, run in the card's folder.

**Controls for a tuned card.** When a card's tuning has finished, its panel has:

- a switch that turns the card's apply service on or off (whether its settings
  are applied after every boot and put back if something resets them);
- a "Use" button next to each power level's best result, with and without a
  clock cap, which applies that setting to the card right away and makes it the
  one the apply service keeps. "Back to the tuner's pick" undoes your choice.

Only settings that were tested and never crashed can be applied, and nothing can
be changed while a run is active on the card. The power limit is still set in
HiveOS; the page tells you which value matches the setting you chose. Start the
dashboard with `--read-only` to remove these controls.

The dashboard lives in the `dashboard/` folder and runs as its own service, so it
keeps running after tuning ends and starts at every boot.

```bash
python3 dashboard/monitor.py install              # install or reinstall by hand
python3 dashboard/monitor.py install --port 9000  # another port
python3 dashboard/monitor.py install --read-only  # view only, no controls
python3 dashboard/monitor.py uninstall
python3 dashboard/monitor.py                      # run in the terminal instead
```
`start --no-dashboard` skips it.

Apart from the controls above, which all work by calling `autotune.py`, the page only reads: `nvidia-smi`, the
miner's local API (OneZeroMiner), the `autotune*` folders, the miner log and
syslog. It has no login, so anyone on your network can view it and use those
controls. Don't forward the port to the internet. Hashrate, shares and voltage
need OneZeroMiner's API;
with other miners the page still shows power, clocks, temperatures, tuning and
logs.

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
| `--all` / `--cards 0,2,1` | off | Tune every card (or the listed ones) in turn, each in its own folder |
| `--no-auto-apply` | off | With `--all`: don't install each card's apply service when it finishes |
| `--offset-start N` | 0 | First offset to test |
| `--offset-max N` | driver max (RTX 3090: 300) | Highest offset to test. Some models have a built-in limit where more offset isn't worth the instability |
| `--offset-step N` | 15 | MHz per offset step |
| `--margin N` | 30 | MHz to stay below the lowest offset that kept crashing |
| `--cap-step N` | auto (~1%) | MHz per clock-cap step |
| `--min-hr-frac F` | 0 | Never accept less than this fraction of the uncapped hashrate (e.g. 0.9) |
| `--share-grace S` | 120 | Extra seconds to wait for a share when none arrived during warm-up and sample |
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
| `dashboard/` | The web dashboard (`monitor.py`, `index.html`) |
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

---

## License

[MIT](LICENSE) © 2026 d57heinz. Free to use, modify and share. Provided as is,
without warranty. Tuning, crashing and power-cycling GPUs is at your own risk.
