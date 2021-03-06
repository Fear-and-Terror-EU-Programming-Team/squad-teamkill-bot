#!/usr/bin/env python3

import aiohttp
import asyncio
import discord
import logging
import msvcrt
import ntpath
import os
import re
import sys
import traceback
import win32file
from . import config
from .teamkill import TeamKill
from datetime import datetime
from discord import Webhook, AsyncWebhookAdapter
from pytz import timezone
from steam import SteamQuery

logger = logging.getLogger(__name__)


class TKMonitor:

    def __init__(self, basedir):
        self.basedir = basedir
        self.log_filename = basedir + r"\SquadGame\Saved\Logs\SquadGame.log"
        self.admincam_log_filename = basedir + r"\SquadGame\Saved\Logs\admincam.log"
        self.recent_damages = []
        self.seen_tks = set()
        self.last_log_id = 0
        self.active_admin_cam_users = set()

        basename = ntpath.basename(basedir)

        self.logger = logging.getLogger(f"{__name__}_{basename}")

    def _open_log_file(self):
        self.logger.debug("(Re-)Opening server log file...")
        # source:
        # https://www.thepythoncorner.com/2016/10/python-how-to-open-a-file-on-windows-without-locking-it/
        # get a handle using win32 API, specifying the SHARED access!
        handle = win32file.CreateFile(self.log_filename,
                                      win32file.GENERIC_READ,
                                      win32file.FILE_SHARE_DELETE |
                                      win32file.FILE_SHARE_READ |
                                      win32file.FILE_SHARE_WRITE,
                                      None,
                                      win32file.OPEN_EXISTING,
                                      0,
                                      None)
        # detach the handle
        detached_handle = handle.Detach()
        # get a file descriptor associated to the handle
        file_descriptor = msvcrt.open_osfhandle(
            detached_handle, os.O_RDONLY)
        # open the file descriptor
        f = open(file_descriptor, encoding="UTF-8")
        # seek to end
        f.seek(0, os.SEEK_END)

        size = os.fstat(f.fileno()).st_size

        self.logger.debug(f"Opened server log file. Size: {size}")
        return f, size

    # Generate the lines in the text file as they are created
    async def _log_follow(self):
        f, file_size = self._open_log_file()
        line_counter = 0
        # read indefinitely
        while True:
            # read until end of file
            while True:
                try:
                    line = f.readline()
                except UnicodeDecodeError as e:
                    sys.stderr.write(
                        "[WARN] Skipped line because of decode error\n")
                    line = "DECODE_ERROR"
                    self.logger.debug(f"DECODE_ERROR")
                if not line:
                    break
                # update file size every 1000 lines
                if line_counter == 0:
                    file_size = os.fstat(f.fileno()).st_size
                line_counter = (line_counter + 1) % 1000
                self.logger.debug(f"[READ]{line}")
                yield line
            try:
                # check if file has been truncated
                cur_size = os.fstat(f.fileno()).st_size
                self.logger.debug(
                    f"[FILE_SIZE] {cur_size} -- {file_size}")
                if cur_size < file_size:
                    self.logger.debug(f"FILE_TRUNCATED")
                    # close and re-open
                    f.close()
                    f, file_size = self._open_log_file()
            except IOError as e:
                self.logger.warning(f"[EXCEPTION] {e}")
            self.logger.debug(f"GOING_TO_SLEEP")
            await asyncio.sleep(1)
            self.logger.debug(f"WOKE_UP")

    # Parser to find teamkills, map, kill info
    def parse_line(self, line):
        """Returns TK object if TK occurred, `None` otherwise."""
        self.logger.debug(f"START")

        # try matching to admin cam usage format
        if self._match_admincam(line):
            return None

        # try matching to damage format
        if self._match_damage(line):
            return None

        # try matching to teamkill format
        return self._match_teamkill(line)

    def _match_damage(self, line):
        """Returns `True` if line was a damage notification,
        `False` otherwise."""
        self.logger.debug(f"?")

        actual_damage = re.search(
            r"\[(?P<time>[^\]]+)\]"  # time
            r"\[(?P<log_id>[0-9]+)\]"  # log_id
            r"LogSquad: Player:"
            r"(?P<victim>.*)"  # victim
            r" ActualDamage=.* from "
            r"(?P<killer>.*)"  # killer
            r" caused by "
            r"BP_(?P<weapon>[^\_]*)\_",  # weapon
            line,
        )

        # remember damage
        if actual_damage is None:
            self.logger.debug(f"-")
            return False
        self.logger.debug(f"+")

        self.recent_damages.append(actual_damage)
        # only track the last 20 damages
        self.logger.debug(f"recent size {len(self.recent_damages)}")
        if len(self.recent_damages) > 20:
            del self.recent_damages[0]
        return True  # matched

    def _match_teamkill(self, line):
        '''Returns TK object if TK occurred, `None` otherwise.'''
        self.logger.debug(f"?")

        team_kill = re.search(
            r"\[(?P<log_id>[0-9]+)\]"  # log_id
            r"[^\n]*"
            r"LogSquadScorePoints:[^\n]*TeamKilled",
            line,
        )

        if team_kill == None:
            self.logger.debug(f"-")
            return None
        self.logger.debug(f"+")

        # delete duplicate info on log id wrap-around
        if int(team_kill.group("log_id")) + 500 < self.last_log_id:
            self.logger.debug(f"WRAP_AROUND")
            self.seen_tks.clear()
        self.last_log_id = int(team_kill.group("log_id"))

        # check for duplicate
        if team_kill.group("log_id") in self.seen_tks:
            self.logger.debug(f"DUPLICATE")
            return None
        self.logger.debug(f"NEW")

        # match log IDs
        for dmg in self.recent_damages:
            if dmg.group("log_id") == team_kill.group("log_id"):
                self.logger.debug(f"MATCH FOUND")
                time_str = dmg.group("time")
                time_naive = datetime.strptime(time_str,
                                               "%Y.%m.%d-%H.%M.%S:%f")
                # Timestamps are UTC
                time_utc = timezone("UTC").localize(time_naive)
                victim = dmg.group("victim")
                killer = dmg.group("killer")
                weapon = dmg.group("weapon")
                tk = TeamKill(time_utc, victim, killer, weapon)

                # remember log ID of last TK to avoid duplicates
                self.seen_tks.add(team_kill.group("log_id"))

                return tk

    def _match_admincam(self, line):
        """Returns `True` if line was admin cam usage,
        `False` otherwise."""
        self.logger.debug(f"ENTER ?")

        change = None

        # check for possess
        match = re.search(
            r"\[(?P<time>[^\]]+)\]"  # time
            r"\[(?P<log_id>[0-9]+)\]"  # log_id
            r"[^\n]*"
            r"ASQPlayerController::Possess"
            r"[^\n]*"
            r"PC=(?P<user>.*) "  # user
            r"[^\n]*"
            r"Pawn=CameraMan_C_"  # admin cam
            ,
            line,
        )

        if match != None:
            self.logger.debug(f"ENTER +")
            change = "++++++++++++ ENTER"
            user = match.group("user")
            self.active_admin_cam_users.add(user)
        else:
            self.logger.debug(f"ENTER -")
            self.logger.debug(f"LEAVE ?")
            # check for unpossess
            match = re.search(
                r"\[(?P<time>[^\]]+)\]"  # time
                r"\[(?P<log_id>[0-9]+)\]"  # log_id
                r"[^\n]*"
                r"ASQPlayerController::UnPossess"
                r"[^\n]*"
                r"PC=(?P<user>.*)"  # user
                ,
                line,
            )

            if match != None:
                self.logger.debug(f"LEAVE +")
                change = "--- POSSIBLE LEAVE"
                user = match.group("user")
                if user not in self.active_admin_cam_users:
                    # false positive
                    return False
                else:
                    self.active_admin_cam_users.remove(user)
            self.logger.debug(f"LEAVE -")

        if change is None:
            self.logger.debug(f"-")
            return False
        self.logger.debug(f"+")

        time_str = match.group("time")
        time_naive = datetime.strptime(time_str, "%Y.%m.%d-%H.%M.%S:%f")
        # Timestamps are UTC
        time_utc = timezone("UTC").localize(time_naive)
        time_utc_str = time_utc.strftime("%Y.%m.%d - %H:%M:%S")
        user = match.group("user")
        log_message = f"[{time_utc_str} UTC] {change}: {user}"

        self.logger.debug(f"Opening admincam log")
        with open(self.admincam_log_filename, "a", encoding="UTF-8") as f:
            self.logger.debug(f"Writing admincam log")
            f.write(log_message + "\n")
            self.logger.debug(f"Done writing to admincam log")
        self.logger.debug(f"Closed admincam log")
        self.logger.debug(f"[{self.basedir}]{log_message}")

        return True

    async def tk_follow(self):
        self.logger.debug(f"START")
        async for line in self._log_follow():
            self.logger.debug(f"GOT_LINE")
            tk = self.parse_line(line)
            if tk is not None:
                self.logger.debug(f"TK+")
                yield tk
            self.logger.debug(f"TK-")


