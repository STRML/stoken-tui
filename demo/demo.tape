# Renders the README animation and screenshots from made-up tokens (demo/fixture.py).
#   vhs demo/demo.tape      (run from the repo root)

Output demo/demo.gif

Set Shell bash
Set FontSize 16
Set Width 1120
Set Height 640
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 70ms

Set WaitTimeout 120s
Hide
Type "export STOKEN_TUI_SERVICE=stoken-tui-demo STOKEN_TUI_DIR=$HOME/.stoken-demo STOKEN_TUI_AUTH=off PATH=$PWD:$PATH" Enter
Type "python3 demo/fixture.py cleanup; python3 demo/fixture.py setup" Enter
Type "PS1='\[\e[35m\]❯\[\e[0m\] '; until [ $(( $(date +%s) % 60 )) -eq 58 ]; do sleep 0.2; done; clear; echo RE''ADY" Enter
Wait+Screen /READY/
Type "clear" Enter
Sleep 300ms
Show

Type "stoken-tui"
Sleep 400ms
Enter
Sleep 4s
Screenshot demo/live.png

Down
Sleep 700ms
Down
Sleep 700ms
Enter
Sleep 1500ms
Screenshot demo/copy.png

Type "n"
Sleep 2500ms
Screenshot demo/next.png
Sleep 15s
Type "q"
Sleep 800ms

Type "stoken-tui code vpn --next"
Sleep 500ms
Enter
Sleep 1500ms

Type "stoken-tui rename vpn vpn-work"
Sleep 500ms
Enter
Sleep 1200ms

Type "stoken-tui"
Sleep 300ms
Enter
Sleep 3500ms
Screenshot demo/rename.png
Sleep 1500ms
Type "q"
Sleep 800ms

Hide
Type "python3 demo/fixture.py cleanup" Enter
Sleep 4s
