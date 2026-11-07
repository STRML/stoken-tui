#!/usr/bin/env python3
"""End-to-end scenarios for stoken-tui. One scenario per failure-matrix row (docs/plan.md).

Runs against a temp token dir and a throwaway Keychain service, never touching ~/.stoken or the real
Keychain items. `stoken` and `pbcopy` are wrapped by logging shims so the suite can prove what reached
their argv. Writes tests/e2e-results.txt. Needs the real binaries, so run it outside any sandbox.
"""
import base64
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.environ.get("STOKEN_TUI_BIN") or os.path.join(ROOT, "stoken-tui")
RESULTS = os.environ.get("STOKEN_TUI_RESULTS") or os.path.join(ROOT, "tests", "e2e-results.txt")
T = 1700000000
LEFT = 60 - (T % 60)
PIN = "73915"

work = tempfile.mkdtemp(prefix="stoken-tui-e2e-")
service = "stoken-tui-e2e-%d" % os.getpid()
tokdir = os.path.join(work, "tokens")
shimdir = os.path.join(work, "shims")
shimlog = os.path.join(work, "argv.log")
REAL_STOKEN = shutil.which("stoken")
BASE_PATH = os.environ["PATH"]

os.makedirs(shimdir)
SHIMS = {
    "stoken": ('#!/bin/bash\n{ printf "stoken"; for a in "$@"; do printf " [%%s]" "$a"; done; echo; } >> "$SHIM_LOG"\n'
               'if [ "$1" = "export" ] && [ -n "$SHIM_STOKEN_SLEEP" ]; then sleep 8; fi\n'
               'exec %s "$@"\n' % REAL_STOKEN),
    "pbcopy": '#!/bin/bash\ncat > "$SHIM_CLIP"\ncat "$SHIM_CLIP" >> "$(dirname "$SHIM_CLIP")/clip.log"; echo >> "$(dirname "$SHIM_CLIP")/clip.log"\n',
    "pbpaste": '#!/bin/bash\ncat "$SHIM_CLIP" 2>/dev/null\n',
    "security": ('#!/bin/bash\n{ printf "security"; for a in "$@"; do printf " [%s]" "$a"; done; echo; } >> "$SHIM_LOG"\n'
                 'if [ "$1" = "-i" ] && [ -n "$SHIM_SECURITY_SLEEP" ]; then sleep 4; fi\n'
                 'if [ "$1" = "-i" ] && [ -n "$SHIM_SECURITY_DROP_WRITES" ]; then cat >/dev/null; exit 0; fi\n'
                 'if [ "$1" = "find-generic-password" ] && [ -n "$SHIM_SECURITY_FIND_EXIT" ]; then exit "$SHIM_SECURITY_FIND_EXIT"; fi\n'
                 'exec /usr/bin/security "$@"\n'),
}
for name, body in SHIMS.items():
    p = os.path.join(shimdir, name)
    open(p, "w").write(body)
    os.chmod(p, 0o755)
open(shimlog, "w").close()

env = dict(os.environ, STOKEN_TUI_SECURITY=os.path.join(shimdir, "security"), STOKEN_TUI_DIR=tokdir,
           STOKEN_TUI_SERVICE=service, STOKEN_TUI_PIN="", STOKEN_TUI_AUTH="/usr/bin/true", SHIM_LOG=shimlog,
           SHIM_CLIP=os.path.join(work, "clip.txt"), PATH=shimdir + os.pathsep + BASE_PATH)
for k in ("STOKEN_TUI_PASSWORD", "SHIM_SECURITY_DROP_WRITES", "SHIM_SECURITY_FIND_EXIT", "SHIM_SECURITY_SLEEP",
          "SHIM_STOKEN_SLEEP"):
    env.pop(k, None)

passed, failed, log = [], [], []
secrets_seen = []  # every secret the suite learns; none may ever appear in a logged argv
accounts_to_clean = set()
extra_dirs = []


def run(*args, extra_env=None, stdin=None, path=None):
    e = dict(env, **(extra_env or {}))
    if path is not None:
        e["PATH"] = path
    p = subprocess.run([sys.executable, "-I", TOOL, *args], env=e, input=stdin, capture_output=True, text=True,
                       timeout=60)
    return p.returncode, p.stdout, p.stderr


