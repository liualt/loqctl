# loqctl

A btop-style hardware monitor and control tool for Lenovo LOQ (and Legion) laptops on Linux. It shows temperatures, power draw per RAPL domain, voltages, clocks, fans and throttling next to the firmware power and thermal limits that cause them, and lets you change those limits with read-back verification. It is not a process monitor; use btop for that. Keyboard RGB is driven through [LegionAura](https://github.com/nivedck/LegionAura). UI colors come only from your terminal's ANSI palette.

![preview](preview.png)

## Requirements

| Need | Details |
| --- | --- |
| OS | Linux only (uses `/sys`, `/proc`, `SO_PEERCRED`, `curses`) |
| Python | 3.10+ (standard library only, no `pip install` needed; see `requirements.txt`) |
| Kernel | Lenovo WMI drivers `lenovo-wmi-gamezone` and `lenovo-wmi-other`. You need `/sys/class/firmware-attributes/lenovo-wmi-other*/` to exist. Linux 6.17 or newer is required for these drivers; check your own kernel with the command below. |
| Privileges | `sudo` for writing firmware settings. Reading works without it, except CPU power (RAPL) on kernels that make it root-only and CPU 0 Vcore (always root). |
| Terminal | At least 72×20 (96×24 recommended), UTF-8 locale |
| Optional | `libnvidia-ml` (NVML) or `nvidia-smi` on `PATH` for GPU data; `legionaura` on `PATH` for keyboard RGB; the LenovoLegionLinux `legion_laptop` kernel module for fan curves |

Check that your machine is supported:

```sh
ls /sys/class/firmware-attributes/        # expect lenovo-wmi-other-0
cat /sys/class/platform-profile/*/name    # expect lenovo-wmi-gamezone
cat /sys/class/platform-profile/*/choices # expect a "custom" entry
uname -r                                  # 6.17 or newer
```

If the first command fails, loqctl can still show telemetry but cannot change firmware limits.

## Install

```sh
git clone <this repository> && cd loqctl
install -d ~/.local/lib/loqctl && cp -r loqctl loqctl.py ~/.local/lib/loqctl/ \
  && python3 -m compileall -q ~/.local/lib/loqctl \
  && ln -sf ~/.local/lib/loqctl/loqctl.py ~/.local/bin/loqctl   # make sure ~/.local/bin is on PATH
loqctl --version
```

This user install is for the dashboard only. If you run loqctl with sudo (including for the dashboard's writes), install it to a root-owned path instead:

```sh
sudo install -d /usr/local/lib/loqctl && sudo cp -r loqctl loqctl.py /usr/local/lib/loqctl/ \
  && sudo python3 -m compileall -q /usr/local/lib/loqctl \
  && sudo ln -sf /usr/local/lib/loqctl/loqctl.py /usr/local/bin/loqctl
```

When sudo runs a user-writable file, any process running as you can gain root by editing it.

loqctl needs the `loqctl/` package next to `loqctl.py`: the batched sudo calls (`_apply`, `_capdata`, `_fancurve`) run `sudo -n -- <the running Python> -I <real path of loqctl.py>`, resolved through the symlink. Keep both under the same root-owned directory. Never add a passwordless sudoers rule for a path you can edit.

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
| `f` | Fan curve panel |
| `m` | Mode limits overlay: every mode's firmware limits side by side (`Enter` / `Esc` / `m` closes) |
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
| `Esc` / `l` | Back |
| `q` | Quit |

Zone swatches in the terminal are the nearest ANSI color; the keyboard shows the real RGB. Settings are read from `~/.config/legionaura/config.json`.

### Fan curves (`f`)

Fan curves are read and written through the [LenovoLegionLinux](https://github.com/johnfanv2/LenovoLegionLinux) `legion_laptop` kernel module (loqctl does not copy its code; it only uses the hwmon files the module exposes). Without the module the panel says so and shows fan RPM only.

- Load the module with `enable_platformprofile=0` next to the mainline `lenovo-wmi-gamezone` driver, so gamezone stays the single platform-profile provider (loqctl keeps using it). Also make gamezone load first, or the module's `legion_wmi` driver claims the gamezone WMI device and every limit reads N/A (see Troubleshooting). `/etc/modprobe.d/legion_laptop.conf`:

  ```
  options legion_laptop enable_platformprofile=0
  softdep legion_laptop pre: lenovo_wmi_gamezone
  ```
- Curves are editable only while the live platform mode is `custom`, like limits. Other modes show "Fan curves are locked in <mode> mode".
- The firmware resets the curve when the mode changes and on reboot.
- The panel shows a graph of fan speed against the CPU up temperature of each point (a step line for fan 1, markers for fan 2, the selected point highlighted, a cursor at the live CPU temperature), and below it a table of every point: CPU/GPU/IC up/down temperatures and both fan speeds. "down" is the absolute lower temperature of a step (the `_temp_hyst` file). Staged changes show in the warning color.
- Speeds are shown in the module's unit (`fancurve_speed_unit`): RPM for `rpm` (scaled with `fan1_max`), firmware levels 0-10 for `level`, percent otherwise. loqctl writes the exact pwm value the module maps back to the same speed, so readback matches.
- Temperatures are fixed: up and down temperatures are shown but not editable. Each point has one speed, edited in the curve's unit. Setting it writes the same speed to fan 1 and fan 2, each held between its neighbours' speeds, its safety floor and the top speed.
- A point's speed never crosses its neighbours: it stays between the previous and next point's speed.
- The curve is read in the background when the panel opens and cached until the mode changes or a curve is written. Each file read costs the module a full EC table read (about 0.9 s for the whole curve on a LOQ 15IRX9).
- `loqctl fan` prints the curve, speed unit, `max_rpm` and RPM as JSON.
- Verified on a LOQ 15IRX9 (BIOS NECN44WW, LenovoLegionLinux 0.1.3): reading the curve, the `rpm` unit and its scaling, and that the stock curve passes validation. **Writes are unverified on hardware.** On this model the module reads the curve from one EC table and writes another, so a verified apply needs a real test; if readback never matches, loqctl reports the writes as FAILED rather than guessing.

| Key | Action |
| --- | --- |
| `Tab` / `]` | Select the next point |
| `Shift+Tab` / `[` | Select the previous point |
| `↑` / `↓` | Change the point's speed for both fans by 100 RPM, 5 % or 1 level (`Shift`: 500 RPM, 20 % or 2 levels) |
| `r` | Discard all edits |
| `Enter` | Apply; refuses and selects the first problem point if the curve does not validate |
| `p` | Stage the next curve (`stock`, then saved curves); nothing is written until `Enter` |
| `R` | Reset: write the recorded stock curve for the current mode and delete `active.json` (custom mode only) |
| `W` | Save the staged (or live) curve as `curves/saved-YYYYmmdd-HHMMSS.json` |
| `f` / `Esc` | Back (staged edits are kept) |
| `q` | Quit |

Curves: `stock` is the live curve loqctl read just before its first write in a mode, saved once per mode to `~/.config/loqctl/stock/<mode>.json`. The firmware cannot read the stock curve back once a custom one is written, so this is the only copy. A `<mode>.written` marker next to it records that loqctl has written in that mode (every apply and every `R` sets it), and the stock curve is never recorded again after that. This cannot be verified: a curve written by another tool (or by root, or `loqctl _fancurve`) before loqctl's first apply in a mode is recorded as stock. If the read before the first write fails, the apply still happens and says the stock curve was not recorded. `R` writes the stock curve back (verified like any apply) and deletes `active.json`, so `serve` stops re-applying a custom curve. With no recorded stock curve, `R` only deletes `active.json` and says the firmware restores the stock curve on the next mode change or power cycle. File errors during `R` are shown in the notice. Saved curves live in `~/.config/loqctl/curves/<name>.json` (`XDG_CONFIG_HOME` is respected), listed after `stock` by name (a saved curve named `stock` shows as `stock (saved)`); one that fails validation or has the wrong point count shows a red "(invalid)" and cannot be staged. The staged curve is tracked by name. After every verified apply the dashboard writes the applied points to `~/.config/loqctl/active.json`. All files are written atomically with mode 0600, by your user only (never by root; nothing is saved while the dashboard runs as root).

Re-apply under `sudo loqctl serve`: the firmware resets the curve on mode change and reboot, so `serve` re-writes `active.json` for the served uid when the live mode becomes `custom` (also when `serve` starts in `custom`) and after a suspend/resume while in `custom`. Resume is detected when `CLOCK_BOOTTIME` gains 10 s or more on `CLOCK_MONOTONIC` between two 5 s firmware polls. The file must be owned by the served uid, must not be group or world writable, must have the same point count as the hardware and must pass the same validation; otherwise `serve` logs one line and carries on. The dashboard itself never re-applies.

Safety: writes go only to an allowlist of the per-point temperature and pwm files (existing files only, never created), never `fanN_target`, `auto_points_size`, `minifancurve`, `lockfancontroller` or `fan_fullspeed`. Every write is verified by reading the whole curve back. A curve is refused before any write unless: all values are integers (temperatures 0-127, pwm 0-255); upper temperatures and fan speeds never drop from one point to the next; each down temperature is not above its own up temperature (the stock LOQ curve puts down temperatures above the previous point's up temperature, so that is allowed); and the safety floors, a loqctl policy rather than a firmware limit: after a point whose CPU or GPU up temperature is 80 °C or more fans run at least 2000 RPM (`rpm` unit) or 40 % (other units), and the last point runs at least 3000 RPM or 60 %. The module itself only range-checks values.

## CLI

Read commands print JSON.

```sh
loqctl status
loqctl metrics
loqctl capabilities
loqctl limits                        # per-mode firmware limits as JSON
loqctl fan                           # fan curve, speed unit and RPM as JSON
loqctl mode                          # current mode and available modes
sudo loqctl mode custom
sudo loqctl set ppt_pl1_spl 50
```

Other forms: `loqctl tui` (same as no command), `loqctl --socket PATH ...`, `sudo loqctl serve [--uid UID]`, `loqctl --version`. `_apply`, `_capdata` and `_fancurve` are internal, used via sudo by the dashboard.

`sudo` may not find a `~/.local/bin` install; install system-wide as described above so `sudo loqctl ...` works and the file is root-owned.

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

Only attributes the firmware actually exposes appear. Ranges come from the running firmware; a step of `0` means any integer. Min, max, step and default always come from the firmware's custom-mode table. In other modes the rows show that mode's firmware limits. sysfs `current_value` only holds the custom settings (the firmware returns them in every mode), so loqctl reads the real per-mode defaults from the firmware capability table that the kernel dumps in debugfs (`/sys/kernel/debug/lenovo_wmi/*/capdata`, root only). It reads the table as root once (via sudo, or directly under `sudo loqctl serve`) and caches it per product and BIOS version in `~/.cache/loqctl/mode-limits.json`, so later starts need no sudo. Press `m` for a mode-by-mode overlay, or run `loqctl limits` for the same data as JSON (`{mode: {attribute: value}}`; custom is the live value, plus an `error` key when the table is unavailable). Without the table the stock-mode values show N/A and the overlay says "Stock-mode limits need root: press `s` or run `sudo loqctl serve`".

The cache is read first and only reread when the product name or BIOS version differs, so a BIOS update triggers one new sudo fetch. To force a refresh, delete `~/.cache/loqctl/mode-limits.json` (`XDG_CACHE_HOME` is respected). The cache is written by your user, mode 0600. Read-back confirms the configuration, not real workload wattage.

### Telemetry

Every source appears only when the system exposes it; anything else shows `N/A` (never a guessed value). Slower sources refresh every 5 seconds.

- Power: all RAPL domains (package, core, uncore, psys, dram), battery watts and status, USB-C source watts/volts/amps. PL1/PL2 and cTGP are drawn as markers on the CPU and GPU power charts.
- Temperatures: CPU cores (coretemp, k10temp, zenpower), ACPI, NVMe, RAM (spd5118, jc42), Wi-Fi, GPU (NVML or amdgpu) and the EC when the `legion_laptop` module provides it.
- Voltages: battery and hwmon `in*` inputs. CPU 0 Vcore (read from CPU 0 only; N/A outside 0.3 to 2.0 V) needs root (run `loqctl serve` as root) on Intel with the `msr` module loaded; it is N/A on AMD. GPU voltage is not available from NVML.
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
| GET | `/v1/status` | Sampler snapshot: firmware and telemetry (not a fresh read) |
| GET | `/v1/metrics` | Telemetry from the sampler (about 1 Hz) |
| GET | `/v1/capabilities` | Modes, bounds, defaults, per-mode limits; read fresh |
| GET | `/v1/fancurve` | Fan curve, speed unit, RPM (or `available: false`) |
| POST | `/v1/mode` | `{"mode":"custom"}` |
| POST | `/v1/attributes/NAME` | `{"value":50}` |
| POST | `/v1/fancurve` | `{"points":[...]}`, custom mode only, per-field results |

The server allows the user in `SUDO_UID`. If you start it as root without sudo, pass `--uid UID` so your user can connect.

Note: `/v1/status` returns the server's sampler snapshot (refreshed by its polling), while `/v1/capabilities` reads the firmware fresh on each request. There is no `/v1/limits`; per-mode limits are part of the `/v1/capabilities` and `/v1/status` payloads (`mode_limits`, `mode_limits_error`).

Use `--socket PATH` to point the CLI or dashboard at a specific socket. Without it, a socket that does not accept connections is ignored with a notice and loqctl reads locally.

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
| "Mode set via lenovo-legion, not lenovo-wmi-gamezone" on the mode row | `legion_laptop` was loaded without `enable_platformprofile=0` and registered its own platform profile. Add `options legion_laptop enable_platformprofile=0` to `/etc/modprobe.d/legion_laptop.conf` and reboot |
| "Gamezone WMI device held by legion_wmi, limits can't be read", or every limit N/A with "current value unreadable (Invalid argument)" | `legion_laptop` loaded before `lenovo-wmi-gamezone` and its `legion_wmi` driver claimed the gamezone WMI device (GUID `887B54E3-…`). `lenovo-wmi-other` then cannot learn the mode and rejects every read, and no platform profile exists. Add `softdep legion_laptop pre: lenovo_wmi_gamezone` to `/etc/modprobe.d/legion_laptop.conf` and reboot. Without rebooting: `echo 887B54E3-DDDC-4B2C-8B88-68A26A8835D0-22 \| sudo tee /sys/bus/wmi/drivers/legion_wmi/unbind /sys/bus/wmi/drivers/lenovo_wmi_gamezone/bind` (the `-22` suffix varies; check `ls /sys/bus/wmi/devices`). `legion_laptop` keeps working: it calls the gamezone methods by GUID, not through the binding |
| "Custom cannot be selected through the legacy platform_profile file" (older versions: "Firmware rejected the write: Invalid argument") | No platform-profile class device was found, and the kernel's legacy `/sys/firmware/acpi/platform_profile` refuses `custom`. Make sure `lenovo-wmi-gamezone` is loaded |
| "RAPL needs root: run sudo loqctl serve" | CPU power reads are root-only on some kernels. Use the privileged server; do not run the TUI with `sudo -E` |
| "A loqctl server is already running" | Another `loqctl serve` answers on the socket; stop it first. A stale socket is removed automatically |
| "sudo not authenticated" or "sudo credentials expired" | The sudo cache expired (or you skipped it at start). Press `s` to authenticate; staged edits are kept |
| "Ignored stale API socket" | A server left its socket behind. loqctl reads locally; restart `sudo loqctl serve` if you want the server |
| Stock-mode limits show N/A, "needs root" or "debugfs capability table not found" | The capdata table is root-only. Press `s` (or run `sudo loqctl serve`) so loqctl can read it once. "not found" means debugfs is not mounted (`sudo mount -t debugfs none /sys/kernel/debug`) or the driver exposes no `lenovo_wmi` capdata; custom mode still works |
| "sudo credentials expired" while reading limits or applying | Press `s`; the table read is retried on the next start, staged edits are kept |
| "Fan curves need the LenovoLegionLinux legion_laptop module" or "legion_laptop hwmon not found" | The `legion_laptop` module is not loaded or its hwmon (`legion_hwmon`) has no curve files. Load it with `enable_platformprofile=0` |
| Keyboard panel shows defaults or stale colors | `~/.config/legionaura/config.json` is missing, unreadable or not a JSON object; loqctl ignores it silently. Run `legionaura` once to create it |
| "legionaura not found" | Install LegionAura (AUR package `legionaura` on Arch) |
| GPU shows offline | Neither `libnvidia-ml` nor `nvidia-smi` (on `PATH`) is usable, or the driver is not loaded. `asleep` means the GPU is runtime-suspended |

## Known limitations

- Linux and Lenovo-WMI hardware only. Tested on a LOQ 15IRX9 (Intel) on CachyOS; not tested on other distros or other Lenovo models.
- AMD CPU paths (`k10temp`, `zenpower`, RAPL on Zen) are tested with simulated sysfs only.
- Not verified on hardware: fan curve writes (`legion_laptop`; reads are verified on a LOQ 15IRX9), the extra attribute keys added to the write allowlist in 0.3.0, and the sudo capdata fetch. All are covered by simulated sysfs and mocked subprocess tests.
- Fan curve re-apply and suspend/resume detection in `serve` are untested on real hardware.
- Per-mode limits need the firmware capdata table in debugfs; without it stock modes show N/A.
- Privilege escalation is `sudo` only (no `doas`/`pkexec`).
- Keyboard RGB depends on the third-party `legionaura` tool.

## Changelog

### 0.4.1

- Fix limits showing N/A in every mode when `legion_laptop` is loaded: its `legion_wmi` driver claimed the gamezone WMI device before `lenovo-wmi-gamezone`, so `lenovo-wmi-other` rejected every read with EINVAL. loqctl now detects this and names the cause; the README documents the `softdep` fix.
- An unreadable limit shows the kernel's error in the detail footer instead of a bare N/A.

### 0.4.0

- Fan curve editor rework: a graph of speed against temperature with axes, grid, the selected point and a live CPU temperature cursor; Tab/`[` `]` select a point, arrows move it (Shift for bigger steps); one curve drives both fans; points never cross their neighbours.
- Speeds in the module's real unit: RPM on `rpm` models (the LOQ 15IRX9), levels on `level` models. Writes use the pwm value that reads back unchanged, so verification works on `rpm` and `level` models.
- Validation matches the firmware: the stock LOQ curve was rejected before (down temperatures above the previous up temperature, and percent floors applied to RPM). Safety floors are now per unit.
- The curve is read in the background and cached (about 0.9 s per read on EC models); the panel opens at once.
- The right box border is drawn (the last terminal column was skipped).

### 0.3.2

- Consistent wording: sentence-case panel titles and column headers, a space between numbers and units (`60 W`, `2100 RPM`, `1200 MHz`; none before `°C` and `%`), `Fan 1 %` / `Fan 2 %`, `Battery` / `Power N/A` instead of `BAT` / `PWR?`.
- Tests guard the ANSI-only color rule: no `init_color`, no raw escape sequences, color numbers 15 or below.

### 0.3.1

- Fix "Firmware rejected the write: Invalid argument" when selecting custom. The kernel's legacy `platform_profile` file lists `custom` but refuses to set it; loqctl now uses a platform-profile class device (gamezone first) and only falls back to the legacy file for other modes.
- The mode row warns when the platform mode is handled by a driver other than `lenovo-wmi-gamezone`.

### 0.3.0

- Per-mode firmware limits from the capdata table, cached per product and BIOS; `m` overlay and `loqctl limits`.
- Fan curve editor (`f`) over `legion_laptop` hwmon: validated, verified writes, stock reset, saved curves, `loqctl fan`, `/v1/fancurve`, re-apply in `serve`.
- Batched `sudo -n` applies with pre-auth and re-auth on expiry; one apply results view (`v`).
- Telemetry: RAPL domains, temperatures, voltages, throttling, limit markers on the power charts, sensors view (`t`).
- Portability: PATH lookups, NVML via `ctypes`, no GPU wake-up while suspended, AMD CPU paths, write allowlist.
- Stale socket recovery, resilient sampler, 5 s firmware polling.
- Package layout: `loqctl/` package with the `loqctl.py` entry point; Python 3.10 minimum; GPL-3.0-or-later.

### 0.2.0

Previous release.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 loqctl.py status        # or: python3 -m loqctl status
```

Tests use a temporary simulated sysfs and never touch real laptop settings.
To contribute, read [CONTRIBUTING.md](CONTRIBUTING.md): changes start as an issue and land through a pull request.

| Module | Role |
| --- | --- |
| `loqctl.py` | Single-file entry point (also what `sudo` runs for batched applies) |
| `loqctl/cli.py` | Argument parsing and subcommands |
| `loqctl/common.py` | Shared helpers, the `FIELDS` write allowlist |
| `loqctl/hardware.py` | Firmware reads and verified writes |
| `loqctl/sensors.py` | Telemetry sources, NVML via `ctypes`, the background sampler |
| `loqctl/api.py` | Unix-socket HTTP server and client |
| `loqctl/backend.py` | Chooses local access or the API server |
| `loqctl/keyboard.py` | LegionAura keyboard control |
| `loqctl/fancurve.py` | Fan curve reads, validation and verified writes (legion_laptop hwmon) |
| `loqctl/ui_fans.py` | Fan curve panel of the dashboard |
| `loqctl/curvestore.py` | Stock and saved fan curves, `active.json` and the serve re-apply |
| `loqctl/ui.py`, `loqctl/colors.py` | Curses dashboard and ANSI color helpers |

Why Python and the standard library: [ADR-001](docs/adr/001-language.md).

## License

GPL-3.0-or-later, see `LICENSE`. Third-party credits are in `NOTICE`.
