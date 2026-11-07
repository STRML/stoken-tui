#!/usr/bin/env python3
"""Made-up tokens for the README demo. Nothing here is a real secret.

    python3 demo/fixture.py setup      build the demo tokens
    python3 demo/fixture.py cleanup    remove them again

Run it with the same environment the tape uses: STOKEN_TUI_SERVICE and STOKEN_TUI_DIR point at a demo
Keychain service and a demo directory, and STOKEN_TUI_AUTH=off skips the Touch ID prompt.
"""
import base64
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "stoken-tui")
TOKENS = [  # alias, seed byte, digits, seconds per code
    ("github", 0x21, 6, 30),
    ("aws-prod", 0x32, 6, 60),
    ("vpn", 0x43, 8, 60),
    ("payroll", 0x54, 6, 30),
    ("vault", 0x65, 8, 60),
]
TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<TKNBatch>
<TKNHeader><Origin>demo</Origin><DefPinType>0</DefPinType><DefLocalPIN>0</DefLocalPIN><DefAddPIN>0</DefAddPIN><DefDigits>{digits}</DefDigits><DefInterval>{interval}</DefInterval></TKNHeader>
<TKN><SN>0000000{serial:05d}</SN><UserLogin>demo</UserLogin><Death>2035/12/31</Death><Seed>={seed}</Seed></TKN>
</TKNBatch>
"""


def setup():
    with tempfile.TemporaryDirectory(prefix="stoken-tui-demo-") as work:
        for serial, (alias, byte, digits, interval) in enumerate(TOKENS, 1):
            template = os.path.join(work, alias + ".xml")
            with open(template, "w") as f:
                f.write(TEMPLATE.format(digits=digits, interval=interval, serial=serial,
                                        seed=base64.b64encode(bytes([byte]) * 16).decode()))
            token = os.path.join(work, alias + ".sdtid")
            with open(token, "w") as out:
                subprocess.run(["stoken", "export", "--random", "--sdtid", "--template", template], stdout=out,
                               stdin=subprocess.DEVNULL, check=True)
            subprocess.run([sys.executable, TOOL, "add", alias, "--file", token], stdout=subprocess.DEVNULL, check=True,
                           env=dict(os.environ, STOKEN_TUI_PIN=""))


def cleanup():
    for alias in [t[0] for t in TOKENS] + ["vpn-work"]:
        subprocess.run([sys.executable, TOOL, "rm", alias, "--yes", "--purge"], capture_output=True)
    directory = os.path.expanduser(os.environ.get("STOKEN_TUI_DIR", ""))
    if directory and "demo" in os.path.basename(directory):
        for name in os.listdir(directory) if os.path.isdir(directory) else []:
            os.unlink(os.path.join(directory, name))
        if os.path.isdir(directory):
            os.rmdir(directory)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("setup", "cleanup"):
        sys.exit(__doc__)
    {"setup": setup, "cleanup": cleanup}[sys.argv[1]]()
