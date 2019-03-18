#!/usr/bin/env python3

import json
import logging
import socket
import time
from toolmanager import ToolManager, NotFound

incoming_buffer_size = 1500
listen_port = 13259

logging.basicConfig(level=logging.DEBUG)

tm = ToolManager()

class Client(object):
    def __init__(self, sock, address, timeout=60):
        self.sock = sock
        self.address = address
        self.timeout = timeout
        self.timestamp = time.time()
        self.session = tm.get_session(address=self.address, protocol='UDP')
    def __del__(self):
        if self.session:
            self.session.close()
    def encode_msg(self, msg):
        #msg["padding"] = ""
        #l = len(json.dumps(msg, separators=(',', ':'), indent=None).encode())
        #target_size = 1024
        #if l < target_size:
        #    msg["padding"] = "." * (target_size-l)
        return json.dumps(msg, separators=(',', ':'), indent=None).encode()
    def decode_msg(self, data):
        return json.loads(data.decode())
    def receive(self, data):
        #print("{} {} recv {}".format(self, self.address, data))
        self.timestamp = time.time()
        try:
            msg = self.decode_msg(data)
            logging.info('{} {} recv {}'.format(self.address, self.session.logid, msg))
            if msg == {}:
                return
            for response in self.session.handle_message(msg):
                if response:
                    logging.info('{} {} send {}'.format(self.address, self.session.logid, response))
                    self.sock.sendto(self.encode_msg(response), self.address)
                else:
                    return
        except json.decoder.JSONDecodeError:
            logging.exception('{} {} recv {}'.format(self.address, self.session.logid, data))
        except Exception as e:
            logging.exception('{} {} recv {}'.format(self.address, self.session.logid, data))
    def is_stale(self):
        if time.time() - self.timestamp > self.timeout:
            return True
    def periodic(self):
        try:
            for response in self.session.periodic():
                if response:
                    logging.info('{} {} re-send {}'.format(self.address, self.session.logid, response))
                    self.sock.sendto(self.encode_msg(response), self.address)
                else:
                    return
        except Exception as e:
            logging.exception('{} {} recv {}'.format(self.address, self.session.logid, data))
    def close(self):
        self.sock.sendto(self.encode_msg({'cmd': 'bye'}), self.address)


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", listen_port))
    sock.settimeout(1)

    clients = {}

    try:
        while True:
            try:
                data, address = sock.recvfrom(incoming_buffer_size)
                try:
                    client = clients[address]
                except:
                    logging.info("{}:{} connected".format(*address))
                    client = Client(sock, address)
                    clients[address] = client
                client.receive(data)
            except socket.timeout:
                for address in list(clients.keys()):
                    if clients[address].is_stale():
                        logging.info("{}:{} connection timed-out".format(*address))
                        del clients[address]
                for client in clients.values():
                    client.periodic()
    except KeyboardInterrupt:
        for client in clients.values():
            client.close()

logging.basicConfig(level=logging.INFO)
main()
