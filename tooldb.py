import logging

import yamlloader


class ToolDB:

    def __init__(self, tools_yaml):
        self.tool_loader = yamlloader.YamlLoader(tools_yaml, defaults_key='DEFAULTS')

    def authenticate(self, clientid, password):
        self.tool_loader.check()
        if clientid in self.tool_loader.data:
            if password == self.tool_loader.data[clientid]['password']:
                return True
        return False
    
    def get_value(self, clientid, k, default=None):
        self.tool_loader.check()
        return self.tool_loader.data[clientid].get(k, default)

    def get_config(self, clientid):
        return self.get_value(clientid, 'config')
