# Auto-Proton

Automatically choose an installed Proton version **per Steam game**, using recent ProtonDB PC reports. Native Linux games keep native execution: Auto-Proton clears forced Windows compatibility overrides for them.

Runs locally on Linux with Python 3.10+ and systemd user timers. There are no pip dependencies, accounts, API keys, telemetry, root privileges, or hosted backend. Its only external runtime connection is HTTPS to `www.protondb.com`. Live mode also connects to your own Steam client over IPv4 loopback.

## Install

Download [the latest Linux release bundle](https://github.com/LennonAmos/auto-proton/releases/latest/download/auto-proton-linux.tar.gz), extract it, and run:

```sh
tar -xzf auto-proton-linux.tar.gz
cd auto-proton
sh install.sh
```

For multiple Steam installations, choose one explicitly:

```sh
sh install.sh --steam-root "$HOME/.local/share/Steam"
```

Setup installs all Auto-Proton code, two launchers, a systemd user service/timer, and a small UI companion service. It does not install or replace system packages. Python 3.10+ and a working systemd user session are prerequisites; setup checks both. Running setup again updates a recognized installation while preserving per-game preferences and ownership records. Setup refuses unrecognized destination files and rolls back its program files if installation or upgrade fails.

Monitoring starts automatically, checking installed manifests and compatibility choices approximately every two minutes while your user session is running. No linger setting or root service is added. The first scan can take a few minutes for a large library.

**Live Steam changes:** setup enables Steam's CEF debugging marker if it is absent. If debugging was not already enabled, close and reopen Steam normally once. Auto-Proton never closes or restarts Steam itself. It discovers Steam's changing debug port from this user's Steam webhelper process, selects only Steam's internal `SharedJSContext`, calls `SteamClient.Apps.SpecifyCompatTool`, and reads the result back. Connections are restricted to `127.0.0.1`; no browser pages, extensions, native library injection, or modifications to Steam’s on-disk UI files are installed. The UI companion adds its control only to internal game-properties windows, with automatic cleanup when the companion stops. The debugging interface gives local programs access to Steam's UI; do not expose it over a network.

A currently running game keeps its already loaded Proton until the next launch. If the live interface is unavailable, Auto-Proton saves its plan and applies changes once Steam is closed. It never races Steam by editing its config while Steam is running.

## Per-game control

Open a game's **Properties → Compatibility**. The Auto-Proton panel shows the current automatic recommendation.

- **Override Auto-Proton off:** automatic selection manages the game, and Steam's manual selector is disabled.
- **Override Auto-Proton on:** Steam's selector is enabled. Select a compatibility tool normally; the manual preference is saved for this game, including across restarts and updates.
- Turning the override off schedules a fresh sync and resumes automatic management. A recommendation remains visible while the override is on.

Native Linux execution is the automatic default. An explicit manual override can select a different compatibility tool. The companion restores Steam's controls when removed, and its heartbeat cleanup also handles an unexpected companion failure.

Equivalent commands are available for scripts:

```sh
~/.local/bin/auto-proton override --appid APPID --mode manual
~/.local/bin/auto-proton override --appid APPID --mode auto
~/.local/bin/auto-proton sync
```

[Project website](https://lennonamos.github.io/auto-proton/) · [Releases and checksums](https://github.com/LennonAmos/auto-proton/releases)

## Inspect or run manually

```sh
~/.local/bin/auto-proton status
~/.local/bin/auto-proton scan --json
~/.local/bin/auto-proton sync
~/.local/bin/auto-proton sync --appid 1222140
~/.local/bin/auto-proton scan --offline
systemctl --user status auto-proton.timer auto-proton.service
```

`scan` reads Steam and writes only Auto-Proton's plan/cache; `sync` also applies settings. `--offline` requires fresh local cached reports; it never silently uses expired evidence. `status --json` shows the last plan and sync result. Launchers work by absolute path even if `~/.local/bin` is not on your PATH.

Without systemd, use `python3 -B auto_proton.py scan` or `sync` from this directory. The runtime has no dependency on the installer.

## Selection rules and limits

- Scan Steam's local library list, installed app manifests, and binary `appinfo.vdf` (versions 39–41). Native support comes from local Steam OS metadata, so no Steam web API is needed. Missing metadata means no change. Steam tools, runtimes, DLC, and incomplete installations are excluded.
- Discover official tool IDs from Steam's local tool registry and installed manifests, and custom IDs from `compatibilitytool.vdf`. Preserve custom ID capitalization. Never download Proton automatically. If a required version is absent, leave the game unchanged and expose the vote/skip reason in the plan.
- Fetch the newest page of up to 40 PC reports per game from ProtonDB's public website JSON. Reports are cached for 24 hours; discovery counts for one hour. These website data routes are undocumented and may change. Errors or a changed format produce a visible skip reason, never a fabricated recommendation. No alternate data hosts are contacted.
- Consider reports from the last 180 days, count each contributor once per candidate, and match GPU vendor when exactly one local vendor is detected. On hybrid/unknown systems, use the broader PC sample and label `gpu_vendor` as `unknown`.
- Require at least two independent positive reports without reported stability/significant-bug faults or explicit required tweaks. Any explicit failure for that candidate in the sample vetoes it. Reports with launch options are not evidence for an automatic out-of-box choice. Report text is never executed, and launch options are never copied.
- Prefer the candidate with the most qualifying successes. A newer numbered stable branch breaks an equal-support tie. GE builds require the exact reported release to be installed; a newer GE build is never assumed compatible. Official versions map to Steam's installed branch (for example, a report for 10.0-3 supports selecting the installed `proton_10` branch, which Steam may have updated to 10.0-4). Experimental also changes over time. This selects a reported-working **branch**, not a guarantee that today's build works on your hardware.
- For native Linux games, remove forced Proton/custom Windows compatibility overrides and leave native Steam runtime selection alone. If Windows depots were previously installed, Steam may need to download the native files. Global defaults are never changed.
- Existing manual changes are recognized for Windows games. The explicit per-game switch takes precedence: enable it to keep a manual choice or disable it to resume automatic management. Preferences survive an in-place update.
- Removed games have only Auto-Proton's unchanged owned override restored to its original setting. If any library is unmounted, removal cleanup is skipped to avoid confusing a disconnected drive with deleted games.

ProtonDB does not publish a universal best-version recommendation. This is a conservative local inference from a limited community sample. A Proton change cannot fix every driver, GPU, anti-cheat, runtime, game-file, or prefix problem.

## Remove permanently

```sh
~/.local/bin/auto-proton-uninstall
# Or, from this downloaded repository:
sh uninstall.sh
```

Removal stops **only Auto-Proton's own** timer, sync service, and UI companion and deletes its installed program files, launchers, units, caches, plans, per-game override preferences, locks, backups, ownership records, and installation receipt. It removes the CEF marker only if Auto-Proton created it and the marker is still an empty regular file; a pre-existing marker is left alone. An already running Steam debugging listener remains until you next close Steam normally. The companion removes its panel and restores the manual Steam controls. Its UI watchdog also removes the panel if the companion disappears unexpectedly.

**Uninstall does not change Steam's compatibility choices.** The choices already applied stay in place, and nothing continues monitoring. To undo Auto-Proton's owned choices first, explicitly run:

```sh
~/.local/bin/auto-proton restore
~/.local/bin/auto-proton-uninstall
```

Restore preserves subsequent manual overrides; it changes only choices that still match Auto-Proton's last applied values. Offline changes have a durable backup and transaction record; live changes have a per-game transaction record and read-back verification. Rejected live selections are restored to their previous selection when the connection remains available. Interrupted or conflicting transactions stop further changes for review.

The downloaded source checkout, your Python/systemd installation, existing Proton builds, games, save data, and shared operating-system journal history are not installed Auto-Proton components and are left alone. Delete the downloaded source folder if you no longer want it. Steam prefixes and shader caches are never deleted.

## Installed files

Default locations (XDG overrides are recorded in the installation receipt):

| Location | Contents |
| --- | --- |
| `~/.local/share/auto-proton/` | Python code and installation receipt |
| `~/.local/bin/auto-proton` | CLI launcher |
| `~/.local/bin/auto-proton-uninstall` | Removal launcher |
| `~/.config/systemd/user/auto-proton.service` | Periodic sync job |
| `~/.config/systemd/user/auto-proton.timer` | Two-minute timer |
| `~/.config/systemd/user/auto-proton-ui.service` | Removable Compatibility-tab control |
| `~/.local/state/auto-proton/` | Plan, ownership, lock, transactions, last offline backup |
| `~/.cache/auto-proton/` | ProtonDB JSON cache |
| `STEAM_ROOT/.cef-enable-remote-debugging` | Only created if missing; owned creation recorded |

Steam file edits, when Steam is fully closed, are limited to individual `CompatToolMapping` entries in `config/config.vdf`. Unrelated bytes, comments, global defaults, and all other Steam files are preserved. Writes use atomic replacement, a backup, concurrent-change detection, and repeated Steam-process checks. A tiny process-start race remains between the final check and file replacement; live mode avoids config writes entirely.

## Development

```sh
python3 -B -m unittest discover -s tests -v
sh -n install.sh uninstall.sh
```

Tests use temporary files and mocked Steam/systemd calls. They cover parser versions, report ranking, native exclusion, missing libraries, config preservation, manual edits, interrupted transactions, live read-back/rollback, WebSocket masking/fragmentation, local endpoint restrictions, and complete installation/removal with setup-failure rollback, explicit per-game overrides, and resuming automatic selection. GitHub Actions runs the suite on Python 3.10, 3.12, and 3.13.

## Sources and licensing

Independent of Valve and ProtonDB. Code: MIT; see [LICENSE](LICENSE).

- [ProtonDB](https://www.protondb.com/) provides the community reports. Data licensing and schema notes: [ProtonDB data project](https://github.com/bdefore/protondb-data). ProtonDB database data is ODbL; individual contents are DbCL. No community dataset is bundled or published here; fetched reports remain in the user's local cache.
- [Valve Proton](https://github.com/ValveSoftware/Proton) documents the compatibility layer and runtime options.
- [SteamAppInfo](https://github.com/SteamDatabase/SteamAppInfo) documents the local metadata format through its parser implementation.
