<h1 align="center">🏕️ DSTCamp</h1>

<p align="center">
  <strong>An all-in-one Windows manager for local <em>Don't Starve Together</em> servers</strong><br>
  Create worlds, configure Mods, run dedicated servers, back up saves, tunnel, and accelerate the lobby — all from one GUI.
</p>

<p align="center">
  <a href="https://github.com/chengzhirenchaoshuai/DSTCamp/releases"><img alt="Release" src="https://img.shields.io/github/v/release/chengzhirenchaoshuai/DSTCamp?color=orange"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="UI" src="https://img.shields.io/badge/UI-Qt%20(PySide6)-41cd52">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-informational">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
</p>

<p align="center">
  <a href="#-download-and-run">Download</a> ·
  <a href="#-features">Features</a> ·
  <a href="#-storage-layout">Storage</a> ·
  <a href="#-development">Development</a> ·
  <a href="#179-changes">Changelog</a> ·
  <a href="README.md">中文</a>
</p>

---

## 🧭 Features

| | Module | Capabilities |
|:-:|---|---|
| 🖥️ | **Local server** | Start/stop/restart, console, announcements, player list, rollback, port preflight, crash auto-restart, runtime and log diagnostics, LuaJIT acceleration |
| 🧩 | **Mods** | Steam/WeGame Mod scanning, toggling and configuration, presets, Workshop updates and status, V1 Legacy deployment, stale-copy cleanup, missing-Mod checks |
| 🌍 | **World settings** | Independent Forest/Caves settings, icons and value labels, Island Adventures / Porkland / Beneath the World Below worlds |
| ⚙️ | **Server config** | `cluster.ini`, `server.ini`, shared token pool, administrators and blocklist (pick from players in your saves) |
| 💾 | **Saves** | Player status and Klei IDs, manual/automatic backup and restore, offline world reset, copy to a server save, package for sharing, create/delete saves |
| 🌐 | **Tunneling & acceleration** | SakuraFrp, Lolia, self-hosted frps (one-click SSH deploy), Mihomo TUN + WireGuard lobby acceleration with route diagnostics |

Also included: Chinese/English UI, six themes, three fonts with four size levels, custom backgrounds, lite mode, system tray, remembered window size and position, and verified auto-update.

> [!NOTE]
> WeGame does not provide one-click dedicated-server launching. DSTCamp does not bypass platform restrictions.

## 📦 Download and run

