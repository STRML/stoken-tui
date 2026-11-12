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


def pinless_fixture(name, seed_byte, serial=None):
    seed = base64.b64encode(bytes([seed_byte]) * 16).decode()
    tmpl = os.path.join(work, name + ".xml")
    with open(tmpl, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<TKNBatch>\n<TKNHeader><Origin>E2E</Origin>'
                '<DefPinType>0</DefPinType><DefLocalPIN>0</DefLocalPIN><DefAddPIN>0</DefAddPIN>'
                '<DefDigits>6</DefDigits><DefInterval>60</DefInterval></TKNHeader>\n'
                '<TKN><SN>%s</SN><UserLogin>e2e</UserLogin><Death>2035/12/31</Death>'
                '<Seed>=%s</Seed></TKN>\n</TKNBatch>\n' % (serial or "00000000%04d" % seed_byte, seed))
    path = os.path.join(work, name + ".sdtid")
    with open(path, "w") as out:
        subprocess.run([REAL_STOKEN, "export", "--random", "--sdtid", "--template", tmpl], stdout=out, check=True,
                       stdin=subprocess.DEVNULL)
    return path


def at(sdtid, t):
    return sh(REAL_STOKEN, "tokencode", "--batch", "--use-time=%d" % t, "--file=" + sdtid)[1]


def index_names():
    path = os.path.join(tokdir, "index")
    return [n for n in open(path).read().split("\n") if n] if os.path.exists(path) else []


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
    for bad in ("../evil", " lead", "trail ", "tab\tname", 'quo"te', "back\\slash", ".hid", "x/y", "", "a" * 65, "a\n"):
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


# CT-KIP provisioning and the add flow ---------------------------------------------------------------
# A stand-in for the rsa_ct_kip package: same entry point (client.main), same prints and the same template
# (with the DefAddPin typo). It never touches a network. The fake interpreter logs its argv and puts the
# fake package on PYTHONPATH, because the tool starts the helper as `python -I -c STUB`.

KIP_CODE = "492817305516"
KIP_URL = "https://kip.example.test/ctkip/services/CtkipService"
KIP_SEED = bytes([0x33]) * 16
KIP_TEMPLATE = ('<?xml version="1.0" encoding="UTF-8"?>\n<TKNBatch>\n<TKNHeader><Origin>kip.example.test</Origin>'
                '<DefPinType>0</DefPinType><DefAddPin>%d</DefAddPin><DefDigits>6</DefDigits><DefInterval>60</DefInterval>'
                '</TKNHeader>\n<TKN><SN>000000009901</SN><UserLogin>e2e</UserLogin><Death>2035/12/31</Death>'
                '<Seed>=%s</Seed></TKN>\n</TKNBatch>\n')
FAKE_CLIENT = r'''
import base64, os, shutil, sys, time
import requests
stoken = shutil.which("stoken")
SEED = bytes([0x33]) * 16
''' + "TEMPLATE = %r\n" % KIP_TEMPLATE + r'''


def log(line):
    with open(os.environ["FAKE_KIP_LOG"], "a") as f:
        f.write(line + "\n")


def main(args=None):
    url, code, filename = args
    mode = os.environ.get("FAKE_KIP_MODE", "ok")
    out = open(filename, "w")
    log("pid=%d" % os.getpid())
    log("url=" + url)
    log("code=" + code)
    if stoken:
        raise AssertionError("the stub left stoken enabled")
    log("connection=%s" % requests.session().headers.get("Connection"))
    print("Sending ClientHello request to server...")
    if mode == "fault1":
        raise RuntimeError("soap fault, activation code %s refused" % code)
    print("Received ServerHello response with server nonce (R_S = 00) and 2048-bit RSA public key")
    print("Sending ServerFinished request to server, with encrypted client nonce...")
    if mode == "fault2":
        raise RuntimeError(("Server.Error", "token request failed for code " + code))
    if mode == "hang":
        time.sleep(120)
    print("ERROR: MAC not verified! Expected 00 but server sent 11." if mode == "mac_bad" else "MAC verified (00)")
    print("Received ServerFinished response with token information:")
    print("  Token seed: " + SEED.hex())
    sys.stdout.flush()
    if mode == "crash_after_seed":
        raise TypeError("'NoneType' object is not subscriptable")
    if mode == "partial_template":
        out.write("<TKNBatch><TKN>")
        out.flush()
        raise TypeError("crashed mid-write")
    out.write(TEMPLATE % (1 if mode == "pin" else 0, base64.b64encode(SEED).decode()))
    out.flush()
    if mode == "sleep_after_template":
        log("template_written")
        time.sleep(120)
    out.close()
    print("Saved template to " + filename)
'''
FAKE_REQUESTS = ('class _Session:\n    def __init__(self):\n        self.headers = {}\n        self.verify = True\n\n\n'
                 'def session():\n    return _Session()\n')

