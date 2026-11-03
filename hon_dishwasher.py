"""hon-dishwasher - JSON CLI for dishwashers on Haier's hOn cloud (Hoover, Candy, Haier).

Built to be driven by scripts and LLM agents: every invocation prints exactly one
JSON document on stdout, {"ok": true, ...} or {"ok": false, "error": "..."}.

Exit codes: 0 ok, 1 refused / bad input, 2 hOn login failed, 3 hOn API rejected the command.

Credentials come from HON_EMAIL / HON_PASSWORD, else the macOS Keychain item with
service "hon" (account = email), else the Linux Secret Service with the same attributes.
The hOn session is cached in ~/Library/Caches/hon-dishwasher/session.json on macOS and
~/.cache/hon-dishwasher/session.json on Linux (mode 0600), so repeated calls don't log in again.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import warnings
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from pyhon import HonAPI
from pyhon.appliance import HonAppliance
from pyhon.connection.api import TestAPI
from pyhon.connection.auth import HonAuth, HonAuthData
from pyhon.exceptions import ApiError, HonAuthenticationError
from pyhon.parameter.fixed import HonParameterFixed
from pyhon.parameter.program import HonParameterProgram
from pyhon.parameter.range import HonParameterRange

KEYCHAIN_SERVICE = "hon"
MOBILE_ID = "hon-dishwasher"
SESSION_CACHE = (
    Path.home() / "Library/Caches"
    if sys.platform == "darwin"
    else Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
) / "hon-dishwasher/session.json"
# CIAM tokens expire after ~15 min; refresh proactively before that.
TOKEN_MAX_AGE = timedelta(minutes=10)

# machMode / prPhase codes, from the hOn Home Assistant integration.
MACH_MODE = {
    0: "ready",
    1: "ready",
    2: "running",
    3: "paused",
    4: "scheduled",
    5: "scheduled",
    6: "error",
    7: "finished",
    8: "test",
    9: "ending",
}
DW_PHASE = {0: "idle", 1: "prewash", 2: "washing", 3: "rinse", 4: "drying", 5: "done", 6: "hot_rinse"}
# Applied to every start unless overridden with --set (e.g. --set openDoor=0).
DEFAULT_OPTIONS = {"openDoor": "1"}
SIMPLE_COMMANDS = {"stop": "stopProgram", "pause": "pauseProgram", "resume": "resumeProgram"}

EPILOG = """\
typical flow:
  hon-dishwasher status                what is it doing, is remote control enabled
  hon-dishwasher programs              program names
  hon-dishwasher programs eco          options for one program (enum values, ranges)
  hon-dishwasher start eco --set extraDry=1 --dry-run
  hon-dishwasher start eco --set extraDry=1
  hon-dishwasher stop

