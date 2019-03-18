#!/usr/bin/env python3

import socketserver
import logging
import json
from toolmanager import ToolManager, NotFound

logging.basicConfig(level=logging.INFO)

tm = ToolManager()

class MyTCPHandler(socketserver.StreamRequestHandler):

    read_buffer = b''

    def buffered_read(self, count):
        logging.debug("buffered_read({})".format(count))
        while len(self.read_buffer) < count:
            logging.debug("reading more")
            self.read_buffer = self.read_buffer + self.rfile.read()
            logging.debug("buffer = {}".format(self.read_buffer))
        response = self.read_buffer[0:count]
        logging.debug("response = {}".format(response))
        self.read_buffer = self.read_buffer[count:]
        logging.debug("remaining buffer = {}...".format(self.read_buffer[0:10]))
        return response

    def msg_read(self):
        lenstr = b''
        next_byte = self.rfile.read(1) #self.buffered_read(1)
        if next_byte == b'':
            return None
        while next_byte in b'0123456789':
            lenstr = lenstr + next_byte
            next_byte = self.rfile.read(1) #self.buffered_read(1)
            if next_byte == b'':
                # EOF, no more data
                return None
        if lenstr == b'':
            # corrupt input stream, didn't get a number
            return None
        length = int(lenstr)
        if length < 2:
            # JSON document can't be less than 2 bytes long
            return None
        else:
            # Use the byte we've already read, plus length-1 more
            payload = next_byte + self.rfile.read(length-1) #self.buffered_read(length-1)
            #logging.debug("payload = {}".format(payload))
        return json.loads(payload.decode())

    def msg_write(self, data):
        payload = json.dumps(data, separators=(',', ':'), indent=None).encode()
        lenstr = str(len(payload)).encode()
        logging.debug("send> {}".format(lenstr + payload))
        self.wfile.write(lenstr + payload)

    def msg_write_multi(self, d):
        output = b""
        for data in d:
            payload = json.dumps(data, separators=(',', ':'), indent=None).encode()
            lenstr = str(len(payload)).encode()
            output += lenstr + payload
        logging.debug("send {} bytes> {}".format(len(output), output))
        self.wfile.write(output)

    def handle(self):
        session = tm.get_session()
        self.msg_write(session.greeting_message())
        while True:
            msg = self.msg_read()
            if msg is None:
                logging.info("closing")
                self.msg_write({"cmd": "bye"})
                session.close()
                return
            logging.info(json.dumps(msg, sort_keys=True, indent=2))
            try:
                for response in session.handle_message(msg):
                    if response is None:
                        session.close()
                        return
                    else:
                        self.msg_write(response)
            except Exception as e:
                logging.exception("Exception from handle_command")
                self.msg_write({"cmd": "bye", "error": "Exception"})
                session.close()

if __name__ == "__main__":
    HOST, PORT = "0.0.0.0", 13259

    logging.basicConfig(level=logging.INFO)

    # Create the server, binding to localhost on port 9999
    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer((HOST, PORT), MyTCPHandler)

    # Activate the server; this will keep running until you
    # interrupt the program with Ctrl-C
    server.serve_forever()