def sh(*args, stdin=None):
    p = subprocess.run(list(args), input=stdin, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def service_of(pin):
    return service + (".pin" if pin else "")


def keychain_get(account, pin=False):
    rc, out, _ = sh("/usr/bin/security", "find-generic-password", "-s", service_of(pin), "-a", account, "-w")
    return out if rc == 0 else None


def keychain_set(account, value, pin=False):
    sh("/usr/bin/security", "add-generic-password", "-U", "-s", service_of(pin), "-a", account, "-w", value)
    accounts_to_clean.add(account)


def keychain_del(account, pin=False):
    return sh("/usr/bin/security", "delete-generic-password", "-s", service_of(pin), "-a", account)[0] == 0


def pinless_fixture(name, seed_byte):
    seed = base64.b64encode(bytes([seed_byte]) * 16).decode()
    tmpl = os.path.join(work, name + ".xml")
    with open(tmpl, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<TKNBatch>\n<TKNHeader><Origin>E2E</Origin>'
                '<DefPinType>0</DefPinType><DefLocalPIN>0</DefLocalPIN><DefAddPIN>0</DefAddPIN>'
                '<DefDigits>6</DefDigits><DefInterval>60</DefInterval></TKNHeader>\n'
                '<TKN><SN>00000000%04d</SN><UserLogin>e2e</UserLogin><Death>2035/12/31</Death>'
                '<Seed>=%s</Seed></TKN>\n</TKNBatch>\n' % (seed_byte, seed))
    path = os.path.join(work, name + ".sdtid")
    with open(path, "w") as out:
        subprocess.run([REAL_STOKEN, "export", "--random", "--sdtid", "--template", tmpl], stdout=out, check=True,
                       stdin=subprocess.DEVNULL)
    return path


def at(sdtid, t):
    return sh(REAL_STOKEN, "tokencode", "--batch", "--use-time=%d" % t, "--file=" + sdtid)[1]


def index_names():
    path = os.path.join(tokdir, "index")
    return open(path).read().split() if os.path.exists(path) else []


def frame():
    return run("--use-time", str(T), "--once")


def row_for(out, alias):
    for line in out.splitlines():
        if line.split() and line.split()[0] == alias:
            return line
    return None


ROW = re.compile(r"^\S+\s+(\d+(?: \d+)?)\s+(\d+)s\b")


def digits_of(row):
    m = ROW.match(row or "")
    return m.group(1).replace(" ", "") if m else None


def left_of(row):
    m = ROW.match(row or "")
    return int(m.group(2)) if m else None


def scenario(name):
    def deco(fn):
        try:
            fn()
            passed.append(name)
            log.append("PASS  " + name)
        except AssertionError as e:
            failed.append(name)
            log.append("FAIL  %s: %s" % (name, e))
        except Exception as e:  # a crash is a red scenario too
            failed.append(name)
            log.append("FAIL  %s: crashed %r" % (name, e))
        return fn
    return deco


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


fx_a = pinless_fixture("a", 0x11)
fx_b = pinless_fixture("b", 0x22)
exp_a, exp_b = at(fx_a, T), at(fx_b, T)
ctf_a = sh(REAL_STOKEN, "export", "--file=" + fx_a)[1]
ctf_b = sh(REAL_STOKEN, "export", "--file=" + fx_b)[1]
pin_tok = sh(REAL_STOKEN, "export", "--random", "--iphone")[1].splitlines()[0]
pin_data = pin_tok.split("ctfData=")[1]
ctf_pin = sh(REAL_STOKEN, "export", "--token=" + pin_tok)[1]
exp_pin = sh(REAL_STOKEN, "tokencode", "--batch", "--pin=" + PIN, "--use-time=%d" % T, "--token=" + pin_tok)[1]
sdtid_chunk = open(fx_a).read().split("<Seed>")[1][:40] if "<Seed>" in open(fx_a).read() else open(fx_a).read()[600:640]
secrets_seen.extend([ctf_a, ctf_b, ctf_pin, pin_data, PIN, sdtid_chunk])


@scenario("S1 add from file: seed in Keychain, only an alias index on disk, code matches stoken")
def _():
    rc, out, err = run("add", "alpha", "--file", fx_a)
    check(rc == 0, "add exit %d: %s" % (rc, err))
    accounts_to_clean.add("alpha")
    check(keychain_get("alpha") == ctf_a, "Keychain does not hold the 81-digit seed")
    check(sorted(os.listdir(tokdir)) == [".lock", "index"], "unexpected files: %r" % os.listdir(tokdir))
    check(stat.S_IMODE(os.stat(tokdir).st_mode) == 0o700, "dir not 0700")
    check(stat.S_IMODE(os.stat(os.path.join(tokdir, "index")).st_mode) == 0o600, "index not 0600")
    check(index_names() == ["alpha"], "index %r" % index_names())
    check(ctf_a not in out + err, "add printed the seed")
    check("original token" in (out + err).lower(), "no reminder to keep the original token: %r" % (out + err))
    rc, out, err = run("code", "alpha", "--use-time", str(T))
    check(rc == 0 and out.strip() == exp_a, "code %r != %r" % (out.strip(), exp_a))


@scenario("S2 two tokens listed; countdown matches stoken's real window edges")
def _():
    rc, _, err = run("add", "beta", "--file", fx_b)
    check(rc == 0, "add beta: " + err)
    accounts_to_clean.add("beta")
    check(exp_a != exp_b, "fixtures collide")
    rc, out, err = frame()
    check(rc == 0, "frame exit %d: %s" % (rc, err))
    for alias, want in (("alpha", exp_a), ("beta", exp_b)):
        row = row_for(out, alias)
        check(row is not None, alias + " row missing in:\n" + out)
        check(digits_of(row) == want, "%s shows %r want %r" % (alias, digits_of(row), want))
        check(left_of(row) == LEFT, "%s shows %r s left, want %d: %r" % (alias, left_of(row), LEFT, row))
    last, nxt = at(fx_a, T + LEFT - 1), at(fx_a, T + LEFT)
    check(last == exp_a and nxt != exp_a, "countdown does not match stoken's window: %s %s %s" % (exp_a, last, nxt))


@scenario("S3 duplicate alias refused; the stored seed and the index are untouched")
def _():
    rc, _, _ = run("add", "alpha", "--file", fx_b)
    check(rc != 0, "duplicate add succeeded")
    check(keychain_get("alpha") == ctf_a, "seed changed")
    check(index_names() == ["alpha", "beta"], "index %r" % index_names())


@scenario("S4 garbled token: nonzero, nothing stored, no index entry, no stray files")
def _():
    rc, _, _ = run("add", "junk", "--token", "-", stdin="com.rsa.securid.iphone://ctf?ctfData=123\n")
    check(rc != 0, "garbled import succeeded")
    check(keychain_get("junk") is None, "Keychain item left behind")
    check("junk" not in index_names(), "index entry left behind")
    check(sorted(os.listdir(tokdir)) == [".lock", "index"], "stray files: %r" % os.listdir(tokdir))


@scenario("S5 hostile aliases rejected before any write")
def _():
    for bad in ("../evil", "a b", ".hid", "x/y", "", "a" * 65, "a\n"):
        rc, _, _ = run("add", bad, "--file", fx_a)
        check(rc != 0, "alias %r accepted" % bad)
    check(index_names() == ["alpha", "beta"], "index changed: %r" % index_names())
    check(not os.path.exists(os.path.join(work, "evil")), "path traversal wrote something")


@scenario("S6 missing Keychain seed: that row errors, others still show codes")
def _():
    seed = keychain_get("beta")
    check(keychain_del("beta"), "could not delete test item")
    try:
        rc, out, _ = frame()
        check(rc == 0, "frame exit %d" % rc)
        check(digits_of(row_for(out, "alpha")) == exp_a, "alpha broke")
        check("no seed in Keychain" in (row_for(out, "beta") or ""), "beta row: %r" % row_for(out, "beta"))
    finally:
        keychain_set("beta", seed)


@scenario("S7 unreadable seed in the Keychain: that row errors, others still show codes")
def _():
    seed = keychain_get("beta")
    keychain_set("beta", "123")
    try:
        rc, out, _ = frame()
        check(rc == 0, "frame exit %d" % rc)
        check(digits_of(row_for(out, "alpha")) == exp_a, "alpha broke")
        check("seed unreadable" in (row_for(out, "beta") or ""), "beta row: %r" % row_for(out, "beta"))
    finally:
        keychain_set("beta", seed)
    rc, out, _ = frame()
    check(digits_of(row_for(out, "beta")) == exp_b, "beta did not recover")


@scenario("S8 PIN token: the stored PIN gives the right code; no PIN says so")
def _():
    rc, _, err = run("add", "pinned", "--token", "-", stdin=pin_tok + "\n", extra_env={"STOKEN_TUI_PIN": PIN})
    check(rc == 0, "add pinned: " + err)
    accounts_to_clean.add("pinned")
    check(keychain_get("pinned") == ctf_pin, "seed not stored as the 81-digit string")
    check(keychain_get("pinned", pin=True) == PIN, "PIN not stored")
    rc, out, err = run("code", "pinned", "--use-time", str(T))
    check(rc == 0 and out.strip() == exp_pin, "code %r != %r (%s)" % (out.strip(), exp_pin, err))
    rc, _, err = run("add", "nopin", "--token", "-", stdin=pin_tok + "\n")
    check(rc == 0, "add nopin: " + err)
    accounts_to_clean.add("nopin")
    rc, out, _ = frame()
    check("PIN required" in (row_for(out, "nopin") or ""), "row: %r" % row_for(out, "nopin"))
    check(digits_of(row_for(out, "alpha")) == exp_a, "alpha broke")


@scenario("S9 adopt an existing encrypted rc: password verified, seed moved into the Keychain, source untouched")
def _():
    src = os.path.join(work, "existing.rc")
    p = subprocess.run([REAL_STOKEN, "import", "--rcfile=" + src, "--file=" + fx_b],
                       input="hunter2 pass\nhunter2 pass\n", text=True, capture_output=True)
    check(os.path.isfile(src), "fixture import failed: " + p.stderr)
    before = open(src, "rb").read()
    secrets_seen.append("hunter2 pass")
    rc, _, _ = run("adopt", "gamma", src, extra_env={"STOKEN_TUI_PASSWORD": "wrong guess"})
    check(rc != 0, "adopt accepted a wrong password")
    check(keychain_get("gamma") is None and "gamma" not in index_names(), "wrong-password adopt left state")
    rc, _, err = run("adopt", "gamma", src, extra_env={"STOKEN_TUI_PASSWORD": "hunter2 pass"})
    check(rc == 0, "adopt: " + err)
    accounts_to_clean.add("gamma")
    check(keychain_get("gamma") == ctf_b, "seed not stored as the plain 81-digit string")
    check(open(src, "rb").read() == before, "adopt modified the source rc")
    check(sorted(os.listdir(tokdir)) == [".lock", "index"], "adopt left files: %r" % os.listdir(tokdir))
    rc, out, _ = run("code", "gamma", "--use-time", str(T))
    check(out.strip() == exp_b, "adopted code %r != %r" % (out.strip(), exp_b))


@scenario("S10 rm keeps the seed under a .trashed account; --purge deletes it; second rm fails")
def _():
    rc, out, err = run("rm", "alpha", "--yes")
    check(rc == 0, "rm: " + err)
    check("alpha" not in index_names() and keychain_get("alpha") is None, "alias still live")
    m = re.search(r"account (alpha\.trashed-\d+)", out + err)
    check(m, "rm did not name the kept account: %r" % (out + err))
    accounts_to_clean.add(m.group(1))
    check(keychain_get(m.group(1)) == ctf_a, "trashed account does not hold the seed")
    rc, _, _ = run("rm", "alpha", "--yes")
    check(rc != 0, "second rm succeeded")
    rc, _, err = run("add", "purgeme", "--file", fx_a)
    check(rc == 0, "add purgeme: " + err)
    accounts_to_clean.add("purgeme")
    rc, out, err = run("rm", "purgeme", "--yes", "--purge")
    check(rc == 0 and "trashed" not in out + err, "purge: %d %r" % (rc, out + err))
    check(keychain_get("purgeme") is None and "purgeme" not in index_names(), "purge left state")
    rc, _, err = run("add", "alpha", "--file", fx_a)
    check(rc == 0, "re-add alpha: " + err)


@scenario("S11 stoken missing from PATH: clear error, exit 2")
def _():
    rc, _, err = run("--use-time", str(T), "--once", path="/usr/bin:/bin")
    check(rc == 2 and "stoken not found" in err, "exit %d, err %r" % (rc, err))


@scenario("S12 no tokens: frame explains how to add one, exit 0")
def _():
    rc, out, _ = run("--once", extra_env={"STOKEN_TUI_DIR": os.path.join(work, "empty")})
    check(rc == 0 and "add" in out, "exit %d out %r" % (rc, out))


@scenario("S13 add and code work with a controlling terminal (stoken must not prompt on the tty)")
def _():
    import pty, select

    def in_pty(*args):
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, "-I", TOOL, *args], dict(env))
        out, deadline = b"", time.time() + 40
        while time.time() < deadline:
            if select.select([fd], [], [], 0.5)[0]:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    _, status = os.waitpid(pid, 0)
                    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")
                out += chunk
        os.kill(pid, 9)
        raise AssertionError("hung waiting on a prompt: %r" % out[-200:])

    rc, out = in_pty("add", "ttyone", "--file", fx_a)
    accounts_to_clean.add("ttyone")
    check(rc == 0 and "password:" not in out.lower(), "add in pty: rc %d out %r" % (rc, out))
    rc, out = in_pty("code", "ttyone", "--use-time", str(T))
    check(rc == 0 and out.strip() == exp_a, "code in pty: rc %d out %r" % (rc, out))


