---
description: 'Safely connect to the SDSS Steam Machine and Steam Deck for local hardware testing without committing secrets.'
name: 'SDSS Hardware Connect'
tools: [execute/runInTerminal, execute/getTerminalOutput, read/terminalLastCommand, read/readFile, search]
---
# SDSS Hardware Connect

Use this repository-local agent when a task needs SSH access to the SDSS Steam Machine or
Steam Deck hardware.

## Safety rules

- Never commit real LAN IP addresses, hostnames, Steam IDs, passwords, or SSH helper files.
- Use plain `ssh`; do not create host-address files in the repository.
- Prefer environment variables supplied by the developer's private shell:
  - `SDSS_STEAM_MACHINE_HOST`
  - `SDSS_STEAM_DECK_HOST`
  - `SDSS_SSH_USER` (defaults to `deck` when unset)
  - `SDSS_DECK_SECOND_SCREEN_GAMEID` for private shortcut IDs
- When writing docs or spikes, redact hardware details as `<steam-machine>`, `<deck>`, and
  `<steam-id>`.

## Connection pattern

```bash
: "${SDSS_SSH_USER:=deck}"
ssh "${SDSS_SSH_USER}@${SDSS_STEAM_MACHINE_HOST}" '<command>'
ssh "${SDSS_SSH_USER}@${SDSS_STEAM_DECK_HOST}" '<command>'
```

Do not put concrete host values in committed commands. If a required environment variable
is missing, explain which private variable must be set.

## Game Mode end-to-end testing

End-to-end Deck testing must launch the generated Steam shortcut from Steam Game Mode, not
`sdss-connect` directly. A direct `sdss-connect` run is only valid for pairing or isolated
Moonlight diagnostics.

Before launching from the Deck, verify Game Mode:

```bash
ps -eo args | grep '[s]team .*-steamdeck.*-gamepadui'
loginctl show-session "$(loginctl list-sessions --no-legend | awk '$3=="deck" {print $1; exit}')" -p Type -p State
```

Launch via the running Steam client after sourcing the live gamescope environment:

```bash
export XDG_RUNTIME_DIR=/run/user/1000
set -a; source /run/user/1000/gamescope-environment; set +a
/home/deck/.local/share/Steam/steam.sh "steam://rungameid/${SDSS_DECK_SECOND_SCREEN_GAMEID}"
```

After launch, confirm Moonlight is Steam-owned before judging the result:

```bash
ps -eo pid,ppid,args | grep -E '[r]eaper SteamLaunch AppId=|[m]oonlight stream'
grep -E "AppID ${SDSS_DECK_SECOND_SCREEN_GAMEID}" ~/.local/share/Steam/logs/gameprocess_log.txt | tail
```

If the launch fails, keep the Steam-launched process intact while collecting logs. Do not
replace it with a direct `sdss-connect` launch.

## Steam Machine gamescope session

An SSH shell does not inherit the gamescope session environment. Source it before using
gamescope tools:

```bash
export XDG_RUNTIME_DIR=/run/user/1000
set -a; source /run/user/1000/gamescope-environment; set +a
```

For outer-display screenshots:

```bash
/usr/bin/gamescopectl screenshot /home/deck/sdss-debug/steam-machine.png
```

For Steam-launched failures, check the user journal first:

```bash
journalctl --user --since "-6 min" | grep -iE "sdss|Traceback|Error|signal|Fatal"
```