async def run_tkm(server):
    tkm = TKMonitor(server.basedir)
    logging.debug("Creating TKMs")
    async for tk in tkm.tk_follow():
        logging.info(f"[SEND] {server} {tk}")

        try:
            await post_tk(server, tk)
        except Exception as e:
            sys.stderr.write("[ERROR] [{qport}] Exception in post_tk\n")
            traceback.print_exception(type(e), e, e.__traceback__)
            sys.stderr.write("[ERROR] [{qport}] <<< End of exception\n")


async def post_tk(server, teamkill):
    """Posts the Teamkill to Discord via the configured webhooks.

    Current map and servername are obtained through SteamQuery."""

    # Get map and name from SteamQuery
    server_obj = SteamQuery(server.host, server.qport)
    server_info = server_obj.query_game_server()
    cur_map = server_info["map"]
    server_name = server_info["name"]

    # Create embed
    embed = discord.Embed(title=f"TK on {server_name}")

    # Time
    time_utc = teamkill.time_utc
    time_config = time_utc.astimezone(config.TIMEZONE)
    time_config_str = time_config.strftime("%m/%d/%Y %H:%M:%S")
    embed.add_field(name=f"Date / Time ({config.TIMEZONE_NAME})",
                    value=time_config_str, inline=True)

    # Time (UTC)
    time_utc_str = time_utc.strftime("%H:%M:%S")
    if time_utc.date() > time_config.date():
        time_utc_str += " (+1 day)"
    embed.add_field(name='Time (UTC)', value=time_utc_str, inline=True)

    embed.add_field(name='Map', value=cur_map, inline=True)

    # Killer
    embed.add_field(name='Killer', value=teamkill.killer, inline=True)
    # Victim
    embed.add_field(name='Victim', value=teamkill.victim, inline=True)
    # Weapon
    embed.add_field(name='Weapon', value=teamkill.weapon, inline=True)

    # Send message via webhook
    async with aiohttp.ClientSession() as session:
        webhook = Webhook.from_url(server.webhook_url,
                                   adapter=AsyncWebhookAdapter(session))
        await webhook.send(embed=embed)


async def main():
    logger.info("TK tracker started. Following squad logs on:")
    for server in config.servers:
        logger.info(f"- {server.basedir}")
    logger.info("--------------------------------------------")
    tasks = []
    for server in config.servers:
        tasks.append(asyncio.create_task(run_tkm(server)))

    for t in tasks:
        await t