Download from [GitHub Releases](https://github.com/chengzhirenchaoshuai/DSTCamp/releases) or [Gitee Releases](https://gitee.com/orange-blade/DSTCamp/releases):

| File | Description |
|---|---|
| `DSTCamp-<version>.exe` | Single-file build, no installation needed; rarely used tools such as frpc/frps and the SakuraFrp frpc are downloaded on first use |
| `DSTCamp-<version>.sha256.json` | Publisher-signed file sizes and SHA-256 hashes for the updater and manual verification |
| `*.hdiff` | Incremental patches from recent versions, used by auto-update |

Users on 1.4.0 or later can update in place from the About dialog or the update prompt at startup. On a poor connection, download the full, patch or tools package from the cloud drives listed under "About → Manual Update" and click "Import from File".

> **Nothing happens on double-click?** Windows may block files marked as downloaded from another computer: right-click the EXE → Properties → check "Unblock" at the bottom → OK, then double-click again. After one successful launch, the app clears this mark automatically.

To run from source (for development; a plain wheel does not include `icons/` and `tools/`):

```powershell
pip install -e .
python -m dstools.qt.app
```

## ☁️ Cloud servers without the Steam client

If you host locally with the Steam client, you can skip this section -- none of it shows up in the UI.

On a small Windows cloud server where you'd rather not keep the Steam client running, pick "Host on a cloud server" on first launch to use the bundled SteamCMD (change it later under "Settings → Hosting Setup..."). SteamCMD then installs and updates the Dedicated Server and downloads, updates and cleans up Workshop Mods (download by ID, Mod update check before launch), anonymously or with a Steam account (only the username is saved). The "SteamCMD" row on the Local Server page shows the login status; click "Settings" to use another steamcmd.exe or sign in.

## 🗂️ Storage layout

Bundled, read-only resources used on every start:

```text
icons/   Window and UI icons, world-setting icons, official character avatars, recommended-Mod icons
tools/   ktech (texture conversion), incremental patch tool, bundled fonts (frpc/frps, SakuraFrp frpc and the VC++ 2013 runtime are downloaded to data/tools on demand)
```

Runtime data lives in `%APPDATA%/DSTCamp/`:

```text
settings.json   All user settings
cache/          Generated on demand, safe to clear: Mod metadata/versions/translations/icons, character and world-Mod icons, parsed logs, ktech runtime copy
data/           Kept: custom background, player registry, port backups, auto-restart log, update packages, on-demand tools and resident tool copies, frpc configs
security/       Credentials: SSH keys and host trust, WireGuard/Mihomo configs, Lolia sign-in tokens
```

`cache/` can be moved under Settings → Cache folder (ASCII-only path). Clearing it never touches `data/` or `security/`.

## 🛠️ Development

```text
dstools/qt/            Qt UI: app.py entry, window.py main window, pages/ tabs, dialogs.py, theme.py;
                       feature dialogs and panels live in mod/, world/, tunnel/ (tunneling) and server/ (console)
dstools/features/      UI-independent business logic per feature: mod, world, local_service, save_browser,
                       cluster_config, sakura, lolia, frp_selfhost
dstools/shared/        Cross-feature infrastructure: paths, settings, Lua/INI parsing, ports, palettes and font styles
dstools/i18n/          Chinese/English strings (strings.py is the single source)
scripts/               run_gui.py release entry, build_exe.py packaging, sync_gitee_releases.ps1
tests/                 Script-style regression tests (no pytest)
```

```powershell
python tests/run_all.py             # discover tests/test_*.py and run each in an isolated subprocess
pip install -e ".[build]"
python scripts/build_exe.py         # stage allowlisted resources in build/, write the EXE and sha256.json to release/, smoke-test it
```

`tests/_harness.py` runs every `test_*` function in definition order, so new tests need no registration. Real Windows GUI, Steam, frpc, and game behavior still require manual validation before release.

## 1.7.9 changes

### ✨ New features

- **Reset World Offline**: the Save Info tab gains "Reset World Offline", which resets worlds without starting the server: it backs up first, then deletes each world's session saves, and the game generates new worlds from the current world settings on the next start. World settings, Mods and server configuration are kept.
- **Lite mode**: the Settings menu gains "Lite mode (for cloud servers)", which hides the background image, text outlines and window ornaments, skips toast fade animations and doesn't pre-warm the Mod cache at startup. When running on a server edition of Windows or in Remote Desktop, DSTCamp suggests it once after launch.

### ⚡ Performance

- Faster startup and lower resident memory: the World, Mod, Server Config and Save Info tabs are built the first time you open them; only the Local Server and tunneling tabs are built at startup.
- Lower idle CPU and memory: the settings file is cached by modification time, Local Server status polling is split into fast and slow paths and slows down while the window is minimized, status labels restyle only when they change, and bundled fonts load on demand.
- The server console keeps 5,000 lines per world instead of 20,000 (about 27 MB per 20,000 lines measured); the full log stays in server_log.txt.

### 🐞 Fixes

- Local Mods placed manually in the mods folder (such as Chinese-Pro) were read as Workshop Mods, so Mods that detect their source from the folder name showed the wrong name.

<details>
<summary><strong>Earlier versions (1.3.5 ~ 1.7.8)</strong></summary>

### 1.7.8

#### ✨ New features

- **Hosting setup**: on first launch, choose "Host on my own PC" or "Host on a cloud server" (changeable later in "Settings → Hosting Setup..."); it now decides the server program and channel. The "Auto" server program option, "Settings → Advanced", and the SteamCMD recommendation banner are removed.
- **Custom server save directory**: the Settings menu gains "Server Save Folder..." to set it, restore automatic detection, or open it; servers also start when saves are outside the default location, including on a different drive from "Documents". Accounts and local saves are still read from the game's default location.
- The "Switch Program" window uses card layout, the install guide is simpler, and F5 re-detects; "Add Mod Junction" is hidden when hosting with the Bundled Server.
- Log bundles include client logs from the hosting PC covering the same time period.

#### 🐞 Fixes

- The three-in-one Mod moved to "four Mods + a standalone patch (3826855460)" and no longer worked the old way: enabling the patch now brings in Island Adventures Core, Shipwrecked, Above the Clouds, and their patches, with five world types and a five-shard layout.
- With no server program found, clicking "Start" or "Switch Program" raised an error instead of showing the install guide.
- Saving settings failed when the file was briefly locked (WinError 5), interrupting actions such as switching scenarios.
- Message boxes with many buttons cut off their text; some Mod configuration group titles were not translated (e.g. Uncompromising Mode's Mod Compatibility).

### 1.7.7

#### ✨ New features

- The Mod list shows Chinese names provided by Chinese++ Pro; search matches both the Chinese and original names.
- Log bundles include frpc output and note each world's tunnel type, to help diagnose disconnects.

#### 🐞 Fixes

- Dedicated servers installed in a Steam library were mistaken for SteamCMD installs.
- SteamCMD updates stayed stuck after a 0x6 failure: the stuck manifest is now cleared before the next update, keeping downloaded progress.
- Mod configuration options translated by Chinese++ Pro were missing the original name and showed an empty version.
- After cleanup tools deleted temporary files, background tasks kept popping up "Failed to start embedded python interpreter!"; a banner now asks you to restart DSTCamp.

Thanks to @Lovedmo for fixing the SteamCMD issues (#3).

### 1.7.6

#### ✨ New features

- **Unsaved-change reminders and Discard changes**: the Mod and World Settings pages mark unsaved changes with a dot and a note, and gain "Discard changes"; "Apply" in a Mod's configuration is now written together with "Save Changes".
- **Clearer token messages**: registration conflicts are explained in plain words with action buttons; a save's own new token is added to the token pool on start, and you are prompted to add spare tokens when none are left.
- **Better direct-connect codes**: the copy notice appears next to the code, tooltips show the full code, and the code descriptions are rewritten; node pickers suggest choosing a node close to you and your friends.
- **WorldGate check is now a gentle notice** instead of a dialog that blocks starting.
- **Preset buttons moved** to the bottom left; "Apply Preset" is renamed "Load Preset".
- **The Mod page's "Custom" filter is renamed "Local Mods"**.
- **World settings verified for new Mod versions**: Island Adventures Core 1.0.80, Shipwrecked 1.0.63, Beneath the World Below 0.4.16.6, three-in-one 1.2.8.

#### 🐞 Fixes

- With Island Adventures or the three-in-one Mod enabled, starting wrongly warned that some worlds could only be entered through WorldGate.
- Turning off a Mod made the "Disable client mods" row jump to the top of the list.
- Window ornaments appeared before the main window when opening.
- In the English UI, "Set" and "cip.cc" ran together in the public network notice.

### 1.7.5

#### ✨ New features

- **World presets**: the World Settings page gains "Save as Preset" and "Apply Preset", and the world tab of the create-save wizard gains "Apply World Preset" and "Save as World Preset", shared with the custom presets on the game's world creation screen (Chinese names show up in the game too).
- **Clearer LuaJIT install result**: after a successful install, the log ends with a prominent "Installed" message and when it takes effect, and focus moves to the confirm button.
- **Old vs. new tokens explained**: the global token window explains that old tokens can be used by several saves at once, while a new token works for one save at a time and must be released by Klei after an abnormal exit.

#### 🐞 Fixes

- Installing the acceleration patch failed with "complete Workshop companion Mod injector files not found" (now supports the new Workshop layout of companion Mod 3.3.x).
- The VC++ runtime needed by the patch could only be downloaded as 14.44 while 14.51 or later is required, so it still reported the runtime missing after installing (now downloaded from Microsoft's latest-version link; the mirror link is updated to 14.51 as well).
- The first runtime download failed with "No such file or directory", and when DSTCamp was started from PowerShell 7 the runtime's digital signature was wrongly reported as missing.

### 1.7.4

#### ✨ New features

- **Choose remote ports for mappings**: when enabling Sakura or Lolia (login) mapping, each world can get a remote port (blank = assigned automatically); re-enabling with the same port keeps the direct-connect code unchanged. The disable-mapping confirmation lists the current remote ports and explains that simply stopping the server does not require disabling mapping.
- **Download size in the update prompt**: shows the patch size (with the full package size) when an incremental patch applies, otherwise the full size; if the update is already downloaded in the background, it says it can be installed right away.
- **World settings kept up to date**: Caves gains the game's new "Hound Batilisks" and "Rock Masters" settings; Beneath the World Below is updated for 0.4.16.5; the World Settings page warns when a Mod's version differs from the one DSTCamp was verified against.

#### 🐞 Fixes

- If one world failed while enabling Sakura or Lolia mapping, mapping was left half-enabled (new tunnels are now removed and the original ports restored).

### 1.7.3

#### ✨ New features

- **Disable client mods**: the Mod list of dedicated-server saves gains a "Disable client mods" row (with its own icon) that toggles like any other Mod; "Save Changes" writes it to all worlds, and "Apply to Current Shard Only" writes only the current one. After the next start, joining players' local client mods are temporarily disabled. It is hidden for local saves and on the client mods tab.

### 1.7.2

> When auto-updating from 1.7.1 or earlier, the replacement script from the old version is still used, so occasionally clicking "Restart" does not start the new version or replace the program: just reopen DSTCamp and update again (the downloaded file is reused), or download the full package from the cloud drive and install it via "About → Manual Update → Import from File".

#### ✨ New features

- **Smaller installer**: the bundled frpc, frps and SakuraFrp frpc are now xz-compressed (about 7 MB smaller); the unpacked files are byte-for-byte identical.
- **Smaller installer**: three oversized recommended-Mod icons are scaled down to 256 px (about 2.7 MB smaller) and still look sharp on high-DPI screens.
- **Smaller installer**: frps (Linux amd64/arm64), the SakuraFrp frpc and the VC++ 2013 runtime installer are no longer bundled (about 21 MB smaller) and are downloaded on first use (VC++ from Microsoft, the rest from the tools release on Gitee/GitHub, verified against built-in SHA-256 hashes); SakuraFrp asks before downloading its client on first use and continues automatically; one-click frp deployment downloads frps automatically and lets the server download it itself if that fails.
- **Smaller installer**: the frpc used by self-hosted nodes and Lolia is no longer bundled (about 4.7 MB smaller); it is downloaded in the background when a server starts or a mapping is enabled and then started automatically, without any dialog; the status row shows "Downloading client", and on failure "Download Client" retries; a copy unpacked by an older version is reused, so upgrading needs no new download.
- **Cloud drive fallback for tool downloads**: when a download fails, DSTCamp offers to open the tools package on Lanzou (the password is copied automatically); import it via "About → Manual Update → Import from File".
- **Faster, more reliable auto-update**: new versions are pre-downloaded in the background (not when "Update reminders" is off), so "Update now" is usually instant; if the main source fails the other one is used (Gitee ↔ GitHub); interrupted downloads resume after a restart.
- **Incremental updates**: future versions ship a binary patch for each of the last few versions, so auto-update downloads only the patch and rebuilds the new version locally (measured: 6.9 MB patch for 1.6.0→1.7.1 versus an 89 MB full package), verified against the new version's SHA-256; if a patch does not apply, the full package is downloaded instead.
- **Offline update packages**: "About → Manual Update" adds "Import from File", which verifies and installs a full package, patch package or tools package downloaded from a cloud drive, with no manual file replacement; failed auto-updates suggest the offline package. The update manifest is now signed by the publisher, and only validly signed versions are installed.
- **Automatic runtime install for LuaJIT**: installing the LuaJIT patch now checks for the VC++ 2015-2022 x64 runtime; if it is missing or too old, DSTCamp offers to download it from Microsoft (verifying Microsoft's digital signature), installs it, and continues installing the patch; a cloud drive is offered if the download fails.
- **A way out when frpc is blocked**: when security software quarantines or blocks the frpc of self-hosted nodes or Lolia, the status row says "Blocked by security software" and the button becomes "Resolve Block", which opens the Windows Defender exclusions window and restarts frpc after it closes.
- **VC++ 2013 runtime update**: the x86 installer used by "Install runtime" is now Microsoft's current 12.0.40664, including later security fixes.

#### 🐞 Fixes

- After clicking "Restart" for an auto-update, the new version did not start and the program was not replaced.

### 1.7.1

#### ✨ New features

- **Manage Worlds**: "Manage Worlds" on the World page edits each world's WorldGate name, hidden flag, and player cap, renames world folders, and deletes non-master worlds in dedicated-server saves; a backup is made before every change.
- **Faster Mod page**: the Mod cache is warmed up in the background after launch, so 211 Mods appear in about 0.2 s (was about 1.6 s) and switching to the Mod page no longer stutters; without the VC++ runtime only icons are skipped.
- **Smarter backups**: 20 backups are kept by default; scheduled backups are skipped when the save has not changed; manual, pre-restore, and pre-Manage-Worlds backups are marked "Keep" and never deleted automatically.
- **WorldGate reminders**: adding a world says WorldGate will be enabled for all worlds; starting is blocked when extra Forest/Caves worlds exist but WorldGate is turned off.
- **V1/V2 tags in exported Mod images**: each card shows a V1/V2 tag after the version; exports from the console use the folder the server actually loaded.
- **SteamCMD entry moved**: now under "Settings → Choose Steam Client...", so players hosting locally no longer see SteamCMD options; without a Steam client (e.g. on a cloud server) a banner suggests SteamCMD.
- **UI and wording**: the "Two-way sync of mod default configs with the game" switch moves into the Global Default Configs window; the Settings menu is reordered; the "Choose Server Program" explanation is friendlier for newcomers; WorldGate in the recommended Mods gets a new icon and description.

#### 🐞 Fixes

- After adding a world, Mod worlds such as Shipwrecked and Volcano failed to generate (`TaskSet has no data`).
- Clicking "Apply" in a Mod's configuration cleared WorldGate's world names, hidden flags, and player caps.
- After restoring a backup, world folders added or renamed after that backup were left behind as extra worlds.
- After changing a local Mod's modinfo, options, or icon, refreshing the Mod page still showed the old content.

### 1.7.0

#### ✨ New features

- **SteamCMD channel**: the Local Server page's "Steam channel" switches between the Steam client and the bundled SteamCMD; SteamCMD can install and update the dedicated server and download, update, and clean up Workshop Mods, with Download by ID, a Mod update check before starting, and Steam account login (only the username is stored).
- **Add worlds to existing saves**: "Add World" on the World Settings page (dedicated-server saves only) backs up the save first and wires in WorldGate automatically; WorldGate is added to the recommended Mods.
- **Faster "Update Mods" window**: opens in about 0.35 s, search and filters are near-instant, and the first update-status check is faster.
- **More accurate Mod icon runtime check**: when it is broken, the banner names the faulty file, and clicking it installs or repairs the runtime.
- **UI details**: start buttons are disabled with a reason when no server program is found; the "Account" dropdown has a tooltip; long paths in dialogs wrap; the text outline over background images fades in only when needed.

#### 🐞 Fixes

- Vanilla Forest/Caves worlds added in the create-save wizard failed to generate (`Must specify the task set for a level!`).
- With group-pause Mods or pause-when-empty turned off, the master world stayed "not ready" after it had started.
- A broken Mod icon runtime caused repeated system error dialogs (0xc000007b).
- When creating a save, single-world saves were wrongly reported as having port conflicts, and invalid ports skipped the conflict check.
- The "Update Mods" window showed "file missing" Mods that could not be selected.
- Direct-connect code tooltips disappeared; old 16-character Sakura access keys could not be entered; the "all loaded" Mod count left out the LuaJIT companion Mod.

### 1.6.5

#### ✨ New features

- **Refreshed themes and window ornaments**: new Sky theme; Mint is renamed Nature, and Twilight is renamed Starry Night with a deeper main color; Sakura, Campfire, Nature, Sky, and Starry Night gain theme-matched window ornaments (blossoms, autumn leaves and a campfire, a nest in the grass, clouds and birds, a moonlit night sky), off by default and enabled from "Theme → Window Ornaments"; Theme menu items are now aligned.
- **Text adapts to custom backgrounds**: when the background is dark, dark text across the window switches to light text while the image itself is unchanged; clicking the opacity slider jumps straight to that position.
- **Edit server settings for local saves**: server name, description, password, max players (1–6), and PVP can be changed and are written where the game actually reads them when starting; if the game is open you are asked to return to its start screen first, and saving is blocked while the save is running.
- **Small-window and low-resolution layouts**: when the window gets short or narrow, the Local Server, Server Config, and tunnel pages switch to compact layouts or scroll instead of squashing or overlapping controls; room settings fit on one screen at 1280×720.
- **Faster Mod list loading and update checks**: parsing results are cached on disk, and checking hundreds of Mods for updates takes about one network round trip; "Update All" in the update dialog becomes "Quick Update", updating only Mods that need it and showing the count.
- **Sakura node picker**: nodes are grouped into Mainland China and Overseas/HK/Macau/Taiwan, showing live load, public-game support, and high-load markers.

#### 🐞 Fixes

- Auto-update always failed when a `DSTCamp.exe` already existed in the program folder, and a failed update silently fell back to the old version without explaining why (it now rolls back and shows the reason).
- Auto-update downloaded a new version again even if it had already been downloaded but not restarted (it is now reused when its size and SHA-256 match).
- Deleting a save left its tunnel mappings running and remote tunnels behind (they are now stopped and deleted); a new save with no mapping was reported as already mapped another way; mapping conflict messages now name the methods already used by the current save.
- Double-clicking a downloaded EXE did nothing (the download block is now cleared on startup, and the README explains it).
- Empty read-only server settings were left blank; they now show "Not generated yet" or "Not set".
- On the World Settings page, the window height stopped shrinking after the window was made narrower.
- The repository is renamed DSTCamp, with GitHub/Gitee links and the update check updated accordingly.

### 1.6.4

#### ✨ New features

- **Automatic V1 Mod folder repair**: when a V1 folder contains extra files, they are cleaned up before the server starts, with a notice and a log entry; the Workshop update dialog gains a "V1 folder has extra files" status.
- **Console banner improvements**: the Mod load failure banner shows Mod names and opens a failure list with icons, where each Mod's folder can be opened; when all Mods load, clicking the banner exports an image of the Mods the server actually loaded; crash and startup diagnostics support the English UI.
- **Recommended Mods dialog uses icon cards**: icons are sharp at any display scaling, and descriptions support English.
- **Leaner Mod update log**: each Mod shows a single "start checking" line with its name instead of repeating with download progress.

#### 🐞 Fixes

- V1 Mod folders with extra files failed to load or were skipped during deployment, and Mods re-uploaded as V2 with an old package left inside were treated as V1 (V1/V2 is now determined by the install size in Steam's manifest); temporary files were left in the Workshop folder when a V1 download failed.
- When a world that had reported a token conflict later exited, the banner did not show the actual exit reason.
- Clicking "Set Global Tokens" while creating a save did nothing and raised an error.
- Shift+Enter in log search still jumped to the next match.

### 1.6.3

#### ✨ New features

- **Multiple accounts**: all game accounts on this PC are detected and their local saves scanned; the account logged in to Steam is used by default (marked with a green dot in the dropdown). You can switch accounts in the save bar, and local saves, the status bar, the create-save wizard's nickname, and Mod default config sync all follow the current account.
- **Mods subscribed by other accounts**: the subscriber is shown and they can be force-cleaned (Steam is closed and restarted automatically; batch supported); the update window gains an account filter.
- **Editable local saves**: world settings and Mods can be changed and saved even while the game is open; saving is blocked if the save may be running, otherwise you are reminded to return to the game's "Logging in..." screen before entering the save.
- **Editable world generation settings for server saves**: after saving, you are reminded that the world must be reset for them to apply.
- **Full LuaJIT patch uninstall**: removes the copy and leftovers and disables the companion Mod; also works when the patch is already turned off.
- **UI tweaks**: the "Switch Program" window is now a single-select table that applies on Save; world console tabs sit flush on the log box, with the close badge in the top-right corner.

#### 🐞 Fixes

- Mods subscribed by other Steam accounts were deleted as leftovers and then downloaded again by Steam.

### 1.6.2

#### ✨ New features

- **Global Mod defaults**: the Mod page's former "View Local Mods" button is now "Global Default Configs". Its window matches the global Mod configuration in the game's main menu and syncs with it both ways (when both sides changed, the newer one wins and the overwritten game file is backed up first; while the game is running, writes wait until it closes; sync can be turned off in the Settings menu). In the window you can search, filter by configured/unconfigured, edit (including client-only options), restore a Mod's defaults, sync now, and see a result summary and sync log. Mods newly enabled in a save without a configuration get the defaults automatically, and a save's Mod configuration dialog can "Use default config" manually; changing a save's configuration does not change the defaults.
- **"Client Mods" filter on the Mod page**: placed after "Custom", and "All" now lists client mods too; filter buttons are sized to their text.
- **Editable Mods for Steam local saves**: reads and writes Master/save/shardindex, which the game actually uses, and also writes each shard's modoverrides.lua on save; saving is refused while the game client is running, with a prompt to exit the game first (WeGame local saves stay read-only).
- **Three-in-one mod support**: its world settings are supported; world creation recognizes the Shipwrecked, Volcano, and Porkland worlds it provides; the create-save wizard can apply the five-shard layout in one click (fixed shard IDs, no volcano island in Shipwrecked); enabling it together with the original Island Adventures or Above the Clouds Mods shows a warning and blocks creation.
- **Tropical Adventures | Ship of Theseus world settings**: supports Mod-defined groups, per-item icons, and value labels.
- **Editable world generation options in the create-save wizard**: numeric values are written as Lua numbers; Mod options registered without a value list stay read-only.
- **Use an idle token right after a crash**: when auto-restarting after a crash, if the original token is still waiting for Klei to release it and the pool has an idle token, that token is used and the server goes online right away; the replaced token becomes available again once its wait expires. "Wait before switching" now defaults to 0 minutes (0–110 allowed; raise it or turn off "Use an idle token after a crash" to save tokens). When starting manually with an unreleased original token and an idle token available, the default button is now "Use Another Token".
- **Auto-restart only restarts the crashed world**: when the overworld crashes, the caves keep running — the game pauses them and reconnects automatically, then syncs once the overworld is back, so the save stays consistent; if the caves crash while the overworld is also down, the overworld is started with them.
- **Release notes in the update prompt**: when a new version is found, the update dialog shows its release notes, styled to match the theme and font size setting; the dialog is wider accordingly.

#### 🐞 Fixes

- Saving world settings wrote booleans and numbers as strings (e.g. true became "True"), breaking Mods' option checks.
- When several Mods registered the same world setting, the one shown was not the one the game actually uses (the game keeps the first registered, by load order).
- New Porkland worlds lacked a complete level definition, so world generation failed and the server could not start.
- With only the overworld running, crash auto-restart also started the caves that had not been running.

### 1.6.1

#### ✨ New features

- **Multi-column Mod list**: the Mod lists on the Mod page and in the create-save wizard switch between 1, 2, and 3 columns (one shared, remembered setting); multi-column view is compact, with the switch and Configure button on the right of each cell.
- **New version replaces the old one**: opening a new version while an old one is still running closes the old one and starts the new one; if the old version is running dedicated servers, it asks first, then saves and stops them before exiting, leaving no server processes behind.
- **No background throttling for servers**: after a dedicated server starts, Windows power throttling is turned off for it and its priority is raised above normal, so it is no longer slowed down while DSTCamp is in the background.
- **Bundled Server LuaJIT uses the author's method**: Winmm.dll is placed directly in the game's bin64 and shared with the client, and a LuaJIT installed by the author's script is recognized; uninstalling deletes that file (the client's LuaJIT goes with it, as the confirmation explains); while the file is in use by the game or a server, the buttons are disabled with a reason.
- **More reliable direct-connect codes**: when the public or tunnel code fails or times out, it retries after 5/15/30/60 s with a countdown, and once retries run out you can click the row to retry; codes refresh when the master world becomes ready; Lolia and self-hosted mappings read local settings directly instead of being slowed by the Sakura API, and Sakura API errors show "Failed to fetch" instead of "Not mapped".
- The "Game client" server program option is renamed "Bundled Server".

#### 🐞 Fixes

- After closing to the tray, launching the app again made the window appear only on the taskbar; it could not be restored, clicked, or closed.

### 1.6.0

#### ✨ New features

- **Lolia mapping** (new tunneling sub-tab): after OAuth sign-in it shows account traffic, bandwidth limit (Mbps), and tunnel usage, and warns early when available traffic is 0 so you can check in; pick a node to create a UDP tunnel per world — worlds may sit on different nodes, each with its own frpc — and tunnels are deleted when mapping is turned off; nodes are grouped by region with traffic multiplier and high-load labels; when frpc exits, the server's failure reason is shown; if the built-in sign-in app stops working, a guide lets you use your own client_id.
- **Card-style node picker**: shared by Sakura and Lolia, with cards that grow to fit their content; the Sakura account area now matches Lolia.
- **Host with the game client**: without the dedicated-server tool installed, worlds can run on the server program bundled with the game client, with LuaJIT and Mod updates still available. The local-server page gains "Server program" (Auto / Game client / Dedicated server) and "Program location"; the Mod page's Mod location and LuaJIT status follow the current program, and "Mod sync" is disabled with an explanation in game-client mode. While a world is running, switching programs, changing the path, and verifying files are disabled, and disabled buttons explain why on hover.
- **Global font size levels** (font settings): Small / Standard / Large / Extra Large; direct-connect columns, the Start All button, and the save-type drop-down adapt to the size; the font engine is chosen per font style for crisper pixel and KN Maiyuan text (takes effect after restarting when the style changes).
- **Export Mod list image** (Mod page, next to the enabled-Mod count): a light themed banner with info tags, version badges, author and ID, and numbered corner badges; preview, save, or copy it.
- **Mod config dialog**: search options by title or comment; option descriptions now appear per item on hover in the opened list; opening the config no longer shows a loading popup, just a busy cursor.
- **Global token pool**: auto-restart after a crash reuses the original token and keeps the server running to retry on registration conflicts, with release progress in the banner; new "switch token on timeout" toggle and wait time before switching (20–110 minutes, default 30); crashes before a successful registration no longer hold a token, and expired wait marks are cleared automatically; a manual start asks first when the original token has not been released; the token pool dialog uses sectioned cards.
- **Create server save**: the new save is selected automatically and you are asked whether to start the world now; the default folder name is the first free Cluster_N; the "Create server save" button moved to the left of "Refresh".
- **World console**: rounded tabs with a running-status dot before the name and a close badge in the corner replacing the bottom "Close window" button; the log search box keeps search history.
- 19 official character portraits now ship with the app and are preferred in the save browser; unused world-setting icons were removed, shrinking the package by about 2 MB.
- Tooltips use a light-yellow background and appear after 0.1 s on hover (0.7 s for the Mod list lock switch); larger "Configure" text in the Mod list; "Ready" in direct-connect codes is green and a world's "Stopping" is red; when picking users from a save, the "Only users of the current save" switch is hidden if there are no candidates.

#### 🐞 Fixes

- With a dark system theme, log areas in dialogs such as the LuaJIT update and tooltips on drop-down list items had a black background.
- The LuaJIT install confirmation lacked the "Download VC++ 2023" link; the LuaJIT buttons were re-enabled while installing.
- Parsing crashed when a save's cluster.ini was GBK-encoded.
- The Mod page kept showing old versions after refreshing; the Mod tab of the create-save wizard stayed on "Checking version".
- Drop-down options in the Mod config dialog showed stray alignment spaces, and options with identical text could go missing.
- Switching to the tunneling tab lagged about 1 s while Lolia or self-hosted mapping was on.
- Read-only fields in the world settings (server.ini) of the server config squeezed the column, pushing labels and values to the right as the window widened.
- When a running world exited abnormally with no identifiable cause, the diagnosis was titled "Server failed to start"; it now reads "Server exited unexpectedly".
- The public direct-connect status text was cut off.
- The first character of labels and drop-downs was clipped with the pixel and KN Maiyuan fonts; KN Maiyuan strokes had uneven weight; menus did not follow the font size after a restart.

### 1.5.1

#### ✨ New features

- **Check for missing Mods before launch**: when a save enables Mods that are not on this computer (usually not subscribed, common with saves copied from elsewhere), starting the dedicated server is paused and the Mods are listed. Otherwise the server would skip them, and once the save is saved, items, creatures and structures that depend on them could be lost for good. Choose "Subscribe & Download" (subscribes with the current Steam account and waits for the downloads; the Mod page refreshes afterwards), "Start Anyway", or cancel.
- **Guided update when the server is out of date**: when a launch is blocked by a pending update, the dialog offers "Update now"; a red "Update available" appears after the dedicated-server path, with remote/local build numbers on hover.
- **Outdated LuaJIT copies update automatically**: starting, starting all, and restarting no longer ask for confirmation; launching continues once the update succeeds, and failures are explained in the progress window.
- **Create server save window**: clicking the main window while it is open now flashes its title bar and border, like other dialogs; the window remembers the size you last set.

#### 🐞 Fixes

- Mods removed with leftover cleanup were downloaded again by Steam when joining another server. Cleanup now checks Steam's download records and, after confirmation, closes Steam, clears the records, and restarts Steam.
- Unsubscribed legacy (V1) Mods were listed as cleanable but always failed to clean; batch-cleanup failures are now grouped by reason.
- V1/V2 badges in the Mod update window were unstyled and flooded the log with stylesheet parse warnings.
- "Update via Steam" showed no window after clicking, and exiting during an update hung the app.
- "Update via Steam" did not actually update (Steam ignored the request when the server was already installed).
- Drop-down lists extending outside their window were missing their background (e.g. the rollback-days list looked cut off).
- Drop-down lists briefly flashed a default background when opening.
- With a TUN-mode proxy on, the LAN direct-connect code showed a 198.18.x virtual address.
- The public direct-connect code reported the proxy's exit IP; a TUN proxy is now flagged as "Proxy?" with directions for a direct connection.

### 1.5.0

#### 🖥️ New interface

- Rebuilt with Qt (PySide6) with every existing feature kept; visuals, dialogs, font sizes, and themes are unified, and details such as copy toasts and drop-down lists were reworked.
- High-DPI aware: the default window fits the screen work area, the last window size is remembered, large dialogs no longer overflow the screen, and the window cannot be dragged off the desktop.

#### ✨ New features

- **Auto-restart on crash** (Local server page, per save): worlds that crash after starting are restarted automatically. Another usable token from the pool is used when available; otherwise the original token is retried after 5/10/15/30 minutes until Klei releases it, for up to 2 hours. It stops after 3 crashes within 30 minutes. No modal dialogs are shown; progress appears in a banner and tray notifications, and every result is logged to `data/auto_restart/auto_restart.log`.
- **Token hold expires automatically**: after a master-world crash or registration conflict, the token's "waiting for release" mark clears itself after the waiting period instead of requiring a manual release in the token pool.
- **Lobby diagnostics identify connection types**: each player is listed as Steam P2P, WeGame P2P, FRP/local loopback, LAN, or direct IP; when everyone joined directly the result says acceleration was not used. P2P sessions resumed by reconnecting players are no longer missed.
- **"Pick from save" for administrators and blocklists**: a player registry shared across saves lets you add or remove several players in one dialog; the token owner is shown as a read-only entry.
- **Klei IDs and nicknames on the save page**, identified from server logs, including renames, reconnects, and first spawns; historical log parsing is cached.
- **Sharper player avatars**: official characters use the game's high-resolution crafting-menu avatars and Mod characters prefer their save-slot portraits; avatars are still shown when a Mod's scripts are encrypted or the Mod is disabled.
- **Delete saves** to the Windows Recycle Bin; the create-save wizard can remove any world and prompts when the master world is missing.
- **Stale dedicated-server Mod copies are cleaned up automatically** when they would shadow newer Workshop versions; Mod lists and the update window show V1/V2 badges.
- **Live frpc status**: refreshed every second with the exit code shown if frpc exits unexpectedly; running/deployed states for frpc, frps, and lobby acceleration are shown in green.
- Font settings now use font cards with a preview, and every label follows the selected style.
- Server-log search is fixed and always visible, with ↑ ↓ navigation.
- New saves default to LAN-only and offline modes off; the conflict prompt between those modes and port mapping is clearer.
- The Mod configuration page no longer flashes a loading window; Mods enabled in the save but not installed locally now explain where they come from.
- The close option "Minimize to taskbar" is now "Minimize to tray".

#### 🐞 Fixes

- An occasional "Failed to remove temporary directory" dialog when the single-file build exits.
- An error when closing the window during startup.
- Crashes caused by decoding process/port information on some systems.
- "Restart now" in cache-folder settings did not restart.
- Duplicate entries in the administrator list after refreshing.
- The Gitee update source was often rejected (HTTP 403) and fell back to GitHub; checking and downloading updates is now more reliable in mainland China.

### 1.4.0

- Unified the width of several progress/log dialogs (remote deploy, WireGuard deploy, SSH auth/connection check/port mapping, Mod update log) to 100, so long lines no longer wrap awkwardly.
- Added a "Notify on update" toggle, synced between the About dialog and the update window; enabled by default, it now pops the update prompt directly when a new version is detected.
- Hardened single-value Lua parsing: function-call expressions with leftover tokens and bare identifiers are now both rejected and fall back to the raw text, preventing Mod config defaults from being misread.
- Switched frpc/sakura-frpc to gzip-compressed distribution to reduce antivirus quarantines triggered on every launch; also fixed a source-mode regression where the client was misreported as missing.
- Added a timeout watchdog for public-IP and tunnel-mapping queries that shows "failed to fetch" instead of hanging; the polling loop also gained exception isolation so one failing step can no longer stall it permanently.
- The local-server tab's direct-connect info and the server-config tab now refresh promptly after saving server/world settings or toggling a tunnel mapping, without needing to switch saves manually.
- Fixed an intermittent crash (AttributeError) from clicking a tab very early during startup.
- Fixed blurry text on the world-settings tab of the create-save wizard.
- Added a save button next to the reset-world button in the master world console.
- Fixed a column-layout glitch on the room settings page on first cold-start entry.

### 1.3.9

- Trimmed the release down to a single-file EXE; the external-tools ZIP build is no longer produced or published. Existing ZIP-version users are unaffected — auto-update switches them to the embedded EXE automatically.
- Overhauled Windows Defender exclusion handling: detection now covers three targets and falls back to an admin-level check automatically when the result is unknown, and a series of bugs are fixed — crashes on wildcard targets, garbled output, CLIXML leaking into the UI, a blank path box, and modifications being misreported as failed.
- Unified the Mod loading indicator's wording and font size between the main window and the create-save sub-window, matching the "Configure" button and Workshop link's visual style.
- Added click-to-copy for Mod names on the Mod management tab, supported in both the main window and the create-save sub-window.
- The Lua parser now tolerates the KLEI header written by TheSim:SetPersistentString; world settings now show the specific exception when reading leveldataoverride.lua fails, easing remote troubleshooting.
- Fixed a layout imbalance under high-DPI scaling by converting pixel literals using the display's DPI consistently.
- Adjusted the settings menu: moved the Windows Security exclusions entry below Language, renamed the cache directory setting, and added a "clear cache" entry to the File menu.
- Rewrote auto-update cleanup to use a fixed installer filename and self-heal leftover update files; also cleaned up zombie `tools` directories left behind by legacy ZIP-version users after updating.

### 1.3.8

- Improved the Mod configuration dialog's loading feedback and response speed; fixed the main window still lagging after closing the config dialog.
- Bounded the growth of size-keyed image caches, lowered image memory use on the world-creation window, and cleaned up stale Mod folder cache entries.
- Fixed the KN Maiyuan Rounded font's width/size inflation and adjusted short buttons to a consistent four-character visual width.
- Fixed multi-line text clipping in transparent tooltips and the save-session hint; fixed leftover state after an auxiliary button was triggered by mistake.
- Relocated the token storage path and excluded the `reference` directory.

### 1.3.7

- Virtualized world-settings viewport rendering, raised the default visible row count, and unified the world-creation settings layout; inactive tabs release long images promptly.
- Added compatibility with the "Deep Down" and "Never Compromise" Mods: vanilla settings patches, linked settings, and offline ports.
- Adapted the LuaJIT patch injection layout for the new version.
- Improved auto-update naming and progress display.
- Adjusted the Mod-enabled-count position in the status bar and relaxed admin-list user-ID validation.

### 1.3.6

- Virtualized the Mod list by visible viewport to reduce widget and image usage with large Mod libraries.
- Improved Mod icon caching and reclaimed inactive page images more promptly.
- Reduced custom-background memory usage and fixed shared-background scroll-region synchronization.
- Released long world-setting images when their tabs are inactive to limit accumulated memory use.
- Fixed the local-server console polling initialization race.
- Skipped slow tunneling initialization when unconfigured and prioritized `cip.cc` for public-IP lookup with fallbacks.

### 1.3.5

- Added Mihomo TUN/WireGuard lobby acceleration, dual download sources, route diagnostics, and public-IP fallback checks.
- Added Windows Defender exclusion management and network-state Mod recommendations.
- Fixed LAN port conflicts, command-triggered shutdown reporting, connection readiness, and active token-conflict detection.
- Added SakuraFrp client recovery guidance and refined self-hosted node status and masked-address presentation.
- Improved high-DPI configuration layouts, world settings sizing, console command history, and address visibility controls.
- Strengthened test isolation, resource boundaries, ZIP allowlist checks, and release checksum validation.

</details>

## License

[MIT](LICENSE)

Bundled fonts are distributed under their own licenses: Fusion Pixel Font and KN Maiyuan are both licensed under the SIL Open Font License 1.1; see `tools/fonts/` for the license files.
