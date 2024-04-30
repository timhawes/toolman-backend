import asyncio
import base64
import random
import time

from clientbase import CommonConnection, CommonManager

import settings


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

    async def get_last_user(self):
        if self.manager.redis_client:
            data = await self.manager.redis_client.hgetall(f"doorman:{self.clientid}")
            last_user = data.get("last_user")
            last_user_time = data.get("last_user_time")
            if last_user_time:
                last_user_time = float(last_user_time)
        elif self.manager.toolstatedb:
            last_user = self.manager.toolstatedb.get(f"{self.clientid}:last_user")
            if last_user:
                last_user = last_user.decode()
            last_user_time = float(
                self.manager.toolstatedb.get(f"{self.clientid}:last_user_time", 0)
            )
        else:
            last_user = None
            last_user_time = None
        return last_user, last_user_time

    async def set_last_user(self, last_user, last_user_time):
        if self.manager.redis_client:
            await self.manager.redis_client.hset(
                f"doorman:{self.clientid}",
                mapping={"last_user": last_user, "last_user_time": last_user_time},
            )
        if self.manager.toolstatedb:
            self.manager.toolstatedb[f"{self.clientid}:last_user"] = last_user
            self.manager.toolstatedb[f"{self.clientid}:last_user_time"] = str(
                last_user_time
            )

    async def get_motd(self):
        motd = self.config.get("motd", "")
        if not motd:
            last_user, last_user_time = await self.get_last_user()
            if last_user and last_user_time > 0:
                motd = "{:.10}, {}".format(
                    last_user, friendly_age(time.time() - last_user_time)
                )[0:20]
        return motd

    async def send_motd(self):
        await self.send_message({"cmd": "motd", "motd": await self.get_motd()})

    async def handle_post_auth(self):
        await super().handle_post_auth()
        self.create_task(self.tool_task())
        self.create_task(self.motd_task())

    async def tool_task(self):
        await self.send_message({"cmd": "state_query"})
        await asyncio.sleep(
            random.randint(0, int(settings.METRICS_QUERY_INTERVAL * 0.25))
        )
        while True:
            await self.send_message({"cmd": "state_query"})
            await self.send_message({"cmd": "metrics_query"})
            await asyncio.sleep(settings.METRICS_QUERY_INTERVAL)

    async def motd_task(self):
        while True:
            await self.send_motd()
            await asyncio.sleep(60)

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
            last_user, last_user_time = await self.get_last_user()
            if message["user"] != "":
                await self.set_last_user(message["user"], time.time())
                if message["user"] != last_user:
                    states["last_user"] = message["user"]
        # if "last_user" in message:
        #    states["last_user"] = message["last_user"]

        await self.set_states(states, timestamp=message.pop("time", None))
        await self.set_metrics(metrics, timestamp=message.pop("time", None))


class ToolManager(CommonManager):
    connection_class = ToolConnection

    def __init__(self, *args, toolstatedb=None, redis_client=None, **kwargs):
        self.toolstatedb = toolstatedb
        self.redis_client = redis_client
        super().__init__(*args, **kwargs)
