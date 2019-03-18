import aiohttp
import binascii
import hashlib
import json
import logging
import time


class TokenAuthDatabase:
    def __init__(self, url, url_query, headers={}):
        self.url = url
        self.url_query = url_query
        self.headers = headers
        self.data = {}
        self.token_to_user = {}
        self.user_to_groups = {}
        self.md5_cache = {}

    def _md5_lookup(self, data, md5):
        for k, v in data.items():
            if k in self.md5_cache:
                pass
            else:
                self.md5_cache[k] = hashlib.md5(binascii.unhexlify(k)).hexdigest()
            if self.md5_cache[k] == md5:
                return k, v

    async def load(self):
        logging.debug("{} load()".format(time.time()))
        async with aiohttp.ClientSession() as session:
            async with session.get(self.url, headers=self.headers) as response:
                self.data = await response.json()
                logging.debug("{} got data".format(time.time()))
                self.token_to_user = {}
                self.user_to_groups = {}
                for user in self.data.keys():
                    for token in self.data[user]['tokens']:
                        self.token_to_user[token] = user
                    self.user_to_groups[user] = self.data[user]['groups']
                logging.debug("{} hashing complete".format(time.time()))
        #print(json.dumps(self.data, indent=2))
        #print(json.dumps(self.token_to_user, indent=2))
        #print(json.dumps(self.user_to_groups, indent=2))

    def auth_token(self, uid, groups=None):
        groups = groups or []
        if uid.startswith('md5:'):
            return self.auth_token_md5(uid[4:], groups)
        else:
            return self.auth_token_hex(uid, groups)

    def auth_token_hex(self, uid, groups=None):
        groups = groups or []
        if uid in self.token_to_user:
            username = self.token_to_user[uid]
            for group in groups:
                if group in self.user_to_groups[user]:
                    logging.info('token {} -> user {} -> group {} ok'.format(uid, username, group))
                    return {'uid': uid, 'name': username, 'access': 1}
            return {'uid': uid, 'name': username, 'access': 0}
        return None

    async def auth_token_hex_online(self, uid, groups=None):
        groups = groups or []
        async with aiohttp.ClientSession() as session:
            async with session.get(self.url_query.format(uid), headers=self.headers) as response:
                try:
                    response = await response.json()
                except aiohttp.client_exceptions.ContentTypeError:
                    return None
        username = response['username']
        for group in groups:
            if group in response['groups']:
                logging.info('token {} -> user {} -> group {} ok'.format(uid, username, group))
                return {'uid': uid, 'name': username, 'access': 1}
            return {'uid': uid, 'name': username, 'access': 0}
        return None

    #def auth_token_md5(self, md5, groups=None):
    #    groups = groups or []
    #    self.people_loader.check()
    #    for person in self.people_loader.data:
    #        lookup = self._md5_lookup(person['tokens'], md5)
    #        if lookup:
    #            uid = lookup[0]
    #            for group in person['groups']:
    #                if group in groups:
    #                    logging.info('token md5:{} -> {} -> user {} -> group {} ok'.format(md5, uid, person['username'], group))
    #                    return {'uid': uid, 'name': person['username'], 'access': 1}
    #            return {'uid': uid, 'name': person['username'], 'access': 0}
    #    return None

    def token_database_v1(self, groups=None):
        groups = groups or []
        uids = {}
        for username in self.data.keys():
            for group in self.data[username]['groups']:
                if group in groups:
                    for uid in self.data[username]['tokens']:
                        uids[uid] = True
        output = bytes([1]) # version 1
        for uid in sorted(uids.keys()):
            uid = binascii.unhexlify(uid)
            uidlen = len(uid)
            if uidlen == 4 or uidlen == 7:
                output += bytes([uidlen]) + uid
        return output

    def token_database_v2(self, groups=None, hash_length=4, salt=b''):
        groups = groups or []
        uids = {}
        for username in self.data.keys():
            for group in self.data[username]['groups']:
                if group in groups:
                    for uid in self.data[username]['tokens']:
                        uids[uid] = username
        output = bytes([2, hash_length, len(salt)])
        output += salt
        for hexuid in sorted(uids.keys()):
            uid = binascii.unhexlify(hexuid)
            uidlen = len(uid)
            if uidlen == 4 or uidlen == 7:
                output += hashlib.md5(salt + uid).digest()[0:hash_length]
                output += bytes([1])
                try:
                    user = uids[hexuid].encode('us-ascii')
                    output += bytes([len(user)])
                    output += user
                except UnicodeEncodeError:
                    output += bytes([0])
        return output