@scenario("S14 Keychain write that exits 0 but stores nothing is caught; the index is rolled back")
def _():
    rc, _, err = run("add", "ghost", "--file", fx_a, extra_env={"SHIM_SECURITY_DROP_WRITES": "1"})
    check(rc != 0 and "keychain" in err.lower(), "add must fail naming the Keychain: rc %d err %r" % (rc, err))
    check("ghost" not in index_names(), "index entry left after a silent Keychain failure")
    check(keychain_get("ghost") is None, "ghost item exists")


@scenario("S15 Keychain unavailable is reported as such, not as a missing seed")
def _():
    rc, out, _ = run("--use-time", str(T), "--once", extra_env={"SHIM_SECURITY_FIND_EXIT": "36"})
    check(rc == 0, "frame exit %d" % rc)
    row = row_for(out, "beta") or ""
    check("Keychain unavailable" in row and "no seed" not in row, "beta row: %r" % row)


@scenario("S16 export prints the seed and round-trips through add; unknown alias fails")
def _():
    rc, out, _ = run("export", "beta")
    check(rc == 0 and out.strip() == ctf_b, "export output %r" % out)
    rc, _, err = run("add", "roundtrip", "--token", "-", stdin=out)
    check(rc == 0, "re-add from export: " + err)
    accounts_to_clean.add("roundtrip")
    rc, out2, _ = run("code", "roundtrip", "--use-time", str(T))
    check(out2.strip() == exp_b, "round-trip code %r" % out2)
    rc, _, _ = run("export", "nosuch")
    check(rc != 0, "export of an unknown alias succeeded")


