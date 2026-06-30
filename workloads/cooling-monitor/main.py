"""cooling-monitor: journals thermal logs to the shared PVC; S1 trigger = a sustained, fsync-heavy
fio storm that contends the shared physical disk and stalls co-located pods (timescaledb/dcim) via PSI io.

Trigger (either works; both just create the FLUSH flag):
  - scenario script / CLI:  kubectl exec ... -- touch /shared/cooling/FLUSH   (scenarios/S1/trigger.sh)
  - dashboard / HTTP (L4):  POST http://cooling-monitor.factory-data:8080/flush
fio intensity is env-tunable with NO rebuild: FIO_SIZE/JOBS/RUNTIME/FSYNC/DIRECT (Helm values).
The heavy load runs ONLY on trigger; steady state is a light ~64KB/s journal.
"""
import os, subprocess, time, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

DIR         = os.environ.get("DATA_DIR", "/shared/cooling")
FLUSH_FLAG  = os.path.join(DIR, "FLUSH")
FIO_SIZE    = os.environ.get("FIO_SIZE", "512m")    # per-job file size (x JOBS must fit the PVC)
FIO_JOBS    = os.environ.get("FIO_JOBS", "4")       # concurrent writers
FIO_RUNTIME = os.environ.get("FIO_RUNTIME", "45")   # sustained seconds (time_based)
FIO_FSYNC   = os.environ.get("FIO_FSYNC", "8")      # fsync every N writes (lower = more stall)
FIO_DIRECT  = os.environ.get("FIO_DIRECT", "1")     # O_DIRECT: real device I/O, not page cache
PORT        = int(os.environ.get("TRIGGER_PORT", "8080"))

os.makedirs(DIR, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.rstrip("/") == "/flush":
            open(FLUSH_FLAG, "w").close()
            self._reply(202, "S1 armed\n")
        else:
            self._reply(404, "not found\n")

    def do_GET(self):
        self._reply(200, "ok\n") if self.path == "/healthz" else self._reply(404, "not found\n")

    def _reply(self, code, body):
        self.send_response(code); self.end_headers(); self.wfile.write(body.encode())

    def log_message(self, *_):  # keep stdout for the journal/fio prints
        pass


def serve():
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def main():
    threading.Thread(target=serve, daemon=True).start()
    print(f"cooling-monitor up; flush flag={FLUSH_FLAG}; trigger POST :{PORT}/flush", flush=True)
    while True:
        with open(os.path.join(DIR, "thermal.log"), "a") as f:  # steady ~64KB/s journal
            f.write(f"{time.time()} zone=A temp={42.0 + (time.time() % 7):.2f}\n" * 800)
            f.flush(); os.fsync(f.fileno())
        if os.path.exists(FLUSH_FLAG):
            os.remove(FLUSH_FLAG)
            print(f"S1 TRIGGER: sustained fio ({FIO_JOBS}x{FIO_SIZE}, {FIO_RUNTIME}s, fsync={FIO_FSYNC}, direct={FIO_DIRECT})", flush=True)
            subprocess.run([
                "fio", "--name=thermalflush", f"--directory={DIR}", "--rw=write", "--bs=1M",
                f"--size={FIO_SIZE}", f"--numjobs={FIO_JOBS}", f"--fsync={FIO_FSYNC}",
                f"--direct={FIO_DIRECT}", "--ioengine=libaio", "--time_based",
                f"--runtime={FIO_RUNTIME}", "--group_reporting", "--unlink=1",
            ], check=False)
            print("S1 flush complete", flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
