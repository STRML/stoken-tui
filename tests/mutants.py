#!/usr/bin/env python3
"""Mutation check for tests/e2e.py: does each scenario fail when the behavior it guards is broken?

    python3 tests/mutants.py --check          confirm every mutation still applies to stoken-tui
    python3 tests/mutants.py                  run them all (about 2 minutes each)
    python3 tests/mutants.py --only rename    run the ones whose name contains "rename"

Each mutation copies stoken-tui and touchid.swift into a temp directory, breaks one line, and runs the
suite against the copy. The mutation passes when every scenario it names goes red. Needs the same
tools as the suite (stoken, security, tmux), so run it outside any sandbox.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = 'GATED_COMMANDS = (None, "code", "export")'

# name, text to find (exactly once), replacement, scenarios that must go red
MUTANTS = [
    ("seed on argv", 'return run_stoken([*args, "--file=/dev/fd/%d" % read_fd], stdin_text, (read_fd,))',
     'return run_stoken([*args, "--token=" + seed], stdin_text, ())', {"S26"}),
    ("no Keychain read-back", "if kc_get(account, pin) != value:", "if False:", {"S14"}),
    ("index not rolled back", "        except BaseException:\n            write_index(aliases)\n            with contextlib.suppress(Exception):\n                kc_delete(alias)",
     "        except BaseException:\n            with contextlib.suppress(Exception):\n                kc_delete(alias)", {"S14", "S20"}),
    ("no index lock", "        fcntl.flock(lock, fcntl.LOCK_EX)\n", "", {"S21"}),
    ("rm drops the seed", "        keep = None if args.purge or seed is None else", "        keep = None if True else", {"S10"}),
    ("orphan item ignored", "    if alias in aliases or kc_get(alias) is not None:", "    if alias in aliases:", {"S19"}),
    ("PIN not stored", "            if pin:\n                kc_put(alias, pin, pin=True)\n            verify_readable",
     "            verify_readable", {"S8"}),
    ("export ungated", GATE, 'GATED_COMMANDS = (None, "code")', {"S22"}),
    ("live view ungated", GATE, 'GATED_COMMANDS = ("code", "export")', {"S22"}),
    ("gate failure ignored", '    raise Fail("Touch ID check failed: %s%s" % (res.stderr.strip() or "cancelled", hint))', "    return", {"S22"}),
    ("clipboard never cleared", "        if clip:\n            clear_clipboard(clip[0])", "        pass", {"S17"}),
    ("Keychain timeout unmapped", "        raise KeychainUnavailable(124)", "        raise", {"S20"}),
    ("locked Keychain looks missing", "    raise KeychainUnavailable(res.returncode)", "    return None", {"S15"}),
    ("next code equals current", "    for offset in (0, row.interval):", "    for offset in (0, 0):", {"S30", "S31", "S32"}),
    ("n copies the current code", 'upcoming=key == "next")', "upcoming=False)", {"S32"}),
    ("code --next ignored", "print(row.next_code if args.next else row.code)", "print(row.code)", {"S30"}),
    ("rename keeps the old name", "        write_index([a for a in aliases if a != args.old] + [args.new])",
     "        write_index(aliases + [args.new])", {"S33"}),
    ("rename drops the PIN", "            if pin:\n                kc_put(args.new, pin, pin=True)",
     "            if False:\n                kc_put(args.new, pin, pin=True)", {"S33"}),
    ("activation code on argv", "CTKIP_STUB, url, os.path.join(workdir, TEMPLATE_NAME)]",
     "CTKIP_STUB, url, os.path.join(workdir, TEMPLATE_NAME), code]", {"S45"}),
    ("alias checked after provisioning", "        check_alias(alias)\n        preflight_alias(alias)", "        check_alias(alias)", {"S38"}),
    ("MAC ignored", 'if "MAC verified" not in result.stdout or "MAC not verified" in result.stdout:', "if False:", {"S41"}),
    ("recovery never kept", '    if not committed or "MAC not verified" in result.stdout:', "    if True:", {"S43", "S44", "S50"}),
    ("template tag not fixed", 'fixed = template.replace("DefAddPin>", "DefAddPIN>")', "fixed = template", {"S35", "S36"}),
    ("helper outlives a timeout", "errors=\"replace\", env=environment, start_new_session=True)",
     "errors=\"replace\", env=environment, start_new_session=False)", {"S42", "S44"}),
    ("host not confirmed", '    if ask_line("Continue? [y/N] ").lower() != "y":', "    if False:", {"S46"}),
    ("activation code not scrubbed", 'return HEX_RUN_RE.sub("***", text.replace(code, "***"))', "return text", {"S40"}),
    ("a leaves the clipboard", "                    if clip:\n                        clear_clipboard(clip[0])\n                        clip = None\n                    added =",
     "                    added =", {"S49"}),
    ("sh trampoline accepted", "os.path.basename(interpreter) in NO_SHEBANG_INTERPRETERS or ", "", {"S39"}),
    ("http links accepted", 'parts.scheme != "https"', 'parts.scheme not in ("https", "http")', {"S37"}),
    ("requests keep the connection", '    session.headers["Connection"] = "close"', "    pass", {"S35"}),
]


def load_source():
    with open(os.path.join(ROOT, "stoken-tui")) as f:
        return f.read()


def applies(source, old):
    return source.count(old) == 1


def run_mutant(source, old, new):
    """Return the set of scenarios that failed, or None when the suite hung."""
    with tempfile.TemporaryDirectory(prefix="stoken-tui-mutant-") as work:
        tool = os.path.join(work, "stoken-tui")
        with open(tool, "w") as f:
            f.write(source.replace(old, new, 1))
        os.chmod(tool, 0o755)
        shutil.copy(os.path.join(ROOT, "touchid.swift"), work)
        env = dict(os.environ, STOKEN_TUI_BIN=tool, STOKEN_TUI_RESULTS=os.path.join(work, "e2e-results.txt"))
        try:
            done = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "tests", "e2e.py")], capture_output=True,
                                  text=True, env=env, timeout=900)
        except subprocess.TimeoutExpired:
            return None
    return set(re.findall(r"^FAIL\s+(S\d+)", done.stdout, re.M))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only confirm each mutation applies to the source")
    parser.add_argument("--only", default="", help="run the mutations whose name contains this text")
    args = parser.parse_args()
    chosen = [m for m in MUTANTS if args.only in m[0]]
    if not chosen:
        sys.exit("no mutation name contains %r; run with --check to list them" % args.only)
    source, bad = load_source(), 0
    for name, old, new, expected in chosen:
        if not applies(source, old):
            print("STALE     %s: its text no longer appears exactly once in stoken-tui" % name)
            bad += 1
            continue
        if args.check:
            print("applies   %s" % name)
            continue
        reds = run_mutant(source, old, new)
        if reds is None:
            print("TIMED OUT %s: the suite hung for 15 minutes, so nothing was checked" % name)
            bad += 1
            continue
        missing = sorted(expected - reds)
        print("%-9s %s -> red: %s%s" % ("caught" if not missing else "SURVIVED", name, ",".join(sorted(reds)) or "none",
                                         "  (expected %s too)" % ",".join(missing) if missing else ""))
        bad += bool(missing)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