@scenario("S17 live view: renders every alias, j then Enter copies the selected code, q quits, clipboard cleared")
def _():
    import pty, select
    live_dir = os.path.join(work, "live")
    live_env = {"STOKEN_TUI_DIR": live_dir}
    for alias, fx in (("tuia", fx_a), ("tuib", fx_b)):
        rc, _, err = run("add", alias, "--file", fx, extra_env=live_env)
        check(rc == 0, "add %s: %s" % (alias, err))
        accounts_to_clean.add(alias)
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(sys.executable, [sys.executable, "-I", TOOL],
                  dict(env, **live_env, TERM="xterm-256color", LINES="24", COLUMNS="80"))
    seen = b""

    def pump(seconds):
        nonlocal seen
        end = time.time() + seconds
        while time.time() < end:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    seen += os.read(fd, 4096)
                except OSError:
                    return

    pump(3.0)
    for key in (b"j", b"\r"):
        os.write(fd, key)
        pump(0.8)
    now = time.time()
    clip = open(env["SHIM_CLIP"]).read() if os.path.exists(env["SHIM_CLIP"]) else ""
    os.write(fd, b"q")
    pump(1.0)
    _, status = os.waitpid(pid, 0)
    text = seen.decode(errors="replace")
    check(os.waitstatus_to_exitcode(status) == 0, "exit status %r" % status)
    check("tuia" in text and "tuib" in text, "aliases not drawn: %r" % text[-300:])
    check(clip in (at(fx_b, int(now)), at(fx_b, int(now) - 8)), "clipboard %r is not tuib's code" % clip)
    after = open(env["SHIM_CLIP"]).read()
    check(after == "", "clipboard still holds a code after quit: %r" % after)


