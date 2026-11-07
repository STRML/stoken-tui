<h1 align="center">stoken-tui</h1>

<p align="center">
  Your RSA SecurID tokens in the terminal, laid out like a TOTP app. Every code at once, a countdown on each, seeds in the macOS Keychain behind Touch ID.
</p>

<p align="center">
  <a href="https://github.com/STRML/stoken-tui/actions/workflows/test.yml"><img alt="tests" src="https://github.com/STRML/stoken-tui/actions/workflows/test.yml/badge.svg"></a>
  <img alt="macOS" src="https://img.shields.io/badge/macOS-Keychain%20%2B%20Touch%20ID-000000?logo=apple">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-3776ab">
  <img alt="no Python dependencies" src="https://img.shields.io/badge/python%20deps-none-2ea44f">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

<p align="center">
  <img alt="Five tokens counting down: the 30 second ones turn yellow, then red, then roll over. Copy a code, copy the next one, rename a token." src="demo/demo.gif" width="860">
</p>

RSA SecurID software tokens don't fit any standard authenticator app. [stoken](https://github.com/stoken-dev/stoken) generates the codes, but it handles one token per file and asks for a password on every run. `stoken-tui` keeps every token in the Keychain, reads them all at once, and shows each code under its name with the seconds it has left.

```
by hand   stoken tokencode --rcfile work.rc ─▶ password ─▶ repeat per token
with it   stoken-tui ─▶ Touch ID ─▶ every code, counting down
```

## Install

