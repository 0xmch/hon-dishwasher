# hon-dishwasher

A command line for dishwashers on Haier's hOn cloud (Hoover, Candy, Haier), built for scripts
and LLM agents. Every run prints one JSON document.

These machines have no local API. They keep one connection to the hOn cloud, so this tool logs
in to the same service the mobile app uses, through the community library
[pyhon-revived](https://github.com/mmalolepszy/pyhon-revived). It's unofficial and not affiliated
with Haier, and the next time Haier changes its login it will break until the library catches up.

There's a longer write-up on [how and why it works this way](https://0xmch.xyz/posts/hoover-json-cli/).

## Install

You need [uv](https://docs.astral.sh/uv/).

```
uv tool install --python 3.12 git+https://github.com/0xmch/hon-dishwasher
```

## Log in

It looks for your hOn email and password in these places, and never takes them as arguments:

1. `HON_EMAIL` and `HON_PASSWORD` in the environment
2. On macOS, the Keychain: `security add-generic-password -s hon -a YOUR_EMAIL -w`
3. On Linux, the Secret Service: `secret-tool store --label=hOn service hon account YOUR_EMAIL`

The session is cached in `~/Library/Caches/hon-dishwasher/session.json` on macOS and
`~/.cache/hon-dishwasher/session.json` on Linux, readable only by you, so repeated runs don't log
in again. `hon-dishwasher logout` forgets it.

## Commands

```
hon-dishwasher list                    appliances on the account
hon-dishwasher status                  what is it doing, is remote control enabled
hon-dishwasher status --raw            the same, with every raw hOn parameter
hon-dishwasher programs                program names
hon-dishwasher programs eco            options for one program (enum values, ranges)
hon-dishwasher start eco --set extraDry=1 --dry-run
hon-dishwasher start eco --set extraDry=1
hon-dishwasher pause
hon-dishwasher resume
hon-dishwasher stop
```

`start`, `pause`, `resume` and `stop` all take `--dry-run`, which validates and prints without
sending anything. Use `-a` to pick an appliance by name, MAC or type, and `-v` for the library's
logs on stderr.

## Output

Every run prints `{"ok": true, ...}` or `{"ok": false, "error": "..."}`. This is `status`, with
placeholder values:

```json
{
  "ok": true,
  "appliance": {
    "name": "<nickname>",
    "type": "DW",
    "brand": "<brand>",
    "model": "<model>",
    "mac": "<mac>"
  },
  "connected": true,
  "remote_control_enabled": true,
  "mode": "ready",
  "phase": "idle",
  "program": "<program>",
  "remaining_minutes": 0,
  "delay_minutes": 0,
  "door_open": false,
  "salt_low": false,
  "rinse_aid_low": false,
  "error_code": "<code>",
  "commands": ["pauseProgram", "resumeProgram", "startProgram", "stopProgram"]
}
```

| Exit code | Meaning |
|---|---|
| 0 | ok |
| 1 | refused, or bad input |
| 2 | hOn login failed |
| 3 | hOn rejected the command |

`--help` prints normal help text. A network failure that neither this tool nor the library wraps,
such as a DNS error, ends as a Python traceback on stderr.

## What it checks before sending

- A machine that's offline is always refused.
- A machine with remote control turned off is refused. Remote control has to be enabled on the
  machine itself.
- `start` is refused while a program is running, paused or scheduled.

`--force` skips the last two, never the first. The door isn't checked: `status` reports it as
`door_open`, and the caller decides.

`start` also sets `openDoor=1` on every start, where the program offers that option. Pass
`--set openDoor=0` to turn it off.

## Limits

- It has been run against one dishwasher. `-a` can pick other hOn appliances, but they're untested
  and `status` only names the wash phases for dishwashers.
- It only uses hOn's REST calls, so `status` is as fresh as hOn's last report, and nothing streams.
- It depends on a community fork, over an API Haier never published.

With `HON_TEST_DATA` pointing at a directory of recorded appliance data, it runs against the
library's test API, which reads that data from disk and pretends to send commands.

## License

MIT
