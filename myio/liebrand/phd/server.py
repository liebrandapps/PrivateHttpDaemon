import errno
import os
import re
import select
import signal
import socket
import sys
import time
from BaseHTTPServer import BaseHTTPRequestHandler
from StringIO import StringIO
from datetime import datetime
import threading

import psutil

from myio.liebrand.phd.util.misc import httpDate

globalBlockList = {}

class BlockedIP:

    def __init__(self, ip):
        self.ip = ip
        self.lastSeen = datetime.now()

    def updateLastSeen(self):
        self.lastSeen = datetime.now()

    def getLastSeen(self):
        return self.lastSeen

    def getAddress(self):
        return(self.ip)


class HTTPRequest(BaseHTTPRequestHandler):

    def __init__(self, request_text):
        self.rfile = StringIO(request_text)
        self.raw_requestline = self.rfile.readline()
        self.error_code = self.error_message = None
        self.parse_request()


    def send_error(self, code, message=None):
        self.error_code = code
        if message is not None:
            self.error_message = message


class HttpProcessor(threading.Thread):

    def __init__(self, clientSocket, address, handler, log):
        super(HttpProcessor, self).__init__()
        self.clientSocket = clientSocket
        self.log = log
        self.handler = handler
        self.address = address
        self.timeout = 0.4


    def recvall(self):
        # setup to use non-blocking sockets
        # if no data arrives it assumes transaction is done
        # recv() returns a string
        self.clientSocket.setblocking(0)
        total_data = []
        data = ''
        begin = time.time()
        while 1:
            # if you got some data, then break after wait sec
            if total_data and time.time() - begin > self.timeout:
                break
            # if you got no data at all, wait a little longer
            elif time.time() - begin > self.timeout * 4:
                break
            try:
                data = self.clientSocket.recv(4096)
                if data:
                    total_data.append(data)
                    begin = time.time()
                else:
                    time.sleep(self.timeout)
            except socket.error, e:
                if e.args[0] == errno.EWOULDBLOCK:
                    time.sleep(0.2)
                else:
                    self.log.exception(e)
            # When a recv returns 0 bytes, other side has closed
        result = ''.join(total_data)
        return result



    def run(self):
        #print time.time()
        allData = self.recvall()
        #print time.time()
        #pf = file("/tmp/rq.txt", 'w')
        #pf.write(allData)
        #pf.close()

        request = HTTPRequest(allData)
        length = 0
        if request.headers.has_key('content-length'):
            length = int(request.headers['content-length'])
        contentType=None
        if request.headers.has_key('content-type'):
            contentType = request.headers['content-type']
        body = request.rfile.read(length)

        if length != len(body):
            self.log.debug("[PHD] Incomplete body. Need to read %d more bytes" % (length-len(body)))
            prevLen = -1
            while len(body)!=length and prevLen!=len(body):
                prevLen = len(body)
                moreData = self.recvall()
                allData += moreData
        processed=False
        responseHeaders = None
        responseBody = None
        responseCode = 404
        #self.log.debug(request.path)
        for h in self.handler:
            for e in h.endPoint():
                #self.log.debug(e)
                if e.upper() in request.path.upper() or "*" == e:
                    if "POST" == request.command:
                        (responseCode, responseHeaders, responseBody) = h.doPOST(request.path, request.headers, body)
                        processed = True
                    if "GET" == request.command:
                        (responseCode, responseHeaders, responseBody) = h.doGET(request.path, request.headers)
                        processed = True
                    break
            if processed:
                break
        if processed:
            if responseCode == 200:
                msgToSnd = self.createResponse(200, request.responses[200], body=responseBody,
                                             headers=responseHeaders)
                bytesTotal = len(msgToSnd)
                sent = 0
                while sent < bytesTotal:
                    done = False
                    while not done:
                        try:
                            sent += self.clientSocket.send(msgToSnd[sent:])
                            done = True
                        except socket.error, e:
                            if e.args[0] == errno.EWOULDBLOCK:
                                time.sleep(0.2)

            else:
                self.clientSocket.sendall(self.createResponse(responseCode, request.responses[responseCode]))
        else:
            self.log.debug("[PHD] Path %s did not match any handler" % (request.path))
            self.clientSocket.sendall(self.createResponse(404, request.responses[404]))
            b = BlockedIP(self.address)
            globalBlockList[self.address] = b
        self.clientSocket.close()
        self.log.debug("[PHD] Closing connection to %s" % (self.address, ))


    def createResponse(self, code, text, body=None, headers=None):
        data = []
        if headers is None:
            headers = {}
        data.append("HTTP/1.1 %d %s\r\n" % (code, text))
        if body is not None:
            headers["Content-Length"] = str(len(body))
        headers["Date"] = httpDate(datetime.now())
        for k in headers.keys():
            data.append("%s: %s\r\n" % (k, headers[k]))

        data.append("\r\n")
        sb = StringIO(2048)
        sb.writelines(data)
        if body is not None:
            sb.write(body)
        return sb.getvalue()