kipdir = os.path.join(work, "fakekip")
kipcache = os.path.join(work, "kipcache")
kiplog = os.path.join(work, "kip.log")
os.makedirs(os.path.join(kipdir, "pkg", "rsa_ct_kip"))
for rel, body in (("pkg/rsa_ct_kip/__init__.py", ""), ("pkg/rsa_ct_kip/client.py", FAKE_CLIENT), ("pkg/requests.py", FAKE_REQUESTS)):
    open(os.path.join(kipdir, rel), "w").write(body)
open(os.path.join(kipdir, "fakepy"), "w").write(
    '#!/bin/bash\n{ printf "fakepy"; for a in "$@"; do printf " [%%s]" "$a"; done; echo; } >> "$SHIM_LOG"\n'
    'args=(); for a in "$@"; do [ "$a" = "-I" ] || args+=("$a"); done\n'
    'PYTHONPATH=%s exec %s "${args[@]}"\n' % (os.path.join(kipdir, "pkg"), sys.executable))
open(os.path.join(kipdir, "rsa_ct_kip"), "w").write("#!%s\n" % os.path.join(kipdir, "fakepy"))
for name in ("fakepy", "rsa_ct_kip"):
    os.chmod(os.path.join(kipdir, name), 0o755)

fx_kip = pinless_fixture("kip", 0x33, serial="000000009901")
exp_kip = at(fx_kip, T)
ctf_kip = sh(REAL_STOKEN, "export", "--file=" + fx_kip)[1]
secrets_seen.extend([KIP_CODE, KIP_SEED.hex(), ctf_kip, base64.b64encode(KIP_SEED).decode()])
kip_outputs = []


def kip_link(code=KIP_CODE, url=KIP_URL):
    return "com.rsa.securid://ctkip?url=%s&activationCode=%s" % (url, code)


def kip_env(mode="ok", **more):
    return dict({"STOKEN_TUI_CTKIP": os.path.join(kipdir, "rsa_ct_kip"), "STOKEN_TUI_CACHE": kipcache,
                 "FAKE_KIP_MODE": mode, "FAKE_KIP_LOG": kiplog}, **more)


def kip_add(alias, link=None, mode="ok", **more):
    accounts_to_clean.add(alias)
    if os.path.exists(kiplog):
        os.unlink(kiplog)
    rc, out, err = run("add", alias, "--token", "-", stdin=(link or kip_link()) + "\n", extra_env=kip_env(mode, **more))
    kip_outputs.append(out + err)
    return rc, out, err


def kip_log():
    return open(kiplog).read() if os.path.exists(kiplog) else ""


kip_detail = []


def kip_stored_ok(alias, token_dir=None):
    rc, out, err = run("code", alias, "--use-time", str(T), extra_env={"STOKEN_TUI_DIR": token_dir} if token_dir else None)
    kip_detail[:] = [rc, out.strip(), err.strip(), exp_kip]
    return rc == 0 and out.strip() == exp_kip


def kip_dirs():
    return sorted(d for d in os.listdir(kipcache) if d.startswith("ctkip-")) if os.path.isdir(kipcache) else []


def kept_path(text):
    m = re.search(r"(/\S*ctkip-\w+)", text)
    check(m is not None, "no kept directory named in: %r" % text)
    return m.group(1)


def stored(alias):
    return alias in index_names() or keychain_get(alias) is not None


@scenario("S35 add from a CT-KIP link: provisioned, stored like any token, code right, private directory gone")
def _():
    rc, out, err = kip_add("kip1")
    check(rc == 0, "add exit %d: %s" % (rc, err))
    check(kip_stored_ok("kip1"), "the stored seed does not give the issued token's code: %r" % kip_detail)
    check("kip1" in index_names(), "no index entry")
    rc, out, err = run("code", "kip1", "--use-time", str(T))
    check(rc == 0 and out.strip() == exp_kip, "code %r != %r" % (out.strip(), exp_kip))
    check(not kip_dirs(), "private directory left behind: %r" % kip_dirs())
    log_text = kip_log()
    check("connection=close" in log_text, "requests were not forced to Connection: close: %r" % log_text)
    check("code=" + KIP_CODE in log_text and "url=" + KIP_URL in log_text, "the helper never got the code on stdin: %r" % log_text)


