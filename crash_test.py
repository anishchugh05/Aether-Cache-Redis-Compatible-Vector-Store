"""
Crash test for the AOF: does every write the server acknowledged survive a kill -9?

1. Starts the server with AOF on, in a fresh data folder
2. A writer sends SET key:i i as fast as it can and records every key the server answered OK to
3. After a random 2-4 seconds, kills the server with SIGKILL (kill -9: no chance to clean up)
4. Restarts the server on the same data folder, so it replays the AOF
5. Checks that every acknowledged key came back with the right value

Usage: python crash_test.py [always|everysec|no] [rounds]
Run it from the project folder (where src/main/java/Main.java is).
"""
import os, random, shutil, signal, socket, subprocess, sys, tempfile, threading, time

PORT = 6390
MODE = sys.argv[1] if len(sys.argv) > 1 else "everysec"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3

def start_server(data_dir):
    proc = subprocess.Popen(
        ["java", "src/main/java/Main.java", "--port", str(PORT), "--dir", data_dir,
         "--appendonly", "yes", "--appendfsync", MODE],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + 30
    while time.time() < deadline:  # wait until it accepts connections
        try:
            socket.create_connection(("localhost", PORT), timeout=0.5).close()
            return proc
        except OSError:
            if proc.poll() is not None:
                print(proc.stdout.read().decode())
                raise RuntimeError("server exited during startup")
            time.sleep(0.2)
    raise RuntimeError("server didn't start in time")

def resp(*parts):
    out = b"*%d\r\n" % len(parts)
    for p in parts:
        p = str(p).encode()
        out += b"$%d\r\n%s\r\n" % (len(p), p)
    return out

def writer(acked, stop):
    """Sends SETs one at a time; a key counts as acknowledged only once +OK comes back."""
    try:
        s = socket.create_connection(("localhost", PORT))
        f = s.makefile("rb")
        i = 0
        while not stop.is_set():
            s.sendall(resp("SET", f"key:{i}", i))
            if f.readline() != b"+OK\r\n":
                break
            acked.append(i)
            i += 1
    except OSError:
        pass  # connection died because we killed the server: expected

def check(acked):
    s = socket.create_connection(("localhost", PORT))
    f = s.makefile("rb")
    missing = 0
    for i in acked:
        s.sendall(resp("GET", f"key:{i}"))
        header = f.readline()
        if header.startswith(b"$-1"):
            missing += 1
            continue
        value = f.read(int(header[1:]) + 2)[:-2]
        if value != str(i).encode():
            missing += 1
    return missing

def one_round(n):
    data_dir = tempfile.mkdtemp(prefix="crashtest_")
    try:
        server = start_server(data_dir)
        acked, stop = [], threading.Event()
        t = threading.Thread(target=writer, args=(acked, stop))
        t.start()
        time.sleep(random.uniform(2, 4))
        os.kill(server.pid, signal.SIGKILL)  # the crash
        server.wait()
        stop.set()
        t.join()

        start = time.time()
        server = start_server(data_dir)  # replays the AOF
        recovery = time.time() - start
        missing = check(acked)
        server.terminate()
        server.wait()
        status = "PASS" if missing == 0 else "FAIL"
        print(f"round {n}: {len(acked)} acknowledged writes, {missing} missing after restart, "
              f"restart took {recovery:.1f}s -> {status}")
        return missing == 0
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)

if __name__ == "__main__":
    print(f"Crash test, appendfsync={MODE}, {ROUNDS} rounds")
    results = [one_round(n + 1) for n in range(ROUNDS)]
    print(f"\n{sum(results)}/{ROUNDS} rounds passed")