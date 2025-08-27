# Project Zomboid - Remote Mod Watchdog
### Version 1.0
This script is to keep track of the mods installed on a remote server and to restart the server when needed.<br/>
There are some mods that supposedly do this, but they destroyed our savegames.

# How it works:

When starting for the first time:
- servertest.ini gets downloaded via sFTP
- extracting workshop_id's from servertest.ini
- retrieve `time_updated` from Steam API for each mod and store locally in a file
- generating copy&paste list for Discord in a file

Regular execution:
- Retrieve for each mod actual `time_updated` via Steam API
- Compare `time_updated` with the local file
- If current `time_updated` from Steam API is newer:
  - Send countdown via rcon that the server needs to be restarted.
  - If players are still online after countdown, they get kicked.
  - If they left earlier the server gets restart immediately.
  - To restart the server: first send ‘save’ and then, with a delay (so that the server has enough time to save), send ‘quit’ approx. `RESTART_TIMEOUT` seconds later.
  - If no players are online, it is restarted immediately.

This naturally requires that the game server restarts the PZ server independently. This is the case with [AMP](https://cubecoders.com/AMP), at least.<br/>
Everything gets logged and most files gets saved to /tmp/ (usual a ram-disk).<br>

For example, you can set `COUNTDOWN_MINUTES` to 20 so that all players have enough time to reach a safe place. If all players have left before the countdown expires, the server will restart immediately, as it checks the status after every minute of the countdown.

# Requirements
Requires Python >=3.10
```bash
pip install dotenv paramiko zomboid-rcon timeout_decorator
```
## .env
.env is a simple text file in your user-home-dir with each environment variable listed one per line, in the format of KEY="Value". The lines starting with # are ignored.<br/>
.env file should look like:
```txt
RCON_HOST="<IP>"
RCON_PORT=27015
RCON_PASSWORD="<PASSWD>"

SFTP_HOST="<IP>"
SFTP_PORT=22
SFTP_USER="<USER>"
SFTP_PASSWORD="<PASSWD>"
SFTP_REMOTE_FILE="/Zomboid/Server/servertest.ini"

STEAM_API_KEY="<KEY>"
# true/1/yes → uses IPublishedFileService + ?key=...
STEAM_API_USE_PFS=1
BATCH_SIZE=100
RESTART_TIMEOUT=5
```

## Steam API Key
Steam-API haves a limit of 100.000 Requests per Day. The Script haves a batching and retry logic to prevent blocking.<br/>
If you have a lot of mods, you will need a [Steam API key](https://steamcommunity.com/dev/apikey), but you can also do without it by setting `STEAM_API_USE_PFS` to 0.<br/>

## cron
To run the script automatically on a recurring basis, it should be executed via crontab.<br/>
Every 10 minutes should be sufficient; with many mods, if the interval is too short, there is a risk of being blocked by the Steam API.
```cron
*/10 * * * * /usr/bin/python3 /path/to/script.py >/dev/null 2>&1
```

## CLI Commands:
I've also added some Commands for the Script:<br/>
| Command | Description |
| --- | --- |
| `--get_serverini` | Download server.ini and update local mod's time_updated file. |
| `--msg "message"` | Send Server Message to all Players. |
| `--test` | Only run a test without restarting Server. |

## cron usage:
```cron
*/10 * * * *   /usr/bin/python3 /home/gameserver/PZ-Remote_Mod_Watchdog.py >/dev/null 2>&1
0 0 * * *      /usr/bin/python3 /home/gameserver/PZ-Remote_Mod_Watchdog.py --get_serverini >/dev/null 2>&1
```
Last is only needed if you change your mods more often ;) Usual i run this manually.