@scenario("S36 a token the server marked PINless needs no PIN; one marked PIN-bearing still asks for it")
def _():
    rc, _, err = run("code", "kip1", "--use-time", str(T))
    check(rc == 0, "the PINless token failed: " + err)
    rc, _, err = kip_add("kippin", mode="pin")
    check(rc == 0, "add of the PIN-bearing token failed: " + err)
    rc, out, err = run("code", "kippin", "--use-time", str(T))
    check(rc != 0 and "PIN required" in out + err, "PIN token did not ask for a PIN: %r %r" % (out, err))


@scenario("S37 hostile or malformed links never reach the helper; a CTF link still goes to stoken")
def _():
    host = "kip.example.test"
    bad = {
        "http url": kip_link(url="http://%s/x" % host),
        "user info": kip_link(url="https://user:pw@%s/x" % host),
        "empty host": kip_link(url="https://"),
        "space in url": kip_link(url="https://%s/a b" % host),
        "dash code": kip_link(code="-492817305516"),
        "space in code": kip_link(code="4928 17305516"),
        "short code": kip_link(code="12"),
        "no code": "com.rsa.securid://ctkip?url=https://%s/x" % host,
        "no url": "com.rsa.securid://ctkip?activationCode=" + KIP_CODE,
        "newline": kip_link() + "\nextra",
    }
    for label, link in bad.items():
        rc, out, err = kip_add("hostile", link=link)
        check(rc == 1, "%s: exit %d, want 1 (%s)" % (label, rc, err))
        check(not kip_log() and not stored("hostile"), "%s: the helper ran or something was stored" % label)
    rc, _, err = kip_add("encoded", link=kip_link(url=KIP_URL.replace(":", "%3A").replace("/", "%2F")))
    check(rc == 0 and "url=" + KIP_URL in kip_log(), "a percent-encoded url was not decoded: %s %r" % (err, kip_log()))
    os.unlink(kiplog) if os.path.exists(kiplog) else None
    rc, _, err = run("add", "ctfok", "--token", "-", stdin=ctf_a + "\n", extra_env=kip_env())
    accounts_to_clean.add("ctfok")
    check(rc == 0 and keychain_get("ctfok") == ctf_a and not kip_log(), "a plain seed was not handed to stoken: " + err)


@scenario("S38 a taken alias is refused before the code is spent, for the index and for an orphan Keychain item")
def _():
    rc, _, err = kip_add("alpha")
    check(rc == 1 and not kip_log(), "alpha was provisioned over an existing alias: %s" % err)
    keychain_set("orphkip", "orphan-seed")
    rc, _, err = kip_add("orphkip")
    check(rc == 1 and not kip_log(), "an orphan item did not stop the helper: %s" % err)
    check(keychain_get("orphkip") == "orphan-seed", "the orphan item was touched")
    rc, _, err = kip_add("fresh38")
    check(rc == 0 and "code=" + KIP_CODE in kip_log(), "a free alias did not reach the helper, so the refusals above prove nothing: %s" % err)


@scenario("S39 no usable rsa_ct_kip: exit 2 with a hint, nothing stored; PATH lookup works when the variable is unset")
def _():
    sh_script = os.path.join(work, "sh-trampoline")
    env_script = os.path.join(work, "env-python")
    open(sh_script, "w").write("#!/bin/sh\nexit 0\n")
    open(env_script, "w").write("#!/usr/bin/env python3\n")
    for p in (sh_script, env_script):
        os.chmod(p, 0o755)
    for label, helper in (("missing", os.path.join(work, "nope")), ("sh", sh_script), ("env", env_script), ("not set", "")):
        rc, out, err = kip_add("nohelper", STOKEN_TUI_CTKIP=helper)
        check(rc == 2 and "STOKEN_TUI_CTKIP" in err, "%s: exit %d, hint missing: %r" % (label, rc, err))
        check(not kip_log() and not stored("nohelper"), "%s: helper ran or something was stored" % label)
    rc, _, err = kip_add("viapath", STOKEN_TUI_CTKIP="", PATH=kipdir + os.pathsep + env["PATH"])
    check(rc == 0 and kip_stored_ok("viapath"), "lookup through PATH failed: %s" % err)