class Server:

    def __init__(self, port, handler, logger):
        self.handler = handler
        self.port = port
        self.log = logger
        self._terminate = False
        self.srvSocket = None
        self.blockList = {}


    def serve(self):
        self.log.info("[PHD] Starting Server Thread")
        signal.signal(signal.SIGTERM, self.terminate)
        signal.signal(signal.SIGINT, self.terminate)
        socketArray = []
        self.srvSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srvSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srvSocket.bind(('', self.port))
        self.srvSocket.listen(5)
        self.controlPipe = os.pipe()
        socketArray.append(self.controlPipe[0])
        socketArray.append(self.srvSocket)
        while not self._terminate:
            try:
                readableSocket, writable, errored = select.select(socketArray, [], [], 180)
                for rS in readableSocket:
                    if rS is self.srvSocket:
                        (clientSocket, address) = self.srvSocket.accept()
                        strAddress = address[0] + ":" + str(address[1])
                        self.log.debug("[PHD] Connection from %s", strAddress)
                        if strAddress in globalBlockList:
                            globalBlockList[address].updateLastSeen()
                            clientSocket.close()
                            self.log.debug("Blocking request from %s" % (strAddress))
                            continue
                        proc=HttpProcessor(clientSocket, strAddress, self.handler, self.log)
                        proc.start()
                    if rS is self.controlPipe:
                        os.read(self.controlPipe[0], 1)
                        continue
                if len(readableSocket) == 0:
                    # timeout, check the blocked addresses
                    now = datetime.now()
                    for a in globalBlockList.keys():
                        b = globalBlockList[a]
                        if (now - b.getLastSeen()).seconds > 360:
                            del globalBlockList[b.getAddress()]
            except select.error, (_errno, _strerror):
                if _errno == errno.EINTR:
                    continue

        self.log.info("[PHD] Terminating Server Thread")

    def terminate(self, sigNo, stackFrame):
        if sigNo == signal.SIGINT:
            self.log.info("[PHD] Terminating upon Keyboard Interrupt")
        if sigNo == signal.SIGTERM:
            self.log.info("[PHD] Terminating upon Signal Term")
        self._terminate = True
        os.write(self.controlPipe[1], 'x')
        self.srvSocket.close()


class Daemon:

    def __init__(self, pidFile):
        self.pidFile = pidFile

    def getTimeStamp(self):
        return time.strftime('%d.%m.%Y %H:%M:%S',  time.localtime(time.time()))

    def printLogLine(self, file, message):
        file.write('%s %s\n' % (self.getTimeStamp(), message))
        file.flush()

    def startstop(self, todo, stdout="/dev/null", stderr=None, stdin="/dev/null"):
        try:
            pf = file(self.pidFile, 'r')
            pid = int(pf.read().strip())
            pf.close()
        except IOError:
            pid = None

        if 'stop' == todo or 'restart' == todo:
            if not pid:
                msg = "[PHD] Could not stop. Pidfile %s is missing\n"
                self.printLogLine(sys.stderr, msg % self.pidFile)
                sys.exit(1)
            self.printLogLine(sys.stdout, "Stopping Process with PID %d" % pid);
            try:
                cnt = 10
                while 1:
                    if cnt < 0:
                        os.kill(pid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGTERM)
                    time.sleep(3)
                    cnt -= 1
                self.printLogLine("[PHD] Server refuses to terminate ...")
            except OSError, err:
                err = str(err)
                if err.find("No such process") > 0:
                    if "stop" == todo:
                        if os.path.exists(self.pidFile):
                            os.remove(self.pidFile)
                        sys.exit(0)
                    todo = "start"
                    pid = None
                else:
                    print str(err)
                    sys.exit(1)
        if 'start' == todo:
            if pid:
                msg = "[PHD] Start aborted since Pidfile %s exists\n"
                self.printLogLine(sys.stderr, msg % self.pidFile)
                sys.exit(1)
            self.printLogLine(sys.stdout, "Starting Process as Daemon");
            self.daemonize(stdout, stderr, stdin)
        if 'status' == todo:
            if pid:
                if psutil.pid_exists(pid):
                    process = psutil.Process(pid)
                    with process.oneshot():
                        msg = "[PHD] Process with pid %d is running [%s]" % (pid, process.name())
                        self.printLogLine(sys.stdout, msg);
                        sys.exit(0)
                else:
                    msg = "[PHD] Process with pid %d is NOT running, but we have a PID file - may it crashed." % (pid,)
                    self.printLogLine(sys.stdout, msg)
                    if os.path.exists(self.pidFile):
                        os.remove(self.pidFile)
                        sys.exit(3)
            else:
                msg = "[PHD] Process seems to be not running - no PIDFile (%s) found."
                self.printLogLine(sys.stderr, msg % self.pidFile)
                sys.exit(0)


    def daemonize(self, stdout='/dev/null', stderr=None, stdin='/dev/null'):
        if not stderr:
            stderr = stdout
        si = file(stdin, 'r')
        so = file(stdout, 'a+')
        se = file(stderr, 'a+')

        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())

        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError, e:
            sys.stderr.write("[PHD] fork #1 failed (%d) %s" % (e.errno, e.strerror))
            sys.exit(1)

        os.umask(0)
        os.setsid()

        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError, e:
            sys.stderr.write("[PHD] fork #2 failed (%d) %s" % (e.errno, e.strerror))
            sys.exit(1)
        pid = str(os.getpid())
        self.printLogLine(sys.stdout, "[PHD] Process started as Daemon with pid %s" % pid)
        if self.pidFile:
            file(self.pidFile, 'w+').write('%s\n' % pid)


if __name__ == '__main__':
    #s = Server(8000, None)
    #s.serve()
    pass
