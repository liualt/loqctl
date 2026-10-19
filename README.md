# loqctl

A btop-style terminal dashboard and CLI for Lenovo LOQ laptops on Linux. It shows live CPU, NVIDIA GPU, memory, battery and fan readings next to the firmware power and thermal limits, and lets you change those limits with read-back verification. Keyboard RGB is driven through [LegionAura](https://github.com/nivedck/LegionAura). UI colors come only from your terminal's ANSI palette.

![preview](preview.png)

## Requirements

| Need | Details |
| --- | --- |
| OS | Linux only (uses `/sys`, `/proc`, `SO_PEERCRED`, `curses`) |
| Python | 3.10+ (standard library only, no `pip install` needed; see `requirements.txt`) |
| Kernel | Lenovo WMI drivers `lenovo-wmi-gamezone` and `lenovo-wmi-other`. You need `/sys/class/firmware-attributes/lenovo-wmi-other*/` to exist. Linux 6.17 or newer is required for these drivers; check your own kernel with the command below. |
| Privileges | `sudo` for writing firmware settings. Reading works without it, except CPU power (RAPL), which needs root. |
| Terminal | At least 72×20 (96×24 recommended), UTF-8 locale |
| Optional | `libnvidia-ml` (NVML) or `nvidia-smi` on `PATH` for GPU data; `legionaura` on `PATH` for keyboard RGB |

Check that your machine is supported:

```sh
ls /sys/class/firmware-attributes/        # expect lenovo-wmi-other-0
cat /sys/class/platform-profile/*/name    # expect lenovo-wmi-gamezone
cat /sys/class/platform-profile/*/choices # expect a "custom" entry
```

If the first command fails, loqctl can still show telemetry but cannot change firmware limits.

## Install

```sh
git clone <this repository> && cd loqctl
install -Dm755 loqctl.py ~/.local/bin/loqctl   # make sure ~/.local/bin is on PATH
loqctl --version
```

If you run loqctl with sudo (including for the dashboard's writes), install it to a root-owned path instead:

```sh
sudo install -Dm755 loqctl.py /usr/local/bin/loqctl
```

When sudo runs a user-writable file, any process running as you can gain root by editing it.

Or run it in place with `python3 loqctl.py`.

## Dashboard

```sh
loqctl
```

| Key | Action |
| --- | --- |
| `↑↓` / `j k` | Select a control |
| `←→` / `- +` | Stage one step (on the mode row: cycle modes) |
| `0-9` / `Backspace` | Type into the selected limit (custom mode); the old value is struck through |
| `Enter` | Apply all staged edits, each write verified (refuses while a value is marked red). On the mode row: `←→` choose, `Enter` applies |
| `Esc` | Clear the selected row |
| `d` | Stage the firmware default for the selected control |
| `r` | Discard staged edits |
| `l` | Keyboard lighting panel |
| `t` | Toggle the full sensors view (temperatures, power, voltages, throttling) |
| `Space` | Pause telemetry |
| `v` | Reopen the last apply results |
| `s` | Authenticate sudo (shown when needed) |
| `?` | Help |
| `q` | Quit |

Power and temperature limits are only editable in **custom** mode. The other modes (low-power, balanced, performance, and max-power where the firmware offers it) are managed by the firmware, so the limit rows are dimmed and read-only there. To tune limits, select `custom` on the *Platform mode* row and press `Enter` to apply it. The limit rows then unlock with fresh values. Mode changes and limit edits are never applied together.

Nothing is written on start or exit.

### Sudo and privileges

Firmware writes need root. You have two options:

1. **Batched sudo (default).** At start loqctl runs `sudo -v` once (skipped when you are root, using the server, nothing is writable, or sudo is already cached). All staged edits are then applied in a single `sudo -n` call with per-item results. When the sudo cache expires, a banner appears and `s` re-authenticates; edits stay staged. The dashboard itself never runs as root and never asks for a password inside curses.
2. **Privileged server (recommended for frequent tuning).** Run `sudo loqctl serve` in another terminal. The dashboard then connects to it automatically and never needs to leave the screen. See [Local HTTP API](#local-http-api).

### Keyboard panel (`l`)

| Key | Action |
| --- | --- |
| `1`–`4` | Select zone |
| `↑↓` | Choose H / S / V channel |
| `←→` | Nudge channel (`H` / `L` for fine steps) |
| `e` | Cycle effect: static → breath → wave → hue → off |
| `b` | Toggle brightness low/high |
| `w` | Wave direction ltr/rtl |
| `c` | Copy the selected zone color to all four |
| `Space` | Toggle live apply |
| `Enter` | Apply via `legionaura` |
| `Esc` | Back |

Zone swatches in the terminal are the nearest ANSI color; the keyboard shows the real RGB. Settings are read from `~/.config/legionaura/config.json`.

## CLI

Read commands print JSON.

```sh
loqctl status
loqctl metrics
loqctl capabilities
loqctl mode                          # current mode and available modes
sudo loqctl mode custom
sudo loqctl set ppt_pl1_spl 50
```

`sudo` may not find a `~/.local/bin` install; install to `/usr/local/bin` as described above so `sudo loqctl ...` works and the file is root-owned.

Writes check the firmware's min, max and step, write once, then read the value back. `set` is refused unless the mode is `custom`.

| Attribute | Meaning | Unit |
| --- | --- | --- |
| `ppt_pl1_spl` | CPU sustained power limit | W |
| `ppt_pl2_sppt` | CPU boost power limit | W |
| `ppt_cpu_cl` | CPU cross-load power limit | W |
| `ppt_pl1_spl_cl` | CPU sustained limit under cross-load | W |
| `ppt_pl2_sppt_cl` | CPU boost limit under cross-load | W |
| `ppt_pl3_fppt` | CPU fast (PL3) power limit | W |
| `ppt_pl3_fppt_cl` | CPU PL3 limit under cross-load | W |
| `ppt_pl4_ipl` | CPU peak (PL4) power limit | W |
| `ppt_pl4_ipl_cl` | CPU PL4 limit under cross-load | W |
| `ppt_pl1_apu_spl` | APU sustained power limit | W |
| `ppt_pl1_tau` | CPU boost duration | s |
| `cpu_temp` | CPU temperature limit | °C |
| `gpu_nv_ctgp` | GPU configurable total graphics power | W |
| `gpu_nv_ppab` | GPU power performance aware boost | W |
| `gpu_nv_ac_offset` | GPU AC power baseline offset | W |
| `gpu_nv_bpl` | GPU base power limit | W |
| `gpu_nv_cpu_boost` | GPU power taken from the CPU boost budget | W |
| `gpu_temp` | GPU temperature limit | °C |

Only attributes the firmware actually exposes appear. Ranges come from the running firmware; a step of `0` means any integer. Min, max, step and default always come from the firmware's custom-mode table. In other modes the rows show the active mode's live values, which may sit outside that range. Read-back confirms the configuration, not real workload wattage.

### Telemetry

Every source appears only when the system exposes it; anything else shows `N/A` (never a guessed value). Slower sources refresh every 5 seconds.

- Power: all RAPL domains (package, core, uncore, psys, dram), battery watts and status, USB-C source watts/volts/amps. PL1/PL2 and cTGP are drawn as markers on the CPU and GPU power charts.
- Temperatures: CPU cores (coretemp, k10temp, zenpower), ACPI, NVMe, RAM (spd5118, jc42), Wi-Fi, GPU (NVML or amdgpu) and the EC when the `legion_laptop` module provides it.
- Voltages: battery and hwmon `in*` inputs. CPU Vcore needs root (run `loqctl serve` as root) on Intel with the `msr` module loaded; it is N/A on AMD. GPU voltage is not available from NVML.
- Throttling: Intel thermal-throttle event counts and NVML clocks-event reasons.

### Keyboard CLI

```sh
loqctl kb                                   # status
loqctl kb static f2760a
loqctl kb static ff0000 00ff00 0000ff ffffff --brightness 2
loqctl kb breath aa00ff --speed 2 --brightness 1
loqctl kb wave ltr --speed 3
loqctl kb hue --speed 2
loqctl kb brightness 2
loqctl kb off
loqctl kb apply
```

## Local HTTP API

```sh
sudo loqctl serve
```

Serves HTTP over a Unix socket at `/run/loqctl-UID/control.sock`. Only that user and root can connect (checked with peer credentials); the socket is mode `0600` in a root-owned directory. There is no TCP listener. Stop it with Ctrl+C. If a server crashed and left a stale `control.sock`, the next start removes it; it refuses to start if a live server answers on it or if the path is not a socket. Clients ignore a stale socket and fall back to local access.

```sh
curl --unix-socket /run/loqctl-$(id -u)/control.sock http://localhost/v1/status
curl --unix-socket /run/loqctl-$(id -u)/control.sock \
  -H 'Content-Type: application/json' -d '{"mode":"custom"}' \
  http://localhost/v1/mode
```

| Method | Endpoint | Body |
| --- | --- | --- |
| GET | `/v1/status` | Firmware and telemetry |
| GET | `/v1/metrics` | Telemetry (about 1 Hz) |
| GET | `/v1/capabilities` | Modes, bounds, defaults |
| POST | `/v1/mode` | `{"mode":"custom"}` |
| POST | `/v1/attributes/NAME` | `{"value":50}` |

Use `--socket PATH` to point the CLI or dashboard at a specific socket.

## Where the readings come from

- CPU: Intel and AMD Ryzen are supported. Temperature comes from `coretemp` (Package id), `k10temp` or `zenpower` (Tdie, else Tctl). Power: powercap RAPL package energy deltas (needs root; AMD Zen uses the same driver). AMD is tested with simulated sysfs only. Clocks: cpufreq. Utilization: `/proc/stat`.
- GPU: NVML (`libnvidia-ml`) when present, else `nvidia-smi`. While the GPU is runtime-suspended it is not queried, so loqctl does not wake it; the panel shows `asleep`. The dashboard shows the first GPU; JSON lists all.
- RAM: `/proc/meminfo`. Battery, AC and fans: sysfs, when present.
- Missing readings show as `N/A` / `null`.

Firmware control uses the kernel's [Other Mode](https://www.kernel.org/doc/html/next/wmi/devices/lenovo-wmi-other.html) and [Gamezone](https://www.kernel.org/doc/html/next/wmi/devices/lenovo-wmi-gamezone.html) drivers. loqctl makes no raw EC writes and only writes the attributes listed above.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| "No firmware attributes exposed" | Kernel lacks the Lenovo WMI drivers, or your model isn't supported by them |
| "Select custom mode before changing firmware limits" or "Limits are only editable in custom mode. Select custom first." | Limits are only writable in `custom` mode; select `custom` on the mode row and press Enter |
| "RAPL needs root: run sudo loqctl serve" | CPU power reads are root-only on some kernels. Use the privileged server; do not run the TUI with `sudo -E` |
| "A loqctl server is already running" | Another `loqctl serve` answers on the socket; stop it first. A stale socket is removed automatically |
| "legionaura not found" | Install LegionAura (AUR package `legionaura` on Arch) |
| GPU shows offline | Neither `libnvidia-ml` nor `nvidia-smi` (on `PATH`) is usable, or the driver is not loaded. `asleep` means the GPU is runtime-suspended |

## Known limitations

- Linux and Lenovo-WMI hardware only. Not tested on other distros or other Lenovo models.
- Privilege escalation is `sudo` only (no `doas`/`pkexec`).
- Keyboard RGB depends on the third-party `legionaura` tool.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 loqctl.py status
```

Tests use a temporary simulated sysfs and never touch real laptop settings.

## License

GPL-3.0-or-later, see `LICENSE`. Third-party credits are in `NOTICE`.
