import asyncio
import base64
import json
import logging
import random
import time

from clientbase import Client, ClientFactory, SyncableStringFile


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
        return '%dd ago' % (t/86400)
    elif t > 3600:
        return '%dh ago' % (t/3600)
    elif t > 60:
        return '%dm ago' % (t/60)
    else:
        return 'just now'


class Tool(Client):

    def __init__(self, clientid, factory, address, mqtt_prefix='undefined/'):
        self.clientid = clientid
        self.factory = factory
        self.tooldb = factory.tooldb
        self.tokendb = factory.tokendb
        self.address = address

        # initial value for slug
        self.slug = self.tooldb.get_value(self.clientid, 'slug', self.clientid)

        # asyncio network streams, will be populated by the factory
        self.reader = None
        self.writer = None

        # callback for sending an outbound message object
        # will be populated by the asyncio protocol handler
        self.write_callback = None

        # remote state for syncing
        self.remote_files = {}
        self.remote_firmware_active = None
        self.remote_firmware_pending = None

        # local specification for syncing
        self.files = {}
        self.firmware = None

        # current state of the sync
        self.firmware_complete = asyncio.Event()
        self.firmware_complete.clear()
        self.firmware_pending_reboot = False

        # mqtt
        self.mqtt_prefix = mqtt_prefix
        self.mqtt_cache = {}

    async def reload_settings(self, create=False):
        self.slug = self.tooldb.get_value(self.clientid, 'slug', self.clientid)
        self.token_groups = self.tooldb.get_value(self.clientid, 'groups')
        config_json = json.dumps(self.tooldb.get_config(self.clientid)).encode()
        token_data = self.tokendb.token_database_v2(
            groups=self.token_groups,
            salt=self.tooldb.get_value(self.clientid, 'token_salt').encode())
        if create:
            self.files['config.json'] = SyncableStringFile('config.json', config_json)
            self.files['tokens.dat'] = SyncableStringFile('tokens.dat', token_data)
        else:
            self.files['config.json'].update(config_json)
            self.files['tokens.dat'].update(token_data)
        firmware_filename = self.tooldb.get_value(self.clientid, 'firmware')
        if firmware_filename:
            self.firmware = await self.get_firmware(firmware_filename)

    def get_motd(self):
        last_user = self.factory.toolstatedb.get('{}:last_user'.format(self.clientid))
        last_user_time = float(self.factory.toolstatedb.get('{}:last_user_time'.format(self.clientid), 0))
        if last_user and last_user_time > 0:
            return '{:.10}, {}'.format(last_user.decode(), friendly_age(time.time()-last_user_time))[0:20]
        else:
            return self.tooldb.get_value(self.clientid, 'motd', '')

    async def send_motd(self):
        await self.send_message({'cmd': 'motd', 'motd': self.get_motd()})

    async def main_task(self):
        logging.debug("main_task() started")

        last_keepalive = time.time()
        last_motd = 0

        await self.send_message({'cmd': 'state_query'})
        last_statistics = time.time() - random.randint(15, 60)

        while True:
            if time.time() - last_motd > 60:
                await self.send_motd()
                last_motd = time.time()
            if time.time() - last_keepalive > 30:
                logging.debug('sending keepalive ping')
                await self.send_message({'cmd': 'ping', 'timestamp': str(time.time())})
                last_keepalive = time.time()
            if time.time() - last_statistics > 60:
                await self.send_message({'cmd': 'state_query'})
                await self.send_message({'cmd': 'metrics_query'})
                await self.send_message({'cmd': 'net_metrics_query'})
                last_statistics = time.time()
            await asyncio.sleep(5)

    async def handle_cmd_state_info(self, message):
        if 'state' in message:
            await self.send_mqtt('status', message['state'], True, dedup=True)
        if 'milliamps' in message:
            await self.send_mqtt('current', message['milliamps'] / 1000.0, True)
        if 'milliamps_simple' in message:
            await self.send_mqtt('current_simple', message['milliamps_simple'] / 1000.0, True)
        if 'user' in message:
            await self.send_mqtt('user', message['user'], True, dedup=True)
            last_user = self.factory.toolstatedb.get('{}:last_user'.format(self.clientid))
            if message['user'] != '' and message['user'] != last_user:
                self.factory.toolstatedb['{}:last_user'.format(self.clientid)] = message['user']
                self.factory.toolstatedb['{}:last_user_time'.format(self.clientid)] = str(time.time())
                await self.send_mqtt('last_user', message['user'], True, dedup=True)
        #if 'last_user' in message:
        #    await self.send_mqtt('last_user', message['last_user'], True, dedup=True)


class ToolFactory(ClientFactory):
    
    def __init__(self, tooldb, tokendb, toolstatedb):
        self.tooldb = tooldb
        self.tokendb = tokendb
        self.toolstatedb = toolstatedb
        super(ToolFactory, self).__init__()

    def client_from_auth(self, clientid, password, address=None):
        if self.tooldb.authenticate(clientid, password):
            client = Tool(clientid, factory=self, address=address, mqtt_prefix=self.mqtt_prefix)
            self.clients_by_id[clientid] = client
            self.clients_by_slug[client.slug] = client
            return client
        else:
            logging.info('client {} auth failed (address={})'.format(clientid, address))
        return None
