#!/usr/bin/env python3

import json
import logging
import socket
import time
import queue
import random
import sys

sys.path.insert(0, 'lib')

from toolmanager import ToolManager, NotFound

incoming_buffer_size = 1500
listen_port = 33333

logging.basicConfig(level=logging.DEBUG)

tm = ToolManager()

class Client(object):
    def __init__(self, sock, address):
        self.sock = sock
        self.address = address
        self.min_retry = 0.150
        self.max_retry = 10
        self.timeout = 60
        self.timestamp = time.time()
        self.session = tm.get_session(address=self.address, protocol='UDP', resend_timeout=None)
        # outbound
        self.queue = queue.Queue()
        self.current_packet = None
        self.current_seq = random.randint(0, 65535)
        self.current_start = None
        self.current_attempt = 0
        self.last_send = 0
        self.current_retry = self.min_retry
        # inbound
        self.seen_seq = None
        # stats
        self.stats_send_attempts = 0
        self.stats_acks_received = 0
        self.stats_rtt_total = 0
    def __del__(self):
        logging.debug('{}:{} {} close'.format(self.address[0], self.address[1], self.session.logid))
        if self.session:
            self.session.close()
    def encode_msg(self, msg):
        return json.dumps(msg, separators=(',', ':'), indent=None).encode()
    def decode_msg(self, data):
        return json.loads(data.decode())
    def handle_ack(self, seq):
        if self.current_packet is None:
            logging.debug('{}:{} {} recv ack={} ignored'.format(self.address[0], self.address[1], self.session.logid, seq))
            return
        if seq == self.current_seq:
            rtt = time.time() - self.current_start
            logging.debug('{}:{} {} recv ack={} rtt={} attempts={}'.format(self.address[0], self.address[1], self.session.logid, seq, int(rtt*1000), self.current_attempt))
            self.stats_acks_received += 1
            self.stats_rtt_total += rtt
            self.current_packet = None
            self.current_start = None
            self.current_attempt = 0
    def handle_packet(self, seq, packet):
        if self.seen_seq is None or seq > self.seen_seq:
            logging.debug('{}:{} {} recv seq={} {}'.format(self.address[0], self.address[1], self.session.logid, seq, packet.decode()))
            self.seen_seq = seq
            self.receive(seq, packet)
        else:
            logging.debug('{}:{} {} recv seq={} duplicate'.format(self.address[0], self.address[1], self.session.logid, seq))
    def receive(self, seq, data):
        self.timestamp = time.time()
        try:
            msg = self.decode_msg(data)
            if msg == {}:
                return
            for response in self.session.handle_message(msg):
                if response:
                    packet = self.encode_msg(response)
                    logging.debug('{}:{} {} en-q {}'.format(self.address[0], self.address[1], self.session.logid, packet.decode()))
                    self.queue.put(packet)
                else:
                    return
        except json.decoder.JSONDecodeError:
            logging.exception('{}:{} {} recv-exception seq={} {}'.format(self.address[0], self.address[1], self.session.logid, seq, data))
        except Exception as e:
            logging.exception('{}:{} {} recv-exception seq={} {}'.format(self.address[0], self.address[1], self.session.logid, seq, data))
    def is_stale(self):
        if time.time() - self.timestamp > self.timeout:
            return True
    def outbound(self):
        self.periodic()
        if self.current_packet is None:
            try:
                self.current_packet = self.queue.get(False)
                self.current_seq = (self.current_seq + 1) & 65535
                self.current_retry = self.min_retry
                self.current_start = None
                self.last_send = None
                logging.debug('{}:{} {} de-q seq={} {}'.format(self.address[0], self.address[1], self.session.logid, self.current_seq, self.current_packet.decode()))
            except queue.Empty:
                pass
        if self.current_packet is None:
            return None, None
        if self.last_send is None or time.time() - self.last_send > self.current_retry:
            logging.debug('{}:{} {} send seq={} {}'.format(self.address[0], self.address[1], self.session.logid, self.current_seq, self.current_packet.decode()))
            self.last_send = time.time()
            if self.current_start is None:
                self.current_start = time.time()
                self.current_attempt = 0
            self.current_retry = min(self.current_retry * 1.2, self.max_retry)
            self.current_attempt += 1
            return self.current_seq, self.current_packet
        else:
            return None, None
    def periodic(self):
        try:
            for response in self.session.periodic():
                if response:
                    logging.debug('{}:{} {} en-q {}'.format(self.address[0], self.address[1], self.session.logid, response.decode()))
                    self.queue.put(self.encode_msg(response))
                else:
                    return
        except Exception as e:
            logging.exception('{} {} recv {}'.format(self.address, self.session.logid, data))
    def close(self):
        pass
        #self.sock.sendto(self.encode_msg({'cmd': 'bye'}), self.address)


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", listen_port))
    sock.settimeout(0.1)

    clients = {}

    try:
        while True:
            try:
                data, address = sock.recvfrom(incoming_buffer_size)
                if address not in clients:
                    clients[address] = Client(sock, address)
                client = clients[address]
                if len(data) == 2:
                    # Received ACK
                    seq = (data[0] << 8) + data[1]
                    client.handle_ack(seq)
                else:
                    # Received data
                    seq = (data[0] << 8) + data[1]
                    # send ack
                    sock.sendto(data[0:2], address)
                    # handle data
                    client.handle_packet(seq, data[2:])
                    # send response
                    txseq, txpacket = client.outbound()
                    if txseq is not None:
                        sock.sendto(bytes([txseq >> 8, txseq & 255]) + txpacket, address)
            except socket.timeout:
                for address in list(clients.keys()):
                    if clients[address].is_stale():
                        del clients[address]
                for address, client in clients.items():
                    txseq, txpacket = client.outbound()
                    if txseq is not None:
                        sock.sendto(bytes([txseq >> 8, txseq & 255]) + txpacket, address)
    except KeyboardInterrupt:
        pass

logging.basicConfig(level=logging.DEBUG)
main()
