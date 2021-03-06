import sys
import asyncio
import subprocess
import signal
import traceback

from artiq.protocols import pyon


class WorkerFailed(Exception):
    pass


class RunFailed(Exception):
    pass


class Worker:
    def __init__(self, handlers,
                 send_timeout=0.5, start_reply_timeout=1.0, term_timeout=1.0):
        self.handlers = handlers
        self.send_timeout = send_timeout
        self.start_reply_timeout = start_reply_timeout
        self.term_timeout = term_timeout

    @asyncio.coroutine
    def create_process(self):
        self.process = yield from asyncio.create_subprocess_exec(
            sys.executable, "-m", "artiq.master.worker_impl",
            stdout=subprocess.PIPE, stdin=subprocess.PIPE)

    @asyncio.coroutine
    def _send(self, obj, timeout):
        line = pyon.encode(obj)
        self.process.stdin.write(line.encode())
        self.process.stdin.write("\n".encode())
        try:
            fut = self.process.stdin.drain()
            if fut is not ():  # FIXME: why does Python return this?
                yield from asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise WorkerFailed("Timeout sending data from worker")
        except:
            raise WorkerFailed("Failed to send data to worker")

    @asyncio.coroutine
    def _recv(self, timeout):
        try:
            line = yield from asyncio.wait_for(
                self.process.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            raise WorkerFailed("Timeout receiving data from worker")
        if not line:
            raise WorkerFailed(
                "Worker ended unexpectedly while trying to receive data")
        try:
            obj = pyon.decode(line.decode())
        except:
            raise WorkerFailed("Worker sent invalid PYON data")
        return obj

    @asyncio.coroutine
    def run(self, run_params, result_timeout):
        yield from self._send(run_params, self.send_timeout)
        obj = yield from self._recv(self.start_reply_timeout)
        if obj != "ack":
            raise WorkerFailed("Incorrect acknowledgement")
        while True:
            obj = yield from self._recv(result_timeout)
            action = obj["action"]
            if action == "report_completed":
                if obj["status"] != "ok":
                    raise RunFailed(obj["message"])
                else:
                    return
            else:
                del obj["action"]
                try:
                    data = self.handlers[action](**obj)
                    reply = {"status": "ok", "data": data}
                except:
                    reply = {"status": "failed",
                             "message": traceback.format_exc()}
                yield from self._send(reply, self.send_timeout)

    @asyncio.coroutine
    def end_process(self):
        if self.process.returncode is not None:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            yield from asyncio.wait_for(
                self.process.wait(), timeout=self.term_timeout)
        except asyncio.TimeoutError:
            self.process.send_signal(signal.SIGKILL)