@scenario("S40 a server fault before the commit: scrubbed message, phase named, nothing left")
def _():
    rc, out, err = kip_add("fault", mode="fault1")
    check(rc == 1 and not stored("fault"), "exit %d or something stored" % rc)
    check(KIP_CODE not in out + err and "***" in err, "the activation code was not scrubbed: %r" % err)
    check("unspent" in err, "pre-commit failure should say the code is probably unspent: %r" % err)
    check(not kip_dirs(), "files left after a failure before the commit: %r" % kip_dirs())
    rc, out, err = kip_add("fault", mode="fault2")
    check(rc == 1 and KIP_CODE not in out + err and "may already be spent" in err, "second-POST failure message: %r" % err)
    check(not kip_dirs() and not stored("fault"), "leftovers after a second-POST failure")


@scenario("S41 a MAC that does not verify is refused even though the client exits 0")
def _():
    rc, out, err = kip_add("macbad", mode="mac_bad")
    check(rc == 1 and "MAC" in err and not stored("macbad"), "exit %d, err %r" % (rc, err))
    check(not kip_dirs(), "files kept for a token that failed its MAC: %r" % kip_dirs())


@scenario("S42 a hung helper is killed with its children at the timeout")
def _():
    t0 = time.time()
    rc, out, err = kip_add("hang", mode="hang", STOKEN_TUI_CTKIP_TIMEOUT="2")
    check(rc == 1 and "timed out" in err and time.time() - t0 < 20, "exit %d after %.1fs: %r" % (rc, time.time() - t0, err))
    pid = int(re.search(r"pid=(\d+)", kip_log()).group(1))
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    check(not alive, "the helper process %d is still running" % pid)
    check(not kip_dirs() and not stored("hang"), "leftovers after a timeout")


@scenario("S43 a Keychain failure after the commit keeps recovery.sdtid, rolls back, and the file re-adds")
def _():
    rc, out, err = kip_add("recov", SHIM_SECURITY_DROP_WRITES="1")
    check(rc == 1 and not stored("recov"), "exit %d, stored %s" % (rc, stored("recov")))
    where = kept_path(err)
    check(os.path.isdir(where) and "recovery.sdtid" in os.listdir(where), "no recovery file in %s" % where)
    check(stat.S_IMODE(os.stat(where).st_mode) == 0o700, "kept directory is not 0700")
    recovery = os.path.join(where, "recovery.sdtid")
    check(stat.S_IMODE(os.stat(recovery).st_mode) == 0o600, "recovery file is not 0600")
    check(KIP_SEED.hex() not in out + err, "the seed was printed")
    rc, _, err = run("add", "recov", "--file", recovery)
    check(rc == 0 and kip_stored_ok("recov"), "the recovery file did not re-add: %s" % err)
    rc, out, _ = run("code", "recov", "--use-time", str(T))
    check(out.strip() == exp_kip, "recovered token gives %r" % out.strip())
    shutil.rmtree(where)


@scenario("S44 SIGTERM while the helper holds a written template: helper killed, recovery file kept and usable")
def _():
    import signal
    accounts_to_clean.add("sigkip")
    if os.path.exists(kiplog):
        os.unlink(kiplog)
    link_file = os.path.join(work, "sigkip-link.txt")
    open(link_file, "w").write(kip_link() + "\n")
    p = subprocess.Popen([sys.executable, "-I", TOOL, "add", "sigkip", "--token", "-"], env=dict(env, **kip_env("sleep_after_template")),
                         stdin=open(link_file), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 30
    while "template_written" not in kip_log() and time.time() < deadline:
        time.sleep(0.1)
    check("template_written" in kip_log(), "the helper never wrote its template: %r" % kip_log())
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=30)
    pid = int(re.search(r"pid=(\d+)", kip_log()).group(1))
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    check(not alive, "the helper process %d outlived the tool" % pid)
    where = kept_path(err)
    recovery = os.path.join(where, "recovery.sdtid")
    check(os.path.isfile(recovery), "no recovery file after SIGTERM: %r %r" % (os.listdir(where), err))
    check(not stored("sigkip"), "a half-added alias was left")
    rc, _, err = run("add", "sigkip", "--file", recovery)
    check(rc == 0 and kip_stored_ok("sigkip"), "the recovery file did not re-add: %s" % err)
    shutil.rmtree(where)


