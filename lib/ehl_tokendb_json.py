import binascii
import hashlib
import logging

import jsonloader

class TokenAuthDatabase:
    def __init__(self, people_json):
        self.people_loader = jsonloader.JsonLoader(people_json)
        self.md5_cache = {}

    def _md5_lookup(self, data, md5):
        for k, v in data.items():
            if k in self.md5_cache:
                pass
            else:
                self.md5_cache[k] = hashlib.md5(binascii.unhexlify(k)).hexdigest()
            if self.md5_cache[k] == md5:
                return k, v

    def auth_token(self, uid, groups=None):
        groups = groups or []
        if uid.startswith('md5:'):
            return self.auth_token_md5(uid[4:], groups)
        else:
            return self.auth_token_hex(uid, groups)

    def auth_token_hex(self, uid, groups=None):
        groups = groups or []
        self.people_loader.check()
        for person in self.people_loader.data:
            if uid in person['tokens']:
                for group in person['groups']:
                    if group in groups:
                        logging.info('token {} -> user {} -> group {} ok'.format(uid, person['username'], group))
                        return {'uid': uid, 'name': person['username'], 'access': 1}
                return {'uid': uid, 'name': person['username'], 'access': 0}
        return None

    def auth_token_md5(self, md5, groups=None):
        groups = groups or []
        self.people_loader.check()
        for person in self.people_loader.data:
            lookup = self._md5_lookup(person['tokens'], md5)
            if lookup:
                uid = lookup[0]
                for group in person['groups']:
                    if group in groups:
                        logging.info('token md5:{} -> {} -> user {} -> group {} ok'.format(md5, uid, person['username'], group))
                        return {'uid': uid, 'name': person['username'], 'access': 1}
                return {'uid': uid, 'name': person['username'], 'access': 0}
        return None

    def token_database_v1(self, groups=None):
        groups = groups or []
        uids = {}
        self.people_loader.check()
        for person in self.people_loader.data:
            for group in person['groups']:
                if group in groups:
                    for uid in person['tokens']:
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
        self.people_loader.check()
        for person in self.people_loader.data:
            for group in person['groups']:
                if group in groups:
                    for uid in person['tokens']:
                        uids[uid] = person['username']
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
