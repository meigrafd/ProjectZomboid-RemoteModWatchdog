#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
 ProjectZomboid - Remote Mod Watchdog
 :copyright: (c) 2025 by meigrafd: https://github.com/meigrafd/ProjectZomboid-RemoteModWatchdog/
 :license: GPL-3.0, see LICENSE for more details.
"""

import os
import sys
import json
import time
import logging
import asyncio
import argparse
import paramiko
import requests
import subprocess
import timeout_decorator
from pathlib import Path
from dotenv import load_dotenv
from zomboid_rcon import ZomboidRCON
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from timeout_decorator import timeout, TimeoutError
from typing import List, Dict, Tuple, Iterable, Optional, Any

load_dotenv()

# Steam Web API key / endpoint selection from .env
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
STEAM_API_USE_PFS = os.getenv("STEAM_API_USE_PFS", "false").lower() in ("1", "true", "yes")
STEAM_API_URL_SR = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
STEAM_API_URL_PFS = "https://api.steampowered.com/IPublishedFileService/GetDetails/v1/"

# RCON configuration from .env
RCON_HOST = os.getenv("RCON_HOST", "")
RCON_PORT = int(os.getenv("RCON_PORT", ""))
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")

# SFTP configuration from .env
SFTP_HOST = os.getenv("SFTP_HOST", RCON_HOST)
SFTP_PORT = int(os.getenv("SFTP_PORT", 22))
SFTP_USER = os.getenv("SFTP_USER", "")
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD", "")
SFTP_REMOTE_FILE = os.getenv("SFTP_REMOTE_FILE", "")
LOCAL_SERVER_INI = Path(f"{Path().resolve()}/{Path(SFTP_REMOTE_FILE).name}")

BATCH_SIZE = int(os.getenv("BATCH_SIZE", 50))  # You can adjust this value based on API limits
RESTART_TIMEOUT = int(os.getenv("RESTART_TIMEOUT", 5))  # Seconds before quit, give server time to save
COUNTDOWN_MINUTES = int(os.getenv("COUNTDOWN_MINUTES", 5))  # Minutes how long the countdown should be
WARNING_MESSAGE = "[SERVER] Restart in {minutes} minutes due to a mod update!"
RESTART_MESSAGE = "[SERVER] Server is restarting now due to a mod update! Please disconnect within {seconds}sec or get kicked!"

DISCORD_MODLIST_FILE = "/tmp/discord_modlist.txt"
LOCAL_MODINFO_FILE = "/tmp/modInfos.json"
DEFAULT_WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id="

# Configure logging with RotatingFileHandler and console output, filename corresponds to script name
script_name = os.path.splitext(os.path.basename(__file__))[0]
log_file = f'/tmp/log.{script_name}'
# Handler for writing WARNING and above messages to a rotating log file
log_handler = RotatingFileHandler(log_file, maxBytes=2 * 1024 * 1024, backupCount=3)
log_handler.setLevel(logging.WARNING)  # Only WARNING and higher levels
log_format = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
log_handler.setFormatter(log_format)
# Handler for printing INFO and above messages to the console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)  # INFO and higher levels
console_handler.setFormatter(log_format)
# Configure the logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # Receive all messages, filtering is done by handlers
logger.addHandler(log_handler)
logger.addHandler(console_handler)

# PID file to prevent multiple executions
PID_FILE = f'/tmp/pid.{script_name}'


def check_required_env():
    required_vars = ["RCON_HOST","RCON_PORT","RCON_PASSWORD","STEAM_API_USE_PFS","SFTP_PORT","SFTP_USER","SFTP_PASSWORD","SFTP_REMOTE_FILE"]
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)


def check_pid():
    """Checks if the script is already running based on PID file."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            # Check if process is still running
            if os.path.exists(f'/proc/{pid}'):
                logger.info(f"Script is already running under PID {pid}. Exiting.")
                sys.exit(0)
            else:
                # Previous PID file is stale
                pass
        except Exception as e:
            logger.error(f"Error reading PID file: {e}")
    try:
        with open(PID_FILE, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logger.error(f"Failed to write PID file: {e}")
        sys.exit(1)


def remove_pid():
    """Removes the PID file."""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception as e:
        logger.error(f"Failed to remove PID file: {e}")


def send_rcon_message(rcon: object, message: str, test_mode: Optional[bool]=False) -> None:
    """Send a server message, appending '(TEST)' if in test mode."""
    try:
        if test_mode:
            message += " (TEST)"
        logger.info(f"Sending server message: {message}")
        command = rcon.command(f"servermsg \"{message}\"")
    except Exception as e:
        logger.error(f"Error sending server message: {e}")


async def send_manual_message(message: str) -> None:
    """Send a manual message to all players."""
    rcon = ZomboidRCONient(ip=RCON_HOST, port=RCON_PORT, password=RCON_PASSWORD)
    send_rcon_message(rcon, message)


def get_connected_players(rcon) -> List[str] | None:
    players: List[str] = []
    cmd = rcon.command("players")
    players_raw = cmd.response
    for line in players_raw.splitlines():
        if line.strip() and not line.startswith("Players connected"):
            player_name = line[1:].strip()
            players.append(player_name)
    return players


def kick_all_players(rcon, players: List[str]) -> None:
    """Kick all connected players."""
    logger.info("Kicking all players before restart...")
    try:
        for player in players:
            logger.info(f"Kicking player: {player}")
            rcon.command(f"kickuser {player}")
    except Exception as e:
        logger.error(f"Error while kicking players: {e}")


async def warn_and_restart(test_mode: bool) -> None:
    """Warn players and restart the server."""
    async def saveAndQuit(rcon):
        rcon.command("save")
        await asyncio.sleep(RESTART_TIMEOUT)
        rcon.command("quit")
    try:
        rcon = ZomboidRCON(ip=RCON_HOST, port=RCON_PORT, password=RCON_PASSWORD)
        # Get connected players and restart without countdown if theres non
        players = get_connected_players(rcon)
        if len(players) == 0 and not test_mode:
            logger.info("No players online. Saving world and quit.")
            saveAndQuit(rcon)
        else:
            # Countdown warnings
            _playersGone = False
            logger.info("Countdown for Server Restart...")
            for minutes_left in range(COUNTDOWN_MINUTES, 0, -1):
                send_rcon_message(rcon, WARNING_MESSAGE.format(minutes=minutes_left), test_mode)
                await asyncio.sleep(60)
                players = get_connected_players(rcon)
                if not players:
                    logger.info("No players left. Saving world and quit.")
                    _playersGone = True
                    break
            if not _playersGone:
                # Send restart message
                send_rcon_message(rcon, RESTART_MESSAGE.format(seconds=RESTART_TIMEOUT*2), test_mode)
                if not test_mode:
                    await asyncio.sleep(RESTART_TIMEOUT*2)
                    players = get_connected_players(rcon)
                    if players:
                        kick_all_players(rcon, players)
                        await asyncio.sleep(2)
                    logger.info("Saving world and quit.")
                    saveAndQuit(rcon)
            elif not test_mode:
                logger.info("Saving world and quit.")
                saveAndQuit(rcon)
    except Exception as e:
        logger.error(f"Error during restart: {e}")


def sftp_download(remote_file: str, local_file: str) -> bool:
    """Download a file via SFTP."""
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sftp_client = None
    logger.info(f"sFTP: Downloading {SFTP_REMOTE_FILE}.")
    try:
        ssh_client.connect(SFTP_HOST, SFTP_PORT, SFTP_USER, SFTP_PASSWORD)
        sftp_client = ssh_client.open_sftp()
        sftp_client.get(remote_file, local_file)
        logger.info(f"sFTP: Successfully downloaded {SFTP_REMOTE_FILE}.")
        return True
    except Exception as e:
        logger.error(f"sFTP: Error downloading {SFTP_REMOTE_FILE}: {e}")
        return False
    finally:
        if sftp_client:
            sftp_client.close()
        ssh_client.close()


def read_enabled_mods(server_config: Path) -> (List[str], List[str]):
    """Read enabled mods and workshop IDs from server.ini."""
    enabled_mods: List[str] = []  # title's
    enabled_workshop_ids: List[str] = []  # id's
    try:
        with open(server_config, 'r', encoding='utf-8') as f:
            for line in f.readlines():
                line = line.strip()
                if line.startswith("Mods=") and len(line) > len("Mods="):
                    enabled_mods = line[len("Mods="):].split(';')
                elif line.startswith("WorkshopItems=") and len(line) > len("WorkshopItems="):
                    enabled_workshop_ids = line[len("WorkshopItems="):].split(';')
    except Exception as e:
        logger.error(f"Error reading {server_config}: {e}")
    return enabled_mods, enabled_workshop_ids


def fetch_workshop_details(mod_ids: List[str], batch_size: int = 100) -> Dict[str, Any] | None:
    """Fetch workshop details via Steam API with batching and retry logic."""
    if not mod_ids:
        return {}
    try:
        results: Dict[str, Dict[str, Any]] = {}
        use_pfs = STEAM_API_USE_PFS and bool(STEAM_API_KEY)
        base_url = STEAM_API_URL_PFS if use_pfs else STEAM_API_URL_SR
        if use_pfs:
            logger.info("Using IPublishedFileService/GetDetails with API-Key.")
        else:
            logger.info("Using ISteamRemoteStorage/GetPublishedFileDetails without API-Key.")
        for i in range(0, len(mod_ids), batch_size):
            batch = mod_ids[i:i+batch_size]
            params = {'itemcount': len(batch), 'includechildren': 'true'}
            for idx, mid in enumerate(batch):
                params[f'publishedfileids[{idx}]'] = mid
            if use_pfs:
                params['key'] = STEAM_API_KEY
            # Backoff on Rate Limit
            for attempt in range(5):
                try:
                    response = requests.get(base_url, params=params, timeout=20)
                except requests.RequestException as e:
                    wait = min(2 ** attempt, 30)
                    logger.warning(f"HTTP error: {e}, waiting {wait}s")
                    time.sleep(wait)
                    continue
                if response.status_code == 429:
                    wait = min(2 ** attempt, 60)
                    logger.warning(f"Rate limit 429 (Too Many Requests), waiting {wait}s")
                    time.sleep(wait)
                    continue
                try:
                    response.raise_for_status()
                    data = response.json()
                    break
                except Exception as e:
                    wait = min(2 ** attempt, 30)
                    logger.warning(f"Bad response from Steam-API ({e}), waiting {wait}s")
                    time.sleep(wait)
                    continue
            else:
                logger.error("Max retries for batch reached.")
                continue
            for details in data.get("response", {}).get("publishedfiledetails", []):
                mod_id = details.get("publishedfileid")
                results[mod_id] = {
                    'name': details.get('title'),
                    'tags': details.get('tags', []),
                    'time_created': details.get('time_created', 0),
                    'time_updated': details.get('time_updated', 0),
                    'num_children': details.get('num_children', 0),
                    'children': details.get('children', [])
                }
            time.sleep(0.5)  # pause between batches to prevent blocking
        return results
    except Exception as e:
        logger.error(f"Failed to fetch mod details: {e}")
        return {}


def create_modInfo(workshop_ids: List[str]) -> Dict[str, Dict]:
    """Fetch detailed info for all workshop IDs in batches."""
    infos: Dict[str] = {}
    if not workshop_ids:
        return infos
    for i in range(0, len(workshop_ids), BATCH_SIZE):
        batch_ids = workshop_ids[i:i + BATCH_SIZE]
        data = fetch_workshop_details(batch_ids)
        if data:
            infos.update(data)
    return infos


def write_discord_modlist(modInfo: Dict[str, Any], file_path: str):
    """Write mod list for Discord."""
    logger.info(f"Creating new Discord Url-Modlist for copy&paste: {file_path}")
    try:
        with open(file_path, "w", encoding='utf-8') as fh:
            for id, item in modInfo.items():
                url = DEFAULT_WORKSHOP_URL + id
                name = item.get('name', 'Unknown')
                fh.write(f"[{name}](<{url}>)\n")
            fh.write(f"\nTotal Mods: {len(modInfo)}")
    except Exception as e:
        logger.error(f"Error while writing {file_path}: {e}")


def write_modInfo_timeUpdated_file(modInfo: Dict[str, Any], file_path: str):
    """Save the mod info timestamps to a file."""
    logger.info("Saving modInfo-time_updated to local file so that the comparison works...")
    data: Dict[str] = {}
    for id, item in modInfo.items():
        data[id] = {
            "name": item.get('name', ''),
            "time_updated": item.get('time_updated', 0)
        }
    try:
        with open(file_path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=True, indent=4)
    except Exception as e:
        logger.error(f"Error while writing {file_path}: {e}")


async def are_mods_outdated(modInfo: Dict[str, Any], local_file: Path) -> bool:
    """Check if mods are outdated by comparing timestamps."""
    try:
        with open(local_file, 'r', encoding='utf-8') as fh:
            json_data = json.load(fh)
    except Exception as e:
        logger.error(f"Error reading {local_file}: {e}")
        return False
    outdated: List[str] = []
    for mod_id, item in modInfo.items():
        if mod_id in json_data.keys():
            remote_time = item.get('time_updated', 0)
            local_time = json_data[mod_id].get('time_updated', 0)
            if remote_time > local_time:
                outdated.append(mod_id)
                remote_time_str = datetime.fromtimestamp(remote_time).strftime("%d.%m.%Y %H:%M:%S")
                local_time_str = datetime.fromtimestamp(local_time).strftime("%d.%m.%Y %H:%M:%S")
                logger.warning(f'Mod "{item.get("name")}" ({mod_id}) is outdated: local "{local_time_str}" Vs. remote "{remote_time_str}"')
        else:
            # Mod not present locally
            outdated.append(mod_id)
    if outdated:
        # Update local timestamps if any are outdated
        write_modInfo_timeUpdated_file(modInfo, local_file)
    return len(outdated) > 0


async def check_mods_and_handle(modInfo: Dict[str, Any], test_mode: Optional[bool]=False) -> None:
    """Start warning and restart process if mods are outdated."""
    if await are_mods_outdated(modInfo, LOCAL_MODINFO_FILE):
        logger.warning("Outdated mods detected. Starting countdown and server restart.")
        await warn_and_restart(test_mode)


def main():
    check_pid()
    parser = argparse.ArgumentParser(description="Project Zomboid - Yet Another Remote Server Mod-Updated Watcher")
    parser.add_argument('--get_serverini',  action='store_true', help='Download server.ini and update local mod\'s time_updated file.')
    parser.add_argument('--msg', type=str, help='Send Server Message to all Players.')
    parser.add_argument('--test',  action='store_true', help='Only run a test without restarting Server.')
    args = parser.parse_args()
    try:
        if args.msg:
            asyncio.run(send_manual_message(args.msg))
        elif args.get_serverini:
            sftp_download(SFTP_REMOTE_FILE, LOCAL_SERVER_INI)
            mod_names, workshop_ids = read_enabled_mods(Path(LOCAL_SERVER_INI))
            modInfo = create_modInfo(workshop_ids)
            write_discord_modlist(modInfo, DISCORD_MODLIST_FILE)
            write_modInfo_timeUpdated_file(modInfo, LOCAL_MODINFO_FILE)
        elif args.test:
            print("Test mode: checking for mod updates, no server restart.")
            mod_names, workshop_ids = read_enabled_mods(LOCAL_SERVER_INI)
            modInfo = create_modInfo(workshop_ids)
            asyncio.run(check_mods_and_handle(modInfo, test_mode=True))
        else:
            # Regular execution
            if os.path.exists(LOCAL_SERVER_INI):
                mod_names, workshop_ids = read_enabled_mods(Path(LOCAL_SERVER_INI))
                modInfo = create_modInfo(workshop_ids)
                asyncio.run(check_mods_and_handle(modInfo))
            else:
                logger.info("No server.ini found.")
                sftp_download(SFTP_REMOTE_FILE, LOCAL_SERVER_INI)
                mod_names, workshop_ids = read_enabled_mods(Path(LOCAL_SERVER_INI))
                modInfo = create_modInfo(workshop_ids)
                write_modInfo_timeUpdated_file(modInfo, LOCAL_MODINFO_FILE)
                asyncio.run(check_mods_and_handle(modInfo))
    except KeyboardInterrupt:
        logger.info("Script interrupted.")
    finally:
        remove_pid()


if __name__ == "__main__":
    main()
