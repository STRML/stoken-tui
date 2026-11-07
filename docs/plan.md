# stoken-tui plan

`stoken-tui` gives stoken (RSA SecurID software tokens) the feel of a TOTP app in the terminal. It
reads every token at once, keeps each seed in the macOS Keychain, and shows the codes under their
aliases with a countdown.

## The abstraction

The first design kept each token in an encrypted rc file with a random password, and put that password
in the Keychain. That was two encrypted stores per token, plus the glue between them: setpass, setpin,
re-keying, temp files, trash renames and orphan checks. All three medium findings in the security
review came from that glue.

The seed is the only secret, and a code is a pure function of the seed and the time. So the seed goes
in the Keychain, and stoken runs statelessly with the seed on a pipe.

    before   token -> rc file -> random password -> Keychain
    now      token -> seed in Keychain -> stoken reads it from a pipe

What that removed: rc files, per-token passwords, `setpass`, `setpin`, re-keying, temp files in the
token directory, and trash renames of rc files.

What it costs: a plain-text list of alias names (`~/.stoken/index`), because the Keychain cannot list
items by service. The trust level is unchanged: before and after, any process running as you can read
the Keychain item.

Higher up the stack, an authenticator app with pluggable algorithms (a fork of Ente Auth or Aegis plus
libstoken) would turn token types times features into token types plus features. It is a maintained
fork with no iOS version, so it waits. The highest level is asking the issuer for a standard
authenticator or a passkey, which removes this tool entirely and is a phone call.

## Decisions

Rows marked "probed" were measured against stoken 0.93 and macOS 27.2's `security` before they went in.

| Decision | Reason |
|---|---|
| One stdlib-only Python file, `~/git/oss/stoken-tui/stoken-tui`, symlinked into `~/.local/bin` | Nothing to install except stoken. |
| The Keychain holds the seed as stoken's plain 81-digit string, one item per alias (service `stoken-tui`, account = alias). A PIN, if any, lives in a second service, `stoken-tui.pin`. | Probed: `stoken export` turns an sdtid, a CTF string or an iPhone link into the 81-digit form, and `--file` accepts it back. |
| stoken reads the seed from a pipe: `--file=/dev/fd/N` with the pipe passed down as `pass_fds`. The PIN goes on stdin with `--stdin`. | Probed: codes match a direct run, and `--stdin` allows one prompt, so the seed must not use stdin. No token file or temp file ever exists. |
| `~/.stoken/index` lists alias names only (mode 0600, directory 0700). It is rewritten atomically under an `flock`. | The Keychain cannot list items by service. The list holds no secrets. |
| `add` order: write the index entry, store the seed, store the PIN, read the seed back through stoken. Any failure or SIGTERM or SIGHUP undoes all of it. | The visible state (the index) comes first, so a crash leaves an entry `rm` can clean up, never an invisible orphan. |
| Keychain writes use `security -i` with one command per call, the value as hex (`-X`), then a read-back. | Probed: hex round-trips spaces, quotes and backslashes. Probed: `security -i` exits 0 when one of several piped commands fails; with a single command it exits 44. The read-back catches a silent loss (S14). |
| Keychain read status 44 means "no seed". Any other nonzero status means "Keychain unavailable". | A locked Keychain must not look like a missing seed. |
| `security` is called by absolute path (override: `STOKEN_TUI_SECURITY`). The Keychain service and account names are matched with `fullmatch` on every call. | `$` accepts a trailing newline, which would split a `security -i` line. |
| `add` refuses when the index or the Keychain already has the alias, and `kc_put` never overwrites. | An orphan item can be the only copy of a seed. |
| `rm` renames the seed to the account `<alias>.trashed-<epoch>` and names it in its output. `rm --purge` deletes it. | A removed token stays recoverable unless you say otherwise. |
| `adopt` decrypts an existing rc with its password (`stoken export --stdin`), then stores the seed. The original is left alone. | The password is used once and never stored. The tool tells you to trash the original. |
| Seeds, PINs and passwords never reach argv. `--token -` reads a token from stdin, and on a TTY it is read with a hidden prompt. `STOKEN_TUI_PIN` and `STOKEN_TUI_PASSWORD` are scripting hooks that the tool removes from its own environment as soon as it reads them. | argv shows up in `ps` and shell history. Scenario S26 greps every logged argv. |
| The live view, `code` and `export` run a Touch ID check first (`LAContext`, policy `deviceOwnerAuthentication`, so the login password or Apple Watch also work). `add`, `adopt` and `rm` do not. A small Swift helper, `touchid.swift`, is compiled once into `~/.cache/stoken-tui` and reused. `STOKEN_TUI_AUTH=off` skips it; any other value replaces the helper with that command (tests). | It stops casual use of the app and fails closed. It does not protect the Keychain item from other processes. A Keychain access control that demands biometry needs the data-protection keychain, which an unsigned command-line tool cannot use. |
| A code copied from the live view is cleared from the clipboard after 30 seconds and on quit, only if the clipboard still holds it. The view exits without waiting for worker threads. | Codes expire in a minute, and a stuck stoken must not hold the terminal. |
| The live view is drawn with plain ANSI escape sequences (alternate screen, one absolute-positioned write per tick), not curses. Input comes from a raw-mode `select` loop that understands arrows, `j`/`k`, Enter, `c`, `q` and Esc. | Measured: ncurses' repeat-character optimization emitted a stray byte 0x01 before `ESC[Nb` for runs of the heavy bar glyph, in a normal UTF-8 session. Python's curses also needs `setlocale` before it draws any non-ASCII, which was the diamonds in the first screenshot. |
| Each token is a three-line card: an accent marker on the selected one, the name, the grouped code, a half-cell-resolution countdown bar and the seconds, tinted green, yellow under 20 s and red under 10 s. The title line shows the token count and the footer shows the keys or a "copied" toast. Long lists scroll with the selection. `STOKEN_TUI_ASCII=1` swaps every glyph for ASCII. | A TOTP app shows the code large with a draining ring; this is the terminal equivalent. |
| The next code is the code at `now + interval`, computed with `--use-time`. A row holds both, fetched together and keyed to the window of the time the codes were computed, not the time the fetch finished. `n` copies it and `code --next` prints it. | `stoken --next` can't combine with `--use-time` (the tests need it), and the two forms are identical (probed: `--next` equals `--use-time=+60`). Keying the window to the compute time stops a fetch that straddles a boundary from showing the old window's code for a whole minute. |
| `rename OLD NEW` writes the seed (and PIN) under the new name, swaps the index entry, then deletes the old items. Any failure undoes the new items and the index. | The name is both an index entry and a Keychain account, so editing either alone loses the token. |
| Tokens decrypt in a thread pool. A code recomputes only when its interval window changes. | One round of parallel calls covers every token. |

