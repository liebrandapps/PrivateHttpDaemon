


class Context:

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log

    def getLogger(self):
        return self.log

    def getConfig(self):
        return self.cfg