@scenario("S18 add killed mid-way: no seed on disk anywhere, nothing in the Keychain or the index")
def _():
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        tmp = os.path.join(work, "tmp-%d" % sig)
        kdir = os.path.join(work, "kill-%d" % sig)
        os.makedirs(tmp)
        mark = len(open(shimlog).read())
        e = dict(env, STOKEN_TUI_DIR=kdir, TMPDIR=tmp, SHIM_STOKEN_SLEEP="1")
        p = subprocess.Popen([sys.executable, "-I", TOOL, "add", "killed", "--token", "-"], env=e,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p.stdin.write(pin_tok + "\n")
        p.stdin.close()
        deadline = time.time() + 20
        while "[export]" not in open(shimlog).read()[mark:] and time.time() < deadline:
            time.sleep(0.1)
        check("[export]" in open(shimlog).read()[mark:], "add never reached stoken")
        p.send_signal(sig)
        p.wait(timeout=15)
        for root_dir in (kdir, tmp):
            for root, _, files in os.walk(root_dir):
                for name in files:
                    data = open(os.path.join(root, name), "rb").read()
                    check(pin_data.encode() not in data and ctf_pin.encode() not in data,
                          "seed found in %s after signal %d" % (name, sig))
        check(not os.listdir(tmp), "temp files left after signal %d: %r" % (sig, os.listdir(tmp)))
        check(keychain_get("killed") is None, "Keychain item left after signal %d" % sig)
        check("killed" not in (open(os.path.join(kdir, "index")).read().split() if os.path.exists(os.path.join(kdir, "index")) else []),
              "index entry left after signal %d" % sig)


@scenario("S19 an orphan Keychain item blocks add and is never overwritten")
def _():
    keychain_set("orph", "precious")
    rc, _, _ = run("add", "orph", "--file", fx_a)
    check(rc != 0, "add overwrote or ignored an orphan item")
    check(keychain_get("orph") == "precious", "orphan item changed")
    check("orph" not in index_names(), "index entry created despite the orphan")


@scenario("S20 a Keychain write that times out rolls the add back with a clean message")
def _():
    rc, _, err = run("add", "slow", "--file", fx_a, extra_env={"SHIM_SECURITY_SLEEP": "1", "STOKEN_TUI_KEYCHAIN_TIMEOUT": "1"})
    check(rc != 0, "add succeeded through a timeout")
    check("Keychain unavailable" in err and "Traceback" not in err, "timeout surfaced as a crash: %r" % err[-200:])
    check("slow" not in index_names(), "index entry left behind after a Keychain timeout")
    check(keychain_get("slow") is None, "Keychain item left behind")


@scenario("S21 concurrent adds: different aliases both land; the same alias lands exactly once")
def _():
    def start(alias):
        return subprocess.Popen([sys.executable, "-I", TOOL, "add", alias, "--file", fx_a], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    procs = [start("race1"), start("race2")]
    codes = [p.wait(timeout=60) for p in procs]
    accounts_to_clean.update(("race1", "race2"))
    check(codes == [0, 0], "different aliases collided: %r" % codes)
    check({"race1", "race2"} <= set(index_names()), "index lost an entry: %r" % index_names())
    same = [start("racesame") for _ in range(3)]
    codes = sorted(p.wait(timeout=60) for p in same)
    accounts_to_clean.add("racesame")
    check(codes.count(0) == 1 and index_names().count("racesame") == 1, "same-alias race: %r" % codes)


@scenario("S22 failing Touch ID gate stops frame, code, export and the live view before any secret is read")
def _():
    import pty, select
    gate = {"STOKEN_TUI_AUTH": "/usr/bin/false"}
    mark = len(open(shimlog).read())
    for args in (("--once",), ("code", "beta"), ("export", "beta")):
        rc, out, err = run(*args, extra_env=gate)
        check(rc == 1 and "Touch ID" in err, "%r: exit %d err %r" % (args, rc, err))
        check(not re.search(r"\d{6}", out) and ctf_b not in out + err, "%r leaked a secret" % (args,))
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(sys.executable, [sys.executable, "-I", TOOL],
                  dict(env, **gate, STOKEN_TUI_DIR=os.path.join(work, "live"), TERM="xterm-256color",
                       LINES="24", COLUMNS="80"))
    seen, deadline = b"", time.time() + 15
    while time.time() < deadline:
        if select.select([fd], [], [], 0.2)[0]:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            seen += chunk
    _, status = os.waitpid(pid, 0)
    check(os.waitstatus_to_exitcode(status) == 1, "live view exit %r" % status)
    check(b"tuia" not in seen, "live view drew aliases despite a failed gate")
    calls = open(shimlog).read()[mark:]
    check("stoken [" not in calls and "find-generic-password" not in calls,
          "stoken or security was called before the gate passed: %r" % calls[:200])


@scenario("S23 the gate covers only commands that reveal a secret: add, adopt and rm still run")
def _():
    gate = {"STOKEN_TUI_AUTH": "/usr/bin/false"}
    rc, _, err = run("add", "gated", "--file", fx_a, extra_env=gate)
    check(rc == 0, "add was blocked by the gate: " + err)
    accounts_to_clean.add("gated")
    rc, _, err = run("rm", "gated", "--yes", "--purge", extra_env=gate)
    check(rc == 0, "rm was blocked by the gate: " + err)


@scenario("S24 STOKEN_TUI_AUTH=off skips the gate; the real helper compiles and answers --can-evaluate")
def _():
    rc, out, err = run("code", "beta", "--use-time", str(T), extra_env={"STOKEN_TUI_AUTH": "off"})
    check(rc == 0 and out.strip() == exp_b, "off: exit %d %r %r" % (rc, out, err))
    helper = os.path.join(work, "touchid")
    built = subprocess.run(["swiftc", "-O", "-o", helper, os.path.join(ROOT, "touchid.swift")], capture_output=True,
                           text=True, timeout=300)
    check(built.returncode == 0, "swiftc failed: " + built.stderr[-300:])
    probe = subprocess.run([helper, "--can-evaluate"], capture_output=True, text=True, timeout=30)
    check(probe.returncode in (0, 2), "helper exit %d: %s" % (probe.returncode, probe.stderr))


@scenario("S25 default gate: a missing compiler fails closed with no leftovers; the helper builds once and is reused")
def _():
    import importlib.machinery, importlib.util
    cache = os.path.join(work, "cache")
    rc, out, err = run("export", "beta", extra_env={"STOKEN_TUI_AUTH": "", "STOKEN_TUI_CACHE": cache,
                                                    "PATH": shimdir + os.pathsep + "/nonexistent"})
    check(rc == 1 and "swiftc" in err, "no compiler: exit %d err %r" % (rc, err))
    check(not os.path.exists(cache), "a failed gate created the cache dir")
    loader = importlib.machinery.SourceFileLoader("stoken_tui_under_test", TOOL)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    os.environ["STOKEN_TUI_CACHE"] = cache
    first = mod.helper_path()
    stamp = os.stat(first).st_mtime_ns
    check(os.access(first, os.X_OK) and stat.S_IMODE(os.stat(cache).st_mode) == 0o700, "helper missing or cache not 0700")
    check(mod.helper_path() == first and os.stat(first).st_mtime_ns == stamp, "helper was rebuilt instead of reused")
    check(not [n for n in os.listdir(cache) if ".build-" in n], "partial build files left")
    del os.environ["STOKEN_TUI_CACHE"]


@scenario("S26 no seed, PIN or token data ever appears in any logged argv; no file under the token dir holds a seed")
def _():
    calls = open(shimlog).read()
    check("[export]" in calls and "security" in calls, "shim log is empty, so the check proves nothing")
    for s in secrets_seen:
        check(s not in calls, "secret %r... found in an argv" % s[:4])
    for root, _, files in os.walk(tokdir):
        for name in files:
            data = open(os.path.join(root, name)).read()
            check(not any(s in data for s in (ctf_a, ctf_b, ctf_pin)), "seed found in %s" % name)


def live_session(lines, cols, keys=(), drop_locale=False, seconds=3.0, aliases=(("tuia", None), ("tuib", None)), extra=None):
    """Run the live view in a pty and return (exit status, raw output bytes)."""
    import pty, select
    live_dir = tempfile.mkdtemp(prefix="live-", dir=work)
    for alias, _ in aliases:
        keychain_del(alias)
        rc, _, err = run("add", alias, "--file", fx_a, extra_env={"STOKEN_TUI_DIR": live_dir})
        check(rc == 0, "add %s for the live session: %s" % (alias, err))
        accounts_to_clean.add(alias)
    child_env = dict(env, STOKEN_TUI_DIR=live_dir, TERM="xterm-256color", LINES=str(lines), COLUMNS=str(cols))
    if drop_locale:
        for k in ("LANG", "LC_ALL", "LC_CTYPE"):
            child_env.pop(k, None)
    child_env.update(extra or {})
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(sys.executable, [sys.executable, "-I", TOOL], child_env)
    import fcntl, struct, termios
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, cols, 0, 0))
    seen = b""

    def pump(duration):
        nonlocal seen
        end = time.time() + duration
        while time.time() < end:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    seen += os.read(fd, 4096)
                except OSError:
                    return

    pump(seconds)
    for key in keys:
        os.write(fd, key)
        pump(0.5)
    os.write(fd, b"q")
    pump(1.0)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), seen


