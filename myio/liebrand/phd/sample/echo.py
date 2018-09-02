from myio.liebrand.phd.handler import Handler
from myio.liebrand.phd.server import Server, Daemon


class EchoHandler(Handler):

    def __init__(self):
        pass

    def endPoint(self):
        return(["/echo",])

    def doGET(self, path, headers):
        return [404, {}, ""]

    def doPOST(self, path, headers, body):
        resultHeaders = {}
        resultHeaders['Content-Type'] = "text/plain"
        return [200, resultHeaders, body]



if __name__ == '__main__':
    if len(sys.argv) > 1:
        todo = sys.argv[1]
        if todo in [ 'start', 'stop', 'restart', 'status' ]:
            pidFile = "./sample.pid"
            logFile = "./sample.log"
            d = Daemon(pidFile)
            d.startstop(todo, stdout=logFile, stderr=logFile)
    h = EchoHandler()
    s = Server(8000, [h, ])
    s.serve()