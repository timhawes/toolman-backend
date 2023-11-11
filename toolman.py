import asyncio
import base64
import logging
import random
import time

from clientbase import CommonConnection, CommonManager


def encode_tune(tune):
    """Pack a tune into the client's internal format.

    The input tune must be a Python list:

    [ [frequency, milliseconds], [frequency2, milliseconds2], ...]
    """

    output = []
    for note in tune:
        hz = note[0] & 0xFFFF
        ms = note[1] & 0xFFFF
        output.append(hz & 0xFF)
        output.append(hz >> 8)
        output.append(ms & 0xFF)
        output.append(ms >> 8)
    return base64.b64encode(bytes(output)).decode()


def friendly_age(t):
    if t > 86400:
        return "%dd ago" % (t / 86400)
    elif t > 3600:
        return "%dh ago" % (t / 3600)
    elif t > 60:
        return "%dm ago" % (t / 60)
    else:
        return "just now"


class ToolConnection(CommonConnection):
    client_strip_prefix = "toolman-"

    def get_motd(self):
        last_user = self.manager.toolstatedb.get(f"{self.clientid}:last_user")
        last_user_time = float(
            self.manager.toolstatedb.get(f"{self.clientid}:last_user_time", 0)
        )
        if last_user and last_user_time > 0:
            return "{:.10}, {}".format(
                last_user.decode(), friendly_age(time.time() - last_user_time)
            )[0:20]
        else:
            return self.config.get("motd", "")

    async def send_motd(self):
        await self.send_message({"cmd": "motd", "motd": self.get_motd()})

    async def main_task(self):
        logging.debug("main_task() started")

        last_motd = 0

        await self.set_states({"status": "online"})

        await self.send_message({"cmd": "state_query"})
        last_statistics = time.time() - random.randint(0, 45)

        while True:
            await self.loop()
            if time.time() - last_motd > 60:
                await self.send_motd()
                last_motd = time.time()
            if time.time() - last_statistics > 60:
                await self.send_message({"cmd": "state_query"})
                await self.send_message({"cmd": "metrics_query"})
                last_statistics = time.time()
            await asyncio.sleep(5)

    async def handle_cmd_state_info(self, message):
        states = {}
        metrics = {}

        if "state" in message:
            states["state"] = message["state"]
        if "milliamps" in message:
            metrics["current"] = message["milliamps"] / 1000.0
        if "milliamps_simple" in message:
            metrics["current_simple"] = message["milliamps_simple"] / 1000.0
        if "user" in message:
            states["user"] = message["user"]
            last_user = self.manager.toolstatedb.get(f"{self.clientid}:last_user")
            if message["user"] != "" and message["user"] != last_user:
                self.manager.toolstatedb[f"{self.clientid}:last_user"] = message["user"]
                self.manager.toolstatedb[f"{self.clientid}:last_user_time"] = str(
                    time.time()
                )
                states["last_user"] = message["user"]
        # if "last_user" in message:
        #    states["last_user"] = message["last_user"]

        await self.set_states(states, timestamp=message.pop("time", None))
        await self.set_metrics(metrics, timestamp=message.pop("time", None))


class ToolManager(CommonManager):
    connection_class = ToolConnection

    def __init__(self, *args, toolstatedb=None, **kwargs):
        self.toolstatedb = toolstatedb
        super().__init__(*args, **kwargs)