@scenario("S27 live view draws clean box-drawing glyphs with or without a locale; STOKEN_TUI_ASCII=1 gives pure ASCII")
def _():
    for drop in (False, True):
        rc, seen = live_session(24, 80, drop_locale=drop)
        check(rc == 0, "session (no locale: %s) exit %d" % (drop, rc))
        check(b"\xe2\x94\x81" in seen and b"\xe2\x96\x8c" in seen, "bar or marker glyph missing (no locale: %s)" % drop)
        check(seen.count(b"\x01") == 0 and b"\xef\xbf\xbd" not in seen, "garbage bytes (no locale: %s)" % drop)
    rc, seen = live_session(24, 80, extra={"STOKEN_TUI_ASCII": "1"})
    check(rc == 0, "ascii session exit %d" % rc)
    check(b"\xe2" not in seen and b"#" in seen and b"-" in seen, "ASCII mode sent non-ASCII bytes or no bar: %r" % seen[:300])


def tmux_screen(cols, lines, keys=(), aliases=("tuia", "tuib"), short_dir=False):
    """Render the live view in a private tmux server and return (screen text, token directory)."""
    import shlex
    live_dir = tempfile.mkdtemp(prefix="tmux-", dir="/private/tmp" if short_dir else work)
    extra_dirs.append(live_dir)
    for alias in aliases:
        keychain_del(alias)
        rc, _, err = run("add", alias, "--file", fx_a, extra_env={"STOKEN_TUI_DIR": live_dir})
        check(rc == 0, "add %s for the tmux session: %s" % (alias, err))
        accounts_to_clean.add(alias)
    sock, name = "stoken-tui-e2e-%d" % os.getpid(), "view"
    tm = ["tmux", "-L", sock]
    inner = shlex.join(["env", "STOKEN_TUI_DIR=" + live_dir, "STOKEN_TUI_SERVICE=" + service, "STOKEN_TUI_AUTH=/usr/bin/true",
                        "STOKEN_TUI_PIN=", "STOKEN_TUI_SECURITY=" + env["STOKEN_TUI_SECURITY"], "PATH=" + env["PATH"],
                        "SHIM_LOG=" + shimlog, "LANG=en_US.UTF-8", sys.executable, "-I", TOOL])
    try:
        subprocess.run(tm + ["new-session", "-d", "-s", name, "-x", str(cols), "-y", str(lines), inner], check=True)
        time.sleep(3)
        for key in keys:
            subprocess.run(tm + ["send-keys", "-t", name, key], check=True)
            time.sleep(0.5)
        shown = subprocess.run(tm + ["capture-pane", "-p", "-t", name], capture_output=True, text=True, check=True).stdout
        subprocess.run(tm + ["send-keys", "-t", name, "q"])
        return shown, live_dir
    finally:
        subprocess.run(tm + ["kill-server"], capture_output=True)