## Interface

    stoken-tui                         live view: curses on a TTY, one frame otherwise
    stoken-tui add ALIAS (--file F | --token -)    token from a file, or from stdin
    stoken-tui adopt ALIAS RCFILE      move an existing encrypted rc in
    stoken-tui code ALIAS [--next]     print one code, or the code one interval ahead
    stoken-tui rename OLD NEW          give an alias a new name
    stoken-tui export ALIAS            print the stored 81-digit seed (round-trips through add)
    stoken-tui rm ALIAS [--yes] [--purge]
    test hooks: --use-time T, --once; env STOKEN_TUI_DIR, STOKEN_TUI_SERVICE, STOKEN_TUI_SECURITY,
                STOKEN_TUI_KEYCHAIN_TIMEOUT, STOKEN_TUI_CACHE, STOKEN_TUI_AUTH,
                STOKEN_TUI_PASSWORD, STOKEN_TUI_PIN, STOKEN_TUI_ASCII

Live view keys: up/down or j/k select, Enter or c copy the code with `pbcopy`, n copy the next code, q quit.

A card shows the alias, the grouped code with a countdown bar and the seconds, and the next code in dim text under it. A failed token shows `! no seed in Keychain` (or the cause) in place of the code. The footer has two lines: the keys, and where the alias list lives with the `rename` hint.

## Failure matrix

Each row is one scenario in `tests/e2e.py`.

