import binascii
import base64
import dbm
import hashlib
import logging
import os
import random
import time
import json
import paho.mqtt.client as mqtt

import jsonloader
import yamlloader
from ehl_tokendb_json import TokenAuthDatabase

PEOPLE_JSON = os.environ.get('PEOPLE_JSON', 'people.json')
TOOLS_YAML = os.environ.get('TOOLS_YAML', 'tools.yaml')

tools_loader = yamlloader.YamlLoader(TOOLS_YAML, defaults_key='DEFAULTS')
people_loader = jsonloader.JsonLoader(PEOPLE_JSON)
auth_database = TokenAuthDatabase(PEOPLE_JSON)
toolstate_db = dbm.open(os.environ.get('TOOLSTATE_DB', 'toolstate-db'), 'c')
tools_by_clientid = {}

mqtt_session = mqtt.Client()
mqtt_session.connect("mqtt")
mqtt_session.loop_start()

def random_string(length=16):
    characters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return ''.join(map(random.choice, [characters]*length))

def friendly_age(t):
    if t > 86400:
        return '%dd ago' % (t/86400)
    elif t > 3600:
        return '%dh ago' % (t/3600)
    elif t > 60:
        return '%dm ago' % (t/60)
    else:
        return 'just now'

def friendly_time(t):
    age = time.time()-t
    if age > 86400:
        f = "%Y-%m-%d %H:%M"
    elif age > 60:
        f = "%H:%M"
    else:
        f = "now"
    return time.strftime(f, time.localtime(t))

class NotFound(Exception):
    pass

class AuthFailed(Exception):
    pass

class Tool:
    def __init__(self, clientid):
        if clientid not in tools_loader.data:
            raise NotFound
        self.clientid = clientid
        self.firmware = None
        self.config_changed()
    def config_changed(self):
        self.mqtt_prefix = 'tool/{}'.format(tools_loader.data[self.clientid].get('slug', self.clientid))
        if 'firmware' in tools_loader.data[self.clientid]:
            if os.path.isfile(tools_loader.data[self.clientid]['firmware']):
                self.firmware = {}
                self.firmware['payload'] = open(tools_loader.data[self.clientid]['firmware'], 'rb').read()
                self.firmware['md5'] = hashlib.md5(self.firmware['payload']).hexdigest()
                self.firmware['size'] = len(self.firmware['payload'])
            else:
                logging.warning('firmware {} not found'.format(tools_loader.data[self.clientid]['firmware']))
    def auth_token(self, uid):
        auth_data = auth_database.auth_token(uid, tools_loader.data[self.clientid].get('groups', []))
        logging.info("auth token {} on {} -> {}".format(uid, self.clientid, auth_data))
        return auth_data
    def get_config(self):
        return tools_loader.data[self.clientid]['config']
    def get_motd(self):
        last_user = toolstate_db.get('{}:last_user'.format(self.clientid))
        last_user_time = float(toolstate_db.get('{}:last_user_time'.format(self.clientid), 0))
        if last_user and last_user_time > 0:
            return "{:.10}, {}".format(last_user.decode(), friendly_age(time.time()-last_user_time))[0:20]
        else:
            return tools_loader.data[self.clientid].get('motd', '')
    def get_ping_interval(self):
        return tools_loader.data[self.clientid].get('ping_interval', 0)
    def token_database(self):
        if tools_loader.data[self.clientid].get('tokensversion') == 2:
            return auth_database.token_database_v2(groups=tools_loader.data[self.clientid].get('groups', []))
        else:
            return auth_database.token_database_v1(groups=tools_loader.data[self.clientid].get('groups', []))
    def token_database_v1(self):
        return self.token_database()