You need macOS, [stoken](https://github.com/stoken-dev/stoken) (`brew install stoken`) and Python 3.9 or later. The Touch ID helper compiles once with `swiftc`, which comes with the Xcode command line tools (`xcode-select --install`). There are no Python dependencies.

```sh
git clone https://github.com/STRML/stoken-tui
ln -s "$PWD/stoken-tui/stoken-tui" ~/.local/bin/
```

Any directory on your `PATH` works in place of `~/.local/bin`.

## Use

Add a token, then run `stoken-tui`.

```sh
stoken-tui add work --file work.sdtid         # a file from your admin
pbpaste | stoken-tui add vpn --token -        # an activation link or CTF string
stoken-tui adopt old ~/.stokenrc              # a token stoken already holds
stoken-tui
```

<p align="center"><img alt="Five tokens, each with its code, a countdown bar and the next code" src="demo/live.png" width="860"><br><sub>every code at once, the selected one marked</sub></p>

<p align="center"><img alt="The toast reads: copied payroll" src="demo/copy.png" width="860"><br><sub>Enter copies the selected code</sub></p>

<p align="center"><img alt="The toast reads: copied next code of payroll" src="demo/next.png" width="860"><br><sub>n copies the next code</sub></p>

<p align="center"><img alt="The token vpn is now called vpn-work" src="demo/rename.png" width="860"><br><sub>stoken-tui rename vpn vpn-work</sub></p>

The bar and the seconds go green, then yellow under 20 seconds, then red under 10. The code a token will show next sits under the current one, so you can read it before it appears.

| Key | Action |
|---|---|
| <kbd>↑</kbd> <kbd>↓</kbd> or <kbd>j</kbd> <kbd>k</kbd> | Move the selection |
| <kbd>Enter</kbd> or <kbd>c</kbd> | Copy the selected code |
| <kbd>n</kbd> | Copy the selected token's next code |
| <kbd>q</kbd> or <kbd>Esc</kbd> | Quit |

A copied code leaves the clipboard after 30 seconds, or when you quit, if nothing else has replaced it.

### Next codes

An RSA server that doubts a login can ask for the next code, not the current one. The next code is the one a token shows one interval from now (60 seconds on most tokens). `n` copies it, and the same code is one command away:

```sh
stoken-tui code work --next
```

### The commands

| You want to | Run |
|---|---|
| See every code, live | `stoken-tui` |
| Add a token from a file | `stoken-tui add NAME --file token.sdtid` |
| Add a token from a link or CTF string | `pbpaste \| stoken-tui add NAME --token -` |
| Move in a token that stoken already holds | `stoken-tui adopt NAME ~/.stokenrc` |
| Print one code | `stoken-tui code NAME` (`--next` for the next one) |
| Rename a token | `stoken-tui rename OLD NEW` |
| Print the stored seed | `stoken-tui export NAME` |
| Remove a token | `stoken-tui rm NAME` (`--purge` also deletes its seed) |

`add` and `adopt` ask for a PIN when the terminal allows it. Press Enter if the token has none.

Don't edit the name list by hand. The footer shows where it lives (`~/.stoken/index`), but each name is also the Keychain account that holds the seed, so changing one without the other loses the token. `rename` changes both.

## Touch ID

The live view, `code` and `export` ask for Touch ID before they read anything. Your login password and an Apple Watch work too. `add`, `adopt`, `rename` and `rm` don't ask. The first run compiles `touchid.swift` into `~/.cache/stoken-tui` and reuses the result.

Set `STOKEN_TUI_AUTH=off` to skip the prompt, for example over ssh, where no dialog can appear.

This guards the app, not the Keychain item. Any process running as you can still read the item with `security`.

## How it works

```
before   token ─▶ rc file ─▶ random password ─▶ Keychain     two encrypted stores per token
now      token ─▶ seed in the Keychain ─▶ stoken reads it from a pipe
```

The seed is the only secret, and a code is a function of the seed and the time. So the seed sits in the Keychain as stoken's plain 81-digit string, one item per name (a PIN, if the token has one, is a second item). stoken gets the seed on a pipe file descriptor and the PIN on stdin. No token file, password or temp file exists, and a seed never appears in a command line or the environment.

`~/.stoken/index` holds the names and nothing else, because the Keychain can't list items by service.

<details>
<summary><b>Why not curses</b></summary>

<br>

The view draws with plain ANSI escape sequences. In a UTF-8 session, ncurses' repeat-character optimization wrote a stray `0x01` byte before `ESC[Nb` for a run of the heavy bar glyph, which leaves gaps in some terminals. Python's curses also draws nothing but `?` for non-ASCII until `setlocale` runs. A single write per tick over the alternate screen avoids both. Set `STOKEN_TUI_ASCII=1` if your terminal can't draw box characters.

</details>

<details>
<summary><b>Back up and recovery</b></summary>

<br>

The login keychain is the only store, and Time Machine backs it up with your home directory. Your macOS login password protects it. Keep the original token too: the `.sdtid` file, or the activation link or CTF string. `stoken-tui export NAME` prints the stored seed, and `add --token -` accepts it back.

`rm` keeps the seed in the Keychain under `NAME.trashed-<epoch>`, so a removed token can come back. `rm --purge` deletes it.

</details>

<details>
<summary><b>Getting a token in the first place</b></summary>

<br>

If your admin sent an activation link that looks like `com.rsa.securid://ctkip?url=...&activationCode=...`, stoken can't import it, because the link holds a one-time code and a provisioning URL, not the token. [rsa_ct_kip](https://github.com/dlenski/rsa_ct_kip) fetches the token from the server and writes the `.sdtid` file that `add --file` takes. The activation code works once, so keep the file.

If a token made this way shows `! PIN required` although your server uses no PIN, the template that rsa_ct_kip writes is the cause: it spells a tag `DefAddPin`, which stoken ignores ([dlenski/rsa_ct_kip#11](https://github.com/dlenski/rsa_ct_kip/pull/11)). Until that lands, run the conversion by hand with `<DefAddPIN>0</DefAddPIN>` and `<DefLocalPIN>0</DefLocalPIN>` added to the template. With both, `stoken show` reports PIN mode 0.

</details>

## What it relies on

| Need | Source |
|---|---|
| The codes | `stoken tokencode`, with the next code as `--use-time` one interval ahead |
| Seed in, no file | `stoken ... --file=/dev/fd/N` |
| Seed storage | the macOS login keychain, through `security -i` with the value hex-encoded |
| The prompt | `LocalAuthentication`, through a 40-line Swift helper |

## Limits

- Touch ID guards the app. Any process running as you can read the Keychain item through `security`.
- The login keychain file sits in your home directory and its backups. Against someone holding those files, the protection is your macOS login password.
- `export` prints the seed to the terminal, so it can land in scrollback.
- A password-protected `.sdtid` can't be added directly. Decrypt it with stoken first, then `adopt` it.
- It doesn't talk to RSA's activation servers. Use rsa_ct_kip for that.

## Tests

```sh
python3 tests/e2e.py
```

The suite runs 34 scenarios against a temporary directory and a throwaway Keychain service, with `stoken` and `pbcopy` wrapped in shims that log every call. It checks what reached each command line (no seed, PIN or password may), what ended up on disk (no seed anywhere), and what the screen looks like in a real tmux pane. It needs the real `stoken`, `security` and `tmux`, and Touch ID is stubbed out.

A passing suite says little if a scenario can't fail. `python3 tests/mutants.py` breaks the tool in 18 named ways, one line each, and checks that the scenario guarding each one goes red. A full run takes about 40 minutes, so it isn't part of CI. `--check` confirms the mutations still apply to the source in a second.

The screenshots and the animation come from made-up tokens. `demo/fixture.py` builds five of them with different intervals, and `vhs demo/demo.tape` re-renders everything. The design notes and the failure matrix behind each scenario are in [docs/plan.md](docs/plan.md).

## License

[MIT](LICENSE)