@scenario("S28 live view layout: title, token count, names, grouped code, countdown bar and footer on the real screen")
def _():
    shown, _ = tmux_screen(80, 24)
    for needle in ("stoken-tui", "2 tokens", "tuia", "tuib", "copy", "quit", "▌"):
        check(needle in shown, "%r missing from the screen:\n%s" % (needle, shown))
    lines = shown.splitlines()
    first = next((i for i, ln in enumerate(lines) if "tuia" in ln), None)
    check(first is not None and re.search(r"\d{3} \d{3}\s+[━╸─]+\s+\d{1,2}s", lines[first + 1]),
          "first card lacks code, bar and seconds: %r" % (lines[first + 1] if first is not None else None))
    check("▌" in lines[first] and "▌" in lines[first + 1], "selection marker missing on the first card")
    check("�" not in shown, "replacement characters on screen")
    check(lines[-2].strip().startswith("↑↓"), "key footer missing: %r" % lines[-2])
    check("names:" in lines[-1], "names footer missing: %r" % lines[-1])


@scenario("S29 a tiny terminal does not crash the live view, and selection scrolls through many tokens")
def _():
    many = tuple("tok%d" % i for i in range(6))
    shown, _ = tmux_screen(30, 7, keys=("j", "Down", "j", "Down", "j"), aliases=many)
    check("Traceback" not in shown and "stoken-tui" in shown, "tiny terminal crashed or drew nothing:\n" + shown)
    check("tok5" in shown and "tok0" not in shown, "selection did not scroll to the last token:\n" + shown)
    check("6 tokens" in shown, "token count missing:\n" + shown)