class ToolSession:

    def __init__(self, address=None, protocol=None, resend_timeout=60):
        self.address = address
        self.protocol = protocol
        self.resend_timeout = resend_timeout
        self.nonce = random_string()
        self.tool = None
        self.files = {}
        self.firmware = None
        self.config_version = 0
        self.config_changed = False
        self.seq = random.randint(0, (1<<32)-1)
        self.unacked = {}
        self.resent_count = 0
        self.outofseq_count = 0
        self.last_status = None
        self.last_status_user = None
        self.logid = '-'
        self.last_motd = None
        self.last_ping = 0
        self.last_stats = time.time()
        self.last_seq = None

    def _seq(self):
        self.seq += 1
        return self.seq

    def _get_tool(self, clientid, password):
        self.check_config()
        try:
            if tools_loader.data[clientid]["password"] == password:
                tool = Tool(clientid)
                tools_by_clientid[clientid] = tool
                return tool
            else:
                raise AuthFailed
        except KeyError:
            raise NotFound

    def check_config(self):
        tools_loader.check()
        new_version = tools_loader.version
        if new_version != self.config_version:
            self.config_version = new_version
            self.config_changed = True
            if self.tool:
                self.tool.config_changed()

    def greeting_message(self):
        self.check_config()
        return {"cmd": "hello", "nonce": self.nonce, "time": int(time.time())}

    def close(self):
        # if this is the currently active session for a tool,
        # then send MQTT events to show the status as offline
        # (if a newer session has replaced us then we should not send the events)
        if self.tool and self.tool is tools_by_clientid.get(self.tool.clientid):
            if mqtt_session and self.tool and self.tool.mqtt_prefix:
                mqtt_session.publish('{}/status'.format(self.tool.mqtt_prefix), 'offline', retain=True)
                mqtt_session.publish('{}/user'.format(self.tool.mqtt_prefix), '', retain=True)
                mqtt_session.publish('{}/address'.format(self.tool.mqtt_prefix), '', retain=True)
                mqtt_session.publish('{}/current'.format(self.tool.mqtt_prefix), '', retain=True)

    def periodic(self):
        for seq in sorted(self.unacked.keys()):
            if self.resend_timeout is None:
                del self.unacked[seq]
                continue
            (timestamp, msg) = self.unacked[seq]
            if time.time() - timestamp > self.resend_timeout:
                logging.info("timeout of unacked packet")
                del self.unacked[seq]
            else:
                self.resent_count = self.resent_count + 1
                yield msg
        if self.tool:
            ping_interval = self.tool.get_ping_interval()
            if ping_interval > 0 and time.time() - self.last_ping > (ping_interval / 1000.0):
                seq = self._seq()
                msg = {'cmd': 'ping', 'seq': seq}
                self.last_ping = time.time()
                self.unacked[seq] = time.time(), msg
                yield msg
        if time.time() - self.last_stats > 60:
            logging.info('{} stats unacked={} resent={} outofseq={}'.format(self.logid, len(self.unacked), self.resent_count, self.outofseq_count))
            if mqtt_session and self.tool and self.tool.mqtt_prefix:
                mqtt_session.publish('{}/network_unacked'.format(self.tool.mqtt_prefix), str(len(self.unacked)), retain=True)
                mqtt_session.publish('{}/network_resent'.format(self.tool.mqtt_prefix), str(self.resent_count), retain=True)
                mqtt_session.publish('{}/network_outofseq'.format(self.tool.mqtt_prefix), str(self.outofseq_count), retain=True)
            self.resent_count = 0
            self.outofseq_count = 0
            self.last_stats = time.time()

    def add_seq(self, msg):
        seq = self._seq()
        msg['seq'] = seq
        self.unacked[seq] = time.time(), msg
        return msg

    def handle_message(self, data):
        if 'seq' in data:
            if self.last_seq is None:
                self.last_seq = data['seq']
            else:
                delta = data['seq'] - self.last_seq
                if delta != 1:
                    self.outofseq_count += 1
                self.last_seq = data['seq']

        if 'ack' in data:
            if data['ack'] in self.unacked:
                rtt = time.time() - self.unacked[data['ack']][0]
                logging.info('{} seq={} rtt={}ms'.format(self.logid, data['ack'], int(rtt*1000)))
                del self.unacked[data['ack']]

        if 'cmd' not in data:
            return

        self.check_config()
        if self.config_changed:
            if self.tool:
                logging.info("sending updated config")
                msg = {'cmd': 'config'}
                msg.update(self.tool.get_config())
                yield self.add_seq(msg)
                if self.tool.firmware:
                    yield self.add_seq({'cmd': 'firmware', 'md5': self.tool.firmware['md5'], 'size': self.tool.firmware['size']})
            self.config_changed = False

        if data["cmd"] == "auth":
            if self.tool:
                if 'seq' in data:
                    yield {'ack': data['seq']}
                yield {'cmd': 'auth-ok'}
                msg = {'cmd': 'config'}
                msg.update(self.tool.get_config())
                yield msg
                self.files['tokens'] = self.tool.token_database()
                seq = self._seq()
                msg = {
                    'cmd': 'file',
                    'filename': 'tokens',
                    'size': len(self.files['tokens']),
                    'md5': hashlib.md5(self.files['tokens']).hexdigest(),
                    'seq': seq,
                    }
                self.unacked[seq] = time.time(), msg
                yield msg
                new_motd = self.tool.get_motd()
                if new_motd != self.last_motd:
                    seq = self._seq()
                    msg = {'cmd': 'motd', 'motd': new_motd, 'seq': seq}
                    self.unacked[seq] = time.time(), msg
                    yield msg
                    self.last_motd = new_motd
                if self.tool.firmware:
                    seq = self._seq()
                    msg = {'cmd': 'firmware', 'md5': self.tool.firmware['md5'], 'size': self.tool.firmware['size'], 'seq': seq}
                    self.unacked[seq] = time.time(), msg
                    yield msg
            try:
                t = self._get_tool(data["client"], data["password"])
                self.tool = t
                self.logid = '{}/{}'.format(self.tool.clientid, tools_loader.data.get(self.tool.clientid, {}).get('slug'))
                if mqtt_session:
                    mqtt_session.publish('{}/id'.format(self.tool.mqtt_prefix), self.tool.clientid, retain=True)
                    mqtt_session.publish('{}/address'.format(self.tool.mqtt_prefix), self.address[0], retain=True)
                yield {'cmd': 'auth-ok'}
                msg = {'cmd': 'config'}
                msg.update(self.tool.get_config())
                yield msg
                self.files['tokens'] = self.tool.token_database()
                seq = self._seq()
                msg = {
                    'cmd': 'file',
                    'filename': 'tokens',
                    'size': len(self.files['tokens']),
                    'md5': hashlib.md5(self.files['tokens']).hexdigest(),
                    'seq': seq,
                    }
                self.unacked[seq] = time.time(), msg
                yield msg
                new_motd = self.tool.get_motd()
                if new_motd != self.last_motd:
                    seq = self._seq()
                    msg = {'cmd': 'motd', 'motd': new_motd, 'seq': seq}
                    self.unacked[seq] = time.time(), msg
                    yield msg
                    self.last_motd = new_motd
                if self.tool.firmware:
                    seq = self._seq()
                    msg = {'cmd': 'firmware', 'md5': self.tool.firmware['md5'], 'size': self.tool.firmware['size'], 'seq': seq}
                    self.unacked[seq] = time.time(), msg
                    yield msg
            except NotFound:
                t = None
                msg = {'cmd': 'bye', 'error': "Sorry, I don't know you"}
                yield msg
                yield None
                return
            except AuthFailed:
                t = None
                msg = {'cmd': 'bye', 'error': "Sorry, I don't know you (auth failed)"}
                yield msg
                yield None
                return

        if not self.tool:
            yield self.greeting_message()
            return

        if 'seq' in data:
            yield {'ack': data['seq']}

        if tools_loader.data.get(self.tool.clientid, {}).get('reboot'):
            msg = {'cmd': 'reboot'}
            yield msg

        if data["cmd"] in ["token-auth", "token_auth"]:
            if self.tool:
                token = self.tool.auth_token(data["uid"].lower())
                if token:
                    yield {'cmd': 'token-info', "uid": data["uid"], "found": True, "name": token["name"], "access": token["access"]}
                else:
                    yield {"cmd": "token-info", "uid": data["uid"], "found": False}
        if data["cmd"] == "file_request":
            if self.tool:
                logging.info("preparing to serve file {}".format(data['filename']))
                filename = data['filename']
                payload = self.files[filename]
                if 'position' in data:
                    position = int(data['position'])
                    chunk_size = data.get('chunk_size', 256)
                    if position >= len(payload):
                        logging.error('client asked for too much data')
                        return
                    msg = {
                        'cmd': 'file_payload',
                        'filename': filename,
                        'position': position,
                        'data': base64.b64encode(payload[position:position+chunk_size]).decode(),
                        }
                    if position == 0:
                        msg['start'] = True
                        msg['md5'] = hashlib.md5(payload).hexdigest()
                        msg['size'] = len(payload)
                    if position + chunk_size >= len(payload):
                        msg['end'] = True
                    yield msg
                else:
                    chunk_size = 256
                    chunks, mod = divmod(len(payload), chunk_size)
                    if mod > 0:
                        chunks += 1
                    logging.info("there will be {} chunks of data".format(chunks))
                    for i in range(0, chunks):
                        logging.info('chunk {}'.format(i))
                        msg = {
                            'cmd': 'file_payload',
                            'filename': filename,
                            'position': chunk_size*i,
                            'data': base64.b64encode(payload[chunk_size*i:chunk_size*(i+1)]).decode(),
                            }
                        if i == 0:
                            msg['start'] = True
                            msg['md5'] = hashlib.md5(payload).hexdigest()
                            msg['size'] = len(payload)
                        if i == chunks - 1:
                            msg['end'] = True
                        yield msg
        if data["cmd"] == "firmware_request":
            if self.tool:
                position = int(data['position'])
                chunk_size = data.get('chunk_size', 256)
                if position >= len(self.tool.firmware['payload']):
                    logging.error('client asked for too much data')
                    return
                seq = self._seq()
                msg = {
                    'cmd': 'firmware_payload',
                    'position': position,
                    'data': base64.b64encode(self.tool.firmware['payload'][position:position+chunk_size]).decode(),
                    'seq': seq,
                    }
                if position == 0:
                    msg['start'] = True
                    msg['md5'] = self.tool.firmware['md5']
                    msg['size'] = self.tool.firmware['size']
                if position + chunk_size >= len(self.tool.firmware['payload']):
                    msg['end'] = True
                self.unacked[seq] = time.time(), msg
                yield msg
        if data["cmd"] == "keepalive":
            pass
        if data["cmd"] == "ping":
            msg = {'cmd': 'pong'}
            if 'seq' in data:
                msg['seq'] = data['seq']
            if 'millis' in data:
                msg['millis'] = data['millis']
            yield msg
        if data["cmd"] == "status":
            if mqtt_session and 'status' in data and data['status'] != self.last_status:
                mqtt_session.publish('{}/status'.format(self.tool.mqtt_prefix), data['status'], retain=True)
                self.last_status = data['status']
            if mqtt_session and 'udp_retry_count' in data:
                mqtt_session.publish('{}/udp_retry_count'.format(self.tool.mqtt_prefix), data['udp_retry_count'], retain=True)
            if mqtt_session and 'user' in data and data['user'] != self.last_status_user:
                mqtt_session.publish('{}/user'.format(self.tool.mqtt_prefix), data['user'], retain=True)
                mqtt_session.publish('{}/last_user'.format(self.tool.mqtt_prefix), toolstate_db.get('{}:last_user'.format(self.tool.clientid, '')), retain=True)
                self.last_status_user = data['user']
            if mqtt_session and 'milliamps' in data:
                mqtt_session.publish('{}/current'.format(self.tool.mqtt_prefix), data['milliamps'] / 1000.0, retain=True)
            if data.get('user', '') != '' and data.get('status', '') != 'standby':
                toolstate_db['{}:last_user'.format(self.tool.clientid)] = data['user']
                toolstate_db['{}:last_user_time'.format(self.tool.clientid)] = str(time.time())
            new_motd = self.tool.get_motd()
            if new_motd != self.last_motd:
                seq = self._seq()
                msg = {'cmd': 'motd', 'motd': new_motd, 'seq': seq}
                self.unacked[seq] = time.time(), msg
                yield msg
                self.last_motd = new_motd
        if data["cmd"] == "esp_data":
            if "sketch_md5" in data:
                mqtt_session.publish('{}/sketch_md5'.format(self.tool.mqtt_prefix), data['sketch_md5'], retain=True)


class ToolManager:
    def __init__(self):
        pass
    def get_session(self, *args, **kwargs):
        return ToolSession(*args, **kwargs)
    def get_tool(self, clientid, password):
        try:
            if tools[clientid]["password"] == password:
                return Tool(clientid)
        except KeyError:
            raise NotFound

if __name__ == "__main__":
    tm = ToolManager()
    t = tm.get_tool("ABCDEF", "password")
    print(t.auth_token("1234"))
    print(t.auth_token("0000"))
