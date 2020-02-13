"""Command-line interface for rhasspy profile manipulation."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from . import Profile
from .download import download_files

_LOGGER = logging.getLogger(__name__)


# -----------------------------------------------------------------------------


async def main():
    """Main method."""
    args = get_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if args.system_profiles:
        args.system_profiles = Path(args.system_profiles)

    if not args.user_profiles:
        if "XDG_CONFIG_HOME" in os.environ:
            args.user_profiles = (
                Path(os.environ["XDG_CONFIG_HOME"]) / "rhasspy" / "profiles"
            )
        else:
            args.user_profiles = Path("~/.config/rhasspy/profiles").expanduser()
    else:
        args.user_profiles = Path(args.user_profiles)

    _LOGGER.debug(args)

    # Load profile
    _LOGGER.debug("Loading profile %s", args.profile)
    profile = Profile(args.profile, args.system_profiles, args.user_profiles)

    # Override profile settings from the command line
    for key, value in args.set:
        try:
            value = json.loads(value)
        except Exception:
            pass

        _LOGGER.debug("Profile: %s=%s", key, value)
        profile.set(key, value)

    # Dispatch to appropriate sub-command
    await args.func(profile, args)


# -----------------------------------------------------------------------------


def get_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(prog="rhasspy-profile")
    parser.add_argument(
        "--debug", action="store_true", help="Print DEBUG messages to the console"
    )
    parser.add_argument(
        "--profile", "-p", required=True, help="Name of profile to load"
    )
    parser.add_argument(
        "--system-profiles",
        help="Directory with base profile files (read only, default=bundled)",
    )
    parser.add_argument(
        "--user-profiles",
        help="Directory with user profile files (read/write, default=$HOME/.config/rhasspy/profiles)",
    )
    parser.add_argument(
        "--set",
        "-s",
        nargs=2,
        action="append",
        help="Override a profile setting value",
        default=[],
    )

    # Create subparsers for each sub-command
    sub_parsers = parser.add_subparsers()
    sub_parsers.required = True
    sub_parsers.dest = "command"

    # -------------------------------------------------------------------------

    # Download settings
    download_parser = sub_parsers.add_parser(
        "download", help="Download required profile artifacts"
    )
    download_parser.set_defaults(func=download)

    return parser.parse_args()


# -----------------------------------------------------------------------------


async def download(profile: Profile, args: argparse.Namespace):
    """Download required profile artifacts."""
    await download_files(profile)
    print("OK")


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