@scenario("S30 code --next prints the code one interval ahead, for PINless and PIN tokens alike")
def _():
    rc, out, err = run("code", "alpha", "--next", "--use-time", str(T))
    want = at(fx_a, T + 60)
    check(rc == 0 and out.strip() == want and want != exp_a, "next %r want %r (current %r) %s" % (out.strip(), want, exp_a, err))
    rc, out, err = run("code", "pinned", "--next", "--use-time", str(T))
    want_pin = sh(REAL_STOKEN, "tokencode", "--batch", "--pin=" + PIN, "--use-time=%d" % (T + 60), "--token=" + pin_tok)[1]
    check(rc == 0 and out.strip() == want_pin, "pin next %r want %r (%s)" % (out.strip(), want_pin, err))
    rc, out, _ = run("code", "alpha", "--use-time", str(T))
    check(out.strip() == exp_a, "plain code changed: %r" % out)


@scenario("S31 live view shows the next code under every card, dimmed, matching stoken one interval ahead")
def _():
    before = int(time.time())
    shown, _ = tmux_screen(80, 24)
    after = int(time.time())
    lines = shown.splitlines()
    nexts = [ln for ln in lines if re.search(r"next\s+\d{3} \d{3}", ln)]
    check(len(nexts) == 2, "expected a next line on both cards:\n" + shown)
    wanted = {at(fx_a, t + 60) for t in range(before, after + 1)} | {at(fx_a, t + 60) for t in (before - 1, after + 1)}
    for ln in nexts:
        digits = re.sub(r"\D", "", re.search(r"next\s+(\d{3} \d{3})", ln).group(1))
        check(digits in wanted, "next code %s is not one interval ahead (%s)" % (digits, sorted(wanted)))


@scenario("S32 pressing n in the live view copies the next code")
def _():
    t0 = int(time.time())
    rc, _ = live_session(24, 80, keys=(b"n",), seconds=3.0)
    t1 = int(time.time())
    check(rc == 0, "exit %d" % rc)
    wanted = {at(fx_a, t + 60) for t in range(t0 - 1, t1 + 1)}
    log_path = os.path.join(work, "clip.log")
    copies = [ln for ln in open(log_path).read().split("\n") if ln] if os.path.exists(log_path) else []
    check(any(c in wanted for c in copies), "no next code was copied: %r vs %r" % (copies, sorted(wanted)))


@scenario("S33 rename moves the seed and PIN to the new name and fixes the index; bad renames change nothing")
def _():
    rc, _, err = run("add", "oldname", "--token", "-", stdin=pin_tok + "\n", extra_env={"STOKEN_TUI_PIN": PIN})
    check(rc == 0, "add oldname: " + err)
    accounts_to_clean.update(("oldname", "newname"))
    before = index_names()
    for old, new, label in (("oldname", "alpha", "target exists"), ("nosuch", "fresh", "unknown source"),
                            ("oldname", "../evil", "hostile target"), ("oldname", "oldname", "same name")):
        rc, _, _ = run("rename", old, new)
        check(rc != 0, "%s: rename succeeded" % label)
        check(index_names() == before and keychain_get("oldname") == ctf_pin, "%s: state changed" % label)
    rc, _, err = run("rename", "oldname", "newname", extra_env={"SHIM_SECURITY_DROP_WRITES": "1"})
    check(rc != 0 and index_names() == before and keychain_get("oldname") == ctf_pin, "failed write was not rolled back: " + err)
    rc, out, err = run("rename", "oldname", "newname")
    check(rc == 0, "rename: " + err)
    check("newname" in index_names() and "oldname" not in index_names(), "index not updated: %r" % index_names())
    check(keychain_get("oldname") is None and keychain_get("oldname", pin=True) is None, "old Keychain items remain")
    check(keychain_get("newname") == ctf_pin and keychain_get("newname", pin=True) == PIN, "seed or PIN not moved")
    rc, out, _ = run("code", "newname", "--use-time", str(T))
    check(out.strip() == exp_pin, "renamed token gives %r, want %r" % (out.strip(), exp_pin))


@scenario("S34 the live view footer shows where the alias list lives, and how to rename")
def _():
    shown, live_dir = tmux_screen(110, 24, short_dir=True)
    footer = shown.splitlines()[-1]
    check(os.path.join(live_dir, "index") in footer, "footer lacks the index path: %r" % footer)
    check("rename: stoken-tui rename" in footer, "footer lacks the rename hint: %r" % footer)
    check(shown.splitlines()[-2].strip().startswith("↑↓"), "key hints lost")


for acct in list(accounts_to_clean):
    keychain_del(acct)
    keychain_del(acct, pin=True)
shutil.rmtree(work, ignore_errors=True)
for d in extra_dirs:
    shutil.rmtree(d, ignore_errors=True)

summary = "%d passed, %d failed of %d" % (len(passed), len(failed), len(passed) + len(failed))
with open(RESULTS, "w") as f:
    f.write("\n".join(log) + "\n\n" + summary + "\n")
print("\n".join(log) + "\n\n" + summary)
sys.exit(1 if failed else 0)