remote start only works when remote control is enabled on the machine itself
(status.remote_control_enabled == true) and the door is closed.
"""


class CliError(Exception):
    exit_code = 1


class LoginError(CliError):
    exit_code = 2


class RejectedError(CliError):
    exit_code = 3


def credentials() -> tuple[str, str]:
    email, password = os.environ.get("HON_EMAIL"), os.environ.get("HON_PASSWORD")
    if email and password:
        return email, password
    if sys.platform == "darwin":
        return keychain_credentials()
    return secret_service_credentials(email)


def secret_service_credentials(email: str | None) -> tuple[str, str]:
    """Linux: GNOME Keyring / Secret Service, attributes service=hon account=<email>."""
    try:
        password = subprocess.run(
            ["secret-tool", "lookup", "service", KEYCHAIN_SERVICE],
            capture_output=True, text=True, check=True,
        ).stdout.rstrip("\n")
        # secret-tool prints attribute.* lines on stderr, the rest on stdout: read both.
        found = subprocess.run(
            ["secret-tool", "search", "service", KEYCHAIN_SERVICE], capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise LoginError(
            "no hOn credentials (or the keyring is locked): set HON_EMAIL/HON_PASSWORD or run "
            f"`secret-tool store --label=hOn service {KEYCHAIN_SERVICE} account YOUR_EMAIL`"
        ) from error
    if match := re.search(r"^attribute\.account = (.+)$", found.stdout + found.stderr, re.MULTILINE):
        email = match.group(1).strip()
    if not (email and password):
        raise LoginError(f'keyring item service="{KEYCHAIN_SERVICE}" needs an account attribute (email)')
    return email, password


def keychain_credentials() -> tuple[str, str]:
    lookup = ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE]
    try:
        meta = subprocess.run(lookup, capture_output=True, text=True, check=True).stdout
        password = subprocess.run(
            [*lookup, "-w"], capture_output=True, text=True, check=True
        ).stdout.rstrip("\n")
    except subprocess.CalledProcessError as error:
        raise LoginError(
            "no hOn credentials: set HON_EMAIL/HON_PASSWORD or run "
            f"`security add-generic-password -s {KEYCHAIN_SERVICE} -a YOUR_EMAIL -w`"
        ) from error
    if not (match := re.search(r'"acct"<blob>="([^"]*)"', meta)):
        raise LoginError(f'keychain item "{KEYCHAIN_SERVICE}" has no account (email)')
    return match.group(1), password


async def restore_session(auth: HonAuth, email: str) -> None:
    try:
        cached = json.loads(SESSION_CACHE.read_text())
    except (OSError, ValueError):
        return
    if cached.get("email") != email:
        return
    auth._session_id = cached["session_id"]
    auth._code_verifier = cached["code_verifier"]
    auth._auth = HonAuthData(**cached["tokens"])
    auth._expires = datetime.fromisoformat(cached["issued"])
    if datetime.utcnow() - auth._expires > TOKEN_MAX_AGE:
        await auth.refresh()


def save_session(auth: HonAuth, email: str) -> None:
    if not (auth.id_token and auth.cognito_token):
        return
    SESSION_CACHE.parent.mkdir(parents=True, exist_ok=True)
    cached = {
        "email": email,
        "session_id": auth._session_id,
        "code_verifier": auth._code_verifier,
        "tokens": asdict(auth._auth),
        "issued": auth._expires.isoformat(),
    }
    fd = os.open(SESSION_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(cached, file)


async def load_appliances(api: HonAPI) -> list[HonAppliance]:
    appliances = []
    for info in await api.load_appliances():
        appliance = await HonAppliance(api, info).create()
        if appliance.mac_address:
            appliances.append(appliance)
    return appliances


def pick(appliances: list[HonAppliance], selector: str | None) -> HonAppliance:
    if selector:
        wanted = selector.lower()
        matches = [
            a
            for a in appliances
            if wanted
            in (a.nick_name.lower(), a.mac_address.lower(), a.appliance_type.lower(), a.model_name.lower())
        ]
    else:
        matches = [a for a in appliances if a.appliance_type == "DW"] or appliances
    if len(matches) != 1:
        found = [f"{a.appliance_type} {a.nick_name} ({a.mac_address})" for a in appliances]
        reason = "no appliance matches" if not matches else "several appliances match"
        raise CliError(f"{reason}; use --appliance with a name, MAC or type: {found}")
    return matches[0]


def param(appliance: HonAppliance, key: str):
    attribute = appliance.attributes.get("parameters", {}).get(key)
    return None if attribute is None else attribute.value


def as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def flag(value) -> bool | None:
    return None if (code := as_int(value)) is None else code == 1


def identity(appliance: HonAppliance) -> dict:
    return {
        "name": appliance.nick_name,
        "type": appliance.appliance_type,
        "brand": appliance.brand,
        "model": appliance.model_name,
        "mac": appliance.mac_address,
    }


def is_connected(appliance: HonAppliance) -> bool:
    return appliance.attributes.get("lastConnEvent", {}).get("category") != "DISCONNECTED"


def status(appliance: HonAppliance, raw: bool = False) -> dict:
    mode = as_int(param(appliance, "machMode"))
    phase = as_int(param(appliance, "prPhase"))
    result = {
        "appliance": identity(appliance),
        "connected": is_connected(appliance),
        "remote_control_enabled": flag(param(appliance, "remoteCtrValid")),
        "mode": MACH_MODE.get(mode, mode),
        "phase": DW_PHASE.get(phase, phase) if appliance.appliance_type == "DW" else phase,
        "program": appliance.attributes.get("programName"),
        "remaining_minutes": as_int(param(appliance, "remainingTimeMM")),
        "delay_minutes": as_int(param(appliance, "delayTime")),
        "door_open": flag(param(appliance, "doorStatus")),
        "salt_low": flag(param(appliance, "saltStatus")),
        "rinse_aid_low": flag(param(appliance, "rinseAidStatus")),
        "error_code": param(appliance, "errors"),
        "commands": sorted(appliance.commands),
    }
    if raw:
        result["raw_parameters"] = {
            key: attribute.value
            for key, attribute in appliance.attributes.get("parameters", {}).items()
        }
    return result


def describe(setting) -> dict:
    if isinstance(setting, HonParameterRange):
        return {"type": "range", "min": setting.min, "max": setting.max, "step": setting.step, "value": setting.value}
    if isinstance(setting, HonParameterFixed):
        return {"type": "fixed", "value": setting.value}
    return {"type": "enum", "values": setting.values, "value": setting.value}


def program_parameter(appliance: HonAppliance) -> HonParameterProgram:
    if not (start := appliance.commands.get("startProgram")):
        raise CliError(f"appliance has no startProgram command; commands: {sorted(appliance.commands)}")
    if not isinstance(program := start.settings.get("program"), HonParameterProgram):
        raise CliError("startProgram has no program selector")
    return program


def settable(command, include_fixed: bool = False) -> dict:
    return {
        key: describe(setting)
        for key, setting in sorted(command.settings.items())
        if setting.group == "parameters"
        and key != "program"
        and (include_fixed or not isinstance(setting, HonParameterFixed))
    }


def programs(appliance: HonAppliance, name: str | None, include_fixed: bool) -> dict:
    program = program_parameter(appliance)
    if not name:
        return {"programs": program.values, "selected": program.value}
    if name not in program.values:
        raise CliError(f"unknown program {name!r}; programs: {program.values}")
    command = appliance.commands["startProgram"].categories[name]
    return {"program": name, "options": settable(command, include_fixed)}


def ensure_controllable(appliance: HonAppliance, force: bool) -> None:
    if not is_connected(appliance):
        raise CliError("appliance is offline (last connection event: DISCONNECTED)")
    if force:
        return
    if flag(param(appliance, "remoteCtrValid")) is False:
        raise CliError("remote control is disabled on the appliance; enable it on the machine first")


async def start(appliance: HonAppliance, args) -> dict:
    program = program_parameter(appliance)
    if args.program not in program.values:
        raise CliError(f"unknown program {args.program!r}; programs: {program.values}")
    program.value = args.program
    command = appliance.commands["startProgram"]
    # Applied to every start; --set overrides them. Skipped when a program doesn't expose the option.
    for key, value in DEFAULT_OPTIONS.items():
        setting = command.settings.get(key)
        if setting is not None and setting.group == "parameters" and not isinstance(setting, HonParameterFixed):
            setting.value = value
    for assignment in args.set:
        key, sep, value = assignment.partition("=")
        if not sep:
            raise CliError(f"--set expects key=value, got {assignment!r}")
        setting = command.settings.get(key)
        if setting is None or setting.group != "parameters" or key == "program":
            raise CliError(f"unknown option {key!r} for {args.program}; options: {sorted(settable(command))}")
        if isinstance(setting, HonParameterFixed):
            raise CliError(f"option {key!r} is fixed at {setting.value!r} for {args.program}")
        try:
            setting.value = value
        except ValueError as error:
            raise CliError(f"invalid value for {key}: {error}") from error
    ensure_controllable(appliance, args.force)
    if not args.force and MACH_MODE.get(as_int(param(appliance, "machMode"))) in ("running", "paused", "scheduled"):
        raise CliError("a program is already running/paused/scheduled; stop it first or pass --force")
    parameters = command.parameter_groups.get("parameters", {})
    if args.dry_run:
        return {"dry_run": True, "program": args.program, "parameters": parameters}
    await send(command)
    return {"sent": "startProgram", "program": args.program, "parameters": parameters}


async def simple_command(appliance: HonAppliance, action: str, args) -> dict:
    name = SIMPLE_COMMANDS[action]
    if not (command := appliance.commands.get(name)):
        raise CliError(f"appliance has no {name} command; commands: {sorted(appliance.commands)}")
    ensure_controllable(appliance, args.force)
    if args.dry_run:
        return {"dry_run": True, "command": name}
    await send(command)
    return {"sent": name}


async def send(command) -> None:
    try:
        accepted = await command.send()
    except ApiError as error:
        raise RejectedError(f"hOn rejected {command.name}") from error
    if not accepted:
        raise RejectedError(f"hOn rejected {command.name} (not authenticated)")


async def dispatch(api: HonAPI, args) -> dict:
    appliances = await load_appliances(api)
    if args.action == "list":
        for appliance in appliances:
            await appliance.load_attributes()
        return {
            "appliances": [
                {**identity(a), "connected": is_connected(a), "mode": MACH_MODE.get(as_int(param(a, "machMode")))}
                for a in appliances
            ]
        }
    appliance = pick(appliances, args.appliance)
    await appliance.load_commands()
    await appliance.load_attributes()
    if args.action == "status":
        return status(appliance, raw=args.raw)
    if args.action == "programs":
        return programs(appliance, args.program, args.all)
    if args.action == "start":
        return await start(appliance, args)
    return await simple_command(appliance, args.action, args)


async def run(args) -> dict:
    if test_data := os.environ.get("HON_TEST_DATA"):
        return await dispatch(TestAPI(Path(test_data)), args)
    email, password = credentials()
    async with HonAPI(email, password, mobile_id=MOBILE_ID) as api:
        try:
            await restore_session(api.auth, email)
            return await dispatch(api, args)
        except HonAuthenticationError as error:
            SESSION_CACHE.unlink(missing_ok=True)
            raise LoginError(f"hOn login failed: {error}") from error
        finally:
            save_session(api.auth, email)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str):
        emit({"ok": False, "error": f"{message} (see `hon-dishwasher --help`)"})
        sys.exit(CliError.exit_code)


def parser() -> argparse.ArgumentParser:
    root = JsonArgumentParser(
        prog="hon-dishwasher",
        description="Control a dishwasher on the hOn cloud (Hoover, Candy, Haier). Output is always JSON.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.add_argument("-a", "--appliance", help="name, MAC or type (default: the dishwasher)")
    root.add_argument("-v", "--verbose", action="store_true", help="library logs on stderr")
    sub = root.add_subparsers(dest="action", required=True, metavar="COMMAND")

    sub.add_parser("list", help="appliances on the account")
    status_cmd = sub.add_parser("status", help="current state of the appliance")
    status_cmd.add_argument("--raw", action="store_true", help="include every raw hOn parameter")

    programs_cmd = sub.add_parser("programs", help="program names, or options of one program")
    programs_cmd.add_argument("program", nargs="?")
    programs_cmd.add_argument("--all", action="store_true", help="also show fixed (non-settable) parameters")

    start_cmd = sub.add_parser("start", help="start a program")
    start_cmd.add_argument("program")
    start_cmd.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="program option, repeatable")

    for action, command in SIMPLE_COMMANDS.items():
        sub.add_parser(action, help=f"send {command}")

    for name in ("start", *SIMPLE_COMMANDS):
        sub.choices[name].add_argument("--dry-run", action="store_true", help="validate and print, send nothing")
        sub.choices[name].add_argument(
            "--force", action="store_true", help="skip the remote-control and already-running checks"
        )

    sub.add_parser("logout", help="forget the cached hOn session")
    return root


def emit(document: dict) -> None:
    print(json.dumps(document, indent=2, default=str))


def main() -> None:
    args = parser().parse_args()
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.CRITICAL, stream=sys.stderr)
    if args.action == "logout":
        SESSION_CACHE.unlink(missing_ok=True)
        emit({"ok": True, "logged_out": True})
        return
    try:
        result = asyncio.run(run(args))
    except CliError as error:
        emit({"ok": False, "error": str(error)})
        sys.exit(error.exit_code)
    emit({"ok": True, **result})


if __name__ == "__main__":
    main()