@scenario("S45 the activation code and the seed never reach any argv, any output stream or any file outside a kept directory")
def _():
    calls = open(shimlog).read()
    check("fakepy [" in calls, "the fake interpreter never logged, so the check proves nothing")
    for secret in (KIP_CODE, KIP_SEED.hex(), ctf_kip, base64.b64encode(KIP_SEED).decode()):
        check(secret not in calls, "%r... found in a logged argv" % secret[:4])
        check(not any(secret in text for text in kip_outputs), "%r... found in tool output" % secret[:4])
    for root, _, files in os.walk(tokdir):
        for name in files:
            check(KIP_CODE not in open(os.path.join(root, name), errors="replace").read(), "code found in %s" % name)
    check(not kip_dirs(), "kept directories remain after the suite: %r" % kip_dirs())


class Pane:
    """A program in a private tmux server, driven with keystrokes and read back from the screen."""
    counter = 0

    def __init__(self, args, token_dir, cols=100, lines=30, keep_pin_env=False):
        import shlex
        Pane.counter += 1
        self.sock, self.name = "stoken-tui-e2e-p%d-%d" % (os.getpid(), Pane.counter), "pane"
        self.tm = ["tmux", "-L", self.sock]
        settings = ["STOKEN_TUI_DIR=" + token_dir, "STOKEN_TUI_SERVICE=" + service, "STOKEN_TUI_AUTH=/usr/bin/true",
                    "STOKEN_TUI_SECURITY=" + env["STOKEN_TUI_SECURITY"], "PATH=" + env["PATH"], "SHIM_LOG=" + shimlog,
                    "SHIM_CLIP=" + env["SHIM_CLIP"], "LANG=en_US.UTF-8"]
        settings += ["%s=%s" % kv for kv in kip_env().items()]
        inner = shlex.join(["env", *settings, sys.executable, "-I", TOOL, *args]) + "; echo __EXIT__$?; sleep 120"
        subprocess.run(self.tm + ["new-session", "-d", "-s", self.name, "-x", str(cols), "-y", str(lines), "/bin/sh", "-c", inner], check=True)

    def screen(self):
        return subprocess.run(self.tm + ["capture-pane", "-p", "-t", self.name], capture_output=True, text=True).stdout

    def expect(self, text, timeout=25, count=1):
        end = time.time() + timeout
        while time.time() < end:
            shown = self.screen()
            if shown.count(text) >= count:
                return shown
            time.sleep(0.2)
        raise AssertionError("never saw %r on the screen:\n%s" % (text, self.screen()))

    def expect_code(self, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            shown = self.screen()
            if re.search(r"\d{3} \d{3}", shown):
                return shown
            time.sleep(0.2)
        raise AssertionError("no code ever appeared:\n" + self.screen())

    def send(self, text):
        if text:
            subprocess.run(self.tm + ["send-keys", "-t", self.name, "-l", text], check=True)
        subprocess.run(self.tm + ["send-keys", "-t", self.name, "Enter"], check=True)

    def key(self, name):
        subprocess.run(self.tm + ["send-keys", "-t", self.name, name], check=True)

    def close(self):
        subprocess.run(self.tm + ["kill-server"], capture_output=True)


def wizard(args, token_dir, script, **kw):
    """Play (expected prompt, answer) pairs against `stoken-tui add ...` and return the final screen."""
    pane = Pane(args, token_dir, **kw)
    try:
        for prompt, answer in script:
            pane.expect(prompt)
            if answer is not None:
                pane.send(answer)
        return pane.expect("__EXIT__")
    finally:
        pane.close()


def exit_code(shown):
    return int(re.search(r"__EXIT__(\d+)", shown).group(1))


@scenario("S46 the add flow on a terminal: name, path or link, PIN; a CT-KIP host is confirmed before the code is spent")
def _():
    wdir = os.path.join(work, "wiz")
    spaced = os.path.join(work, "my token.sdtid")
    shutil.copy(fx_a, spaced)
    accounts_to_clean.update(("wiz1", "wiz2", "wiz3", "wiz4"))
    shown = wizard(["add"], wdir, [("Name", "wiz1"), ("Token", "'%s'" % spaced), ("PIN", "")])
    check(exit_code(shown) == 0 and keychain_get("wiz1") == ctf_a, "a quoted path with a space did not work:\n" + shown)
    shown = wizard(["add"], wdir, [("Name", "wiz4"), ("Token", spaced.replace(" ", "\\ ")), ("PIN", "")])
    check(exit_code(shown) == 0 and keychain_get("wiz4") == ctf_a, "a backslash-escaped path did not work:\n" + shown)
    if os.path.exists(kiplog):
        os.unlink(kiplog)
    shown = wizard(["add"], wdir, [("Name", "wiz2"), ("Token", kip_link()), ("PIN", ""), ("[y/N]", "y")])
    check(exit_code(shown) == 0 and kip_stored_ok("wiz2", wdir), "CT-KIP through the flow failed:\n" + shown)
    check("kip.example.test" in shown and "password manager" in shown, "host or the save-the-link reminder not shown:\n" + shown)
    check("code=" + KIP_CODE in kip_log(), "the helper never ran after y")
    os.unlink(kiplog)
    shown = wizard(["add"], wdir, [("Name", "wiz3"), ("Token", kip_link()), ("PIN", ""), ("[y/N]", "n")])
    check(exit_code(shown) == 1 and not kip_log() and not stored("wiz3"), "declining still spent the code or stored a token:\n" + shown)


@scenario("S47 the add flow off a terminal, or half-filled: exit 2 with usage and nothing stored; a given name skips its prompt")
def _():
    rc, out, err = run("add", stdin="")
    check(rc == 2 and "alias" in err.lower(), "bare add on a pipe: exit %d %r" % (rc, err))
    rc, out, err = run("add", "halfway", stdin="")
    check(rc == 2 and not stored("halfway"), "add NAME on a pipe: exit %d %r" % (rc, err))
    pane = Pane(["add", "wiz5"], os.path.join(work, "wiz"))
    try:
        shown = pane.expect("Token (paste")
        check("Name for this token" not in shown, "a given name was asked for again:\n" + shown)
    finally:
        pane.close()


@scenario("S48 bad input to the flow: bad and taken names are re-asked, an empty token too, Ctrl-C leaves nothing")
def _():
    accounts_to_clean.add("wiz6")
    pane = Pane(["add"], os.path.join(work, "wiz48"))
    try:
        pane.expect("Name for this token")
        pane.send("../evil")
        pane.expect("alias must match")
        pane.expect("Name for this token", count=2)
        pane.send("alpha")
        pane.expect("already exists")
        pane.expect("Name for this token", count=3)
        pane.send("wiz6")
        pane.expect("Token (paste")
        pane.send("")
        pane.expect("no token given")
        pane.expect("Token (paste", count=2)
        pane.key("C-c")
        shown = pane.expect("__EXIT__")
        check(exit_code(shown) == 130 and "cancelled" in shown, "Ctrl-C at the token prompt:\n" + shown)
    finally:
        pane.close()
    check(not stored("wiz6") and "wiz6" not in open(os.path.join(work, "wiz48", "index")).read() if os.path.exists(os.path.join(work, "wiz48", "index")) else not stored("wiz6"), "a half-written alias was left")


@scenario("S49 a in the live view runs the flow and returns to the view; Ctrl-C returns too; the footer lists a")
def _():
    vdir = os.path.join(work, "view49")
    for alias in ("tuia", "wiza"):
        keychain_del(alias)
        accounts_to_clean.add(alias)
    rc, _, err = run("add", "tuia", "--file", fx_a, extra_env={"STOKEN_TUI_DIR": vdir})
    check(rc == 0, "add tuia: " + err)
    pane = Pane([], vdir)
    try:
        pane.expect("tuia")
        pane.expect_code()
        pane.key("Enter")
        pane.expect("copied")
        pane.key("a")
        pane.expect("Name for this token")
        check(open(env["SHIM_CLIP"]).read() == "", "the copied code was still on the clipboard while the flow ran")
        pane.send("wiza")
        pane.expect("Token (paste")
        pane.send(fx_b)
        pane.expect("PIN (Enter")
        pane.send("")
        shown = pane.expect("2 tokens")
        check("wiza" in shown, "the new token is missing from the view:\n" + shown)
        pane.expect("a add")
        pane.key("a")
        pane.expect("Name for this token")
        pane.key("C-c")
        pane.expect("Press Enter")
        pane.key("Enter")
        shown = pane.expect("2 tokens")
        check("▌" in shown, "the view did not come back after Ctrl-C:\n" + shown)
        pane.key("q")
        shown = pane.expect("__EXIT__")
        check(exit_code(shown) == 0 and "▌" not in shown, "the view did not restore the screen on quit:\n" + shown)
    finally:
        pane.close()
    check(keychain_get("wiza") == ctf_b, "wiza was not stored")


@scenario("S50 a helper crash after the commit keeps its captured output in a 0600 file and says the code is spent")
def _():
    rc, out, err = kip_add("crash", mode="crash_after_seed")
    check(rc == 1 and not stored("crash"), "exit %d" % rc)
    where = kept_path(err)
    files = os.listdir(where)
    check(len(files) == 1 and "spent" in err, "kept %r, message %r" % (files, err))
    kept = os.path.join(where, files[0])
    check(stat.S_IMODE(os.stat(kept).st_mode) == 0o600 and KIP_SEED.hex() in open(kept).read(), "the seed is not in the kept file")
    check(KIP_SEED.hex() not in out + err, "the seed was printed")
    shutil.rmtree(where)


@scenario("S52 a crash that leaves a partial template still keeps the helper's stdout, which holds the seed")
def _():
    rc, out, err = kip_add("partial", mode="partial_template")
    check(rc == 1 and not stored("partial"), "exit %d, stored %s" % (rc, stored("partial")))
    where = kept_path(err)
    kept = os.path.join(where, "helper-output.txt")
    check(os.path.isfile(kept), "stdout was not kept; the folder holds %r" % os.listdir(where))
    check(stat.S_IMODE(os.stat(kept).st_mode) == 0o600 and KIP_SEED.hex() in open(kept).read(), "the kept stdout lacks the seed or is not 0600")
    check(KIP_SEED.hex() not in out + err, "the seed was printed")
    shutil.rmtree(where)


@scenario("S53 an alias may hold spaces: add, code, frame, rename, export, rm and the wizard all take it")
def _():
    sdir = os.path.join(work, "spaces")
    env_dir = {"STOKEN_TUI_DIR": sdir}
    accounts_to_clean.update(("my vpn", "work vpn", "tv room"))
    rc, out, err = run("add", "my vpn", "--file", fx_a, extra_env=env_dir)
    check(rc == 0 and keychain_get("my vpn") == ctf_a, "add 'my vpn': exit %d %s" % (rc, err))
    check(open(os.path.join(sdir, "index")).read().split("\n")[:1] == ["my vpn"], "the index does not hold the full name")
    rc, out, err = run("code", "my vpn", "--use-time", str(T), extra_env=env_dir)
    check(rc == 0 and out.strip() == exp_a, "code: %r %s" % (out, err))
    rc, out, _ = run("--once", "--use-time", str(T), extra_env=env_dir)
    check("my vpn" in out, "the frame lost the name: %r" % out)
    rc, out, err = run("rename", "my vpn", "work vpn", extra_env=env_dir)
    check(rc == 0 and keychain_get("work vpn") == ctf_a and keychain_get("my vpn") is None, "rename: exit %d %s" % (rc, err))
    rc, out, err = run("export", "work vpn", extra_env=env_dir)
    check(rc == 0 and out.strip() == ctf_a, "export: %r %s" % (out, err))
    rc, out, err = run("rm", "work vpn", "--yes", extra_env=env_dir)
    check(rc == 0 and "work vpn.trashed-" in out and keychain_get("work vpn") is None, "rm: exit %d %r %s" % (rc, out, err))
    shown = wizard(["add"], os.path.join(work, "spaces-wizard"), [("Name", "tv room"), ("Token", fx_a), ("PIN", "")])
    check(exit_code(shown) == 0 and keychain_get("tv room") == ctf_a, "the wizard refused a name with a space:\n" + shown)


@scenario("S51 no tokens on a terminal: the view opens with an empty state, and a adds the first token")
def _():
    accounts_to_clean.add("first")
    pane = Pane([], os.path.join(work, "empty51"))
    try:
        shown = pane.expect("No tokens yet")
        check("Press a" in shown, "the empty state does not mention a:\n" + shown)
        pane.key("a")
        pane.expect("Name for this token")
        pane.send("first")
        pane.expect("Token (paste")
        pane.send(fx_a)
        pane.expect("PIN (Enter")
        pane.send("")
        shown = pane.expect("1 token")
        check("first" in shown, "the new row did not appear:\n" + shown)
        pane.key("q")
        check(exit_code(pane.expect("__EXIT__")) == 0, "quit failed")
    finally:
        pane.close()


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