| State or input | What the tool does | How it can fail | What the caller is told |
|---|---|---|---|
| S1 add, good token | Normalize to an 81-digit seed, index entry, seed, PIN, read-back, reminder to keep the original | Any step fails | Everything is undone; exit 1 with the cause |
| S2 two tokens listed | Parallel stoken calls, one row each; countdown from the token's own interval | One call hangs | 8 s timeout per call; that row shows `! timed out`; the others render |
| S3 duplicate alias | Refuse before any write | Two adds race | The index lock lets one win; the loser exits 1 (S21) |
| S4 garbled token | Normalization fails before anything is written | None reachable | Exit 1; no index entry, no Keychain item, no files |
| S5 hostile alias | The regex rejects it before any write | None reachable | Exit 2; nothing written |
| S6 seed missing from the Keychain | The index lists it, the Keychain has nothing | `rm` left a half state, or a manual delete | Row `! no seed in Keychain`; other rows normal |
| S7 seed unreadable | stoken rejects it | The Keychain item was edited | Row `! seed unreadable`; other rows normal |
| S8 PIN token | The PIN is stored separately and goes to stoken on stdin | No PIN was stored | Row `! PIN required` |
| S9 adopt | Decrypt with the old password via `stoken export`, then register | Wrong password; a later step fails | Wrong password: nothing stored, exit 1. The source file is never modified. |
| S10 rm | Rename the seed to `.trashed`, drop the index entry; `--purge` deletes | The alias is unknown | Unknown alias exits 1; the output names the kept account |
| S11 stoken not installed | Check PATH first | None | Exit 2: `stoken not found on PATH (brew install stoken)` |
| S12 no tokens | Print how to add one | None | Exit 0 |
| S13 controlling terminal | Run stoken with our pipe as stdin | stoken prompts on the terminal | The tool would hang, so the scenario fails on a 40 s timeout |
| S14 Keychain write exits 0 but stores nothing | Read back after the write | `security -i` hides the failure | Rollback, exit 1 |
| S15 Keychain locked, denied or cancelled (status other than 44) | Classify by exit status | Mistaken for "no seed" | Row `! Keychain unavailable (exit N)` |
| S16 export | Print the seed; it round-trips through `add` | Unknown alias | Exit 1 |
| S17 live view | Draw every alias; `j` and Enter copy the selected code; clear the clipboard on quit | A code stays on the clipboard | The scenario checks the clipboard is empty after `q` |
| S18 killed mid-add | The seed exists only in memory and in the Keychain after the read-back | SIGKILL during normalization | No seed on disk anywhere; no index entry; no Keychain item |
| S19 orphan Keychain item | Refuse the add, never overwrite | A stale item is the only copy of an old seed | Exit 1; item unchanged |
| S20 Keychain write times out | `security` timeouts map to `KeychainUnavailable`; any exception rolls the add back | A locked-Keychain dialog hangs past the timeout | Rollback, exit 1 with `Keychain unavailable`, no traceback |
| S21 concurrent adds | The index `flock` serializes read-modify-write | Lost update | Two aliases both land; the same alias lands once |
| S22 Touch ID cancelled or failed | The gate runs before any stoken or Keychain call | The helper exits nonzero | Exit 1 `Touch ID check failed`; nothing read |
| S23 gate scope | Only commands that reveal a secret are gated | The gate blocks `add` or `rm` | Those still run |
| S24 helper compiles; `off` skips | `swiftc` builds it; `--can-evaluate` never prompts | The Swift source stops compiling | The suite fails |
| S25 no compiler; build once | Fail closed naming `swiftc`; reuse the binary by source hash | A partial build is left behind | No cache directory on failure; no `.build-` files after success |
| S26 secrets on argv or at rest | Secrets travel on pipes and stdin only | A change adds `--token=SEED` or writes a file | The suite fails if any logged argv or any file under the token dir holds a seed or a PIN |
| S27 glyph rendering | Plain UTF-8 bytes with or without a locale; ASCII under `STOKEN_TUI_ASCII=1` | A renderer change reintroduces garbled glyphs | The suite fails on a stray 0x01, a replacement character or non-ASCII bytes in ASCII mode |
| S28 layout | Title, token count, cards, bar, seconds and footer on a real screen | A layout regression | The scenario reads a tmux screen capture and checks each part |
| S29 tiny terminal and scrolling | Clip every line to the width; page the cards to the height | Overflow or a crash at 30x7 | No traceback, and the selection scrolls to the last token with `j` and Down |
| S30 next code | `code --next` computes the code one interval ahead | A PIN token loses its PIN | Matches stoken at `T + interval` for PINless and PIN tokens |
| S31 next code on screen | Every card shows `next NNN NNN`, dimmed | The next line drops out | The scenario reads a tmux capture and compares against stoken |
| S32 `n` key | Copies the next code, not the current one | The key copies the wrong code | The pbcopy shim log must hold a next code |
| S33 rename | Seed and PIN move; the index swaps; failures change nothing | A write fails midway; the target exists | Rollback, exit 1; the old token still works |
| S34 footer | The alias list path and the rename hint are on screen | The footer drops them | The scenario reads the last two screen lines |
| Resize, Ctrl-C, SIGTERM | The terminal size is read every tick; `finally` restores the cursor, the alternate screen and the tty modes | A killed view leaves the terminal raw | Restored on every exit path |

## Limits

- Any process running as your user can read a Keychain item through `security`. The Touch ID prompt
  guards this app only.
- The login keychain file (`~/Library/Keychains/login.keychain-db`) sits inside your home directory and
  its Time Machine backups. Against someone who holds those files, the protection is the strength of
  your macOS login password.
- `export` prints the seed to the terminal, so it can land in scrollback. Use it only to save the seed
  in a password manager.
- `rm` keeps the removed token's seed in the Keychain until you use `--purge` or delete the `.trashed`
  account by hand.
- A password-protected sdtid file cannot be added directly: import and decrypt it with stoken first,
  then `adopt` the rc.
- The tool does not import CT-KIP links. `rsa_ct_kip` does that.
