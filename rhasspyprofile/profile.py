"""Reads and writes profile settings for Rhasspy."""
import collections
import copy
import json
import logging
import typing
from enum import Enum
from pathlib import Path

import json5
import pydash

_LOGGER = logging.getLogger(__name__)
_DIR = Path(__file__).parent

# -----------------------------------------------------------------------------


class ProfileLayers(str, Enum):
    """Layers to a profile (defaults or user settings)"""

    ALL = "all"
    PROFILE = "profile"
    DEFAULTS = "defaults"


# -----------------------------------------------------------------------------


class Profile:
    """Contains all settings for Rhasspy."""

    def __init__(
        self,
        name: str,
        system_profiles_dir: typing.Optional[typing.Union[str, Path]],
        user_profiles_dir: typing.Union[str, Path],
        layers: ProfileLayers = ProfileLayers.ALL,
    ) -> None:

        self.name: str = name
        self.user_profiles_dir = Path(user_profiles_dir)

        if system_profiles_dir:
            self.system_profiles_dir = Path(system_profiles_dir)
        else:
            # Bundled
            self.system_profiles_dir = _DIR / "profiles"

        self.profiles_dirs: typing.List[Path] = [
            self.user_profiles_dir,
            self.system_profiles_dir,
        ]
        self.layers: str = layers
        self.load_profile()

    # -------------------------------------------------------------------------

    @classmethod
    def load_defaults(
        cls, system_profiles_dir: typing.Union[str, Path]
    ) -> typing.Dict[str, typing.Any]:
        """Load default profile settings."""
        system_profiles_dir = Path(system_profiles_dir)
        defaults_path = system_profiles_dir / "defaults.json"

        _LOGGER.debug("Loading default profile settings from %s", defaults_path)
        with open(defaults_path, "r") as defaults_file:
            return json.load(defaults_file)

    # -------------------------------------------------------------------------

    def get(self, path: str, default: typing.Any = None) -> typing.Any:
        """Get setting by path."""
        return pydash.get(self.json, path, default)

    def set(self, path: str, value: typing.Any) -> None:
        """Set setting by path."""
        pydash.set_(self.json, path, value)

    # -------------------------------------------------------------------------

    def load_profile(self) -> None:
        """Load defaults and user settings."""
        # Load defaults first
        self.json: typing.Dict[str, typing.Any] = {}  # no defaults
        self.system_json: typing.Dict[str, typing.Any] = {}  # no defaults

        if self.layers in {ProfileLayers.ALL, ProfileLayers.DEFAULTS}:
            defaults_path = self.system_profiles_dir / "defaults.json"
            with open(defaults_path, "r") as defaults_file:
                self.json = json.load(defaults_file)
                self.system_json = copy.deepcopy(self.json)

        # Load just the system profile.json (on top of defaults)
        system_profile_path = self.system_profiles_dir / self.name / "profile.json"

        with open(system_profile_path, "r") as system_profile_file:
            Profile.recursive_update(self.system_json, json.load(system_profile_file))

        # Overlay with profile
        self.json_path = self.read_path("profile.json")
        if self.layers in {ProfileLayers.ALL, ProfileLayers.PROFILE}:
            # Read in reverse order so user profile overrides system
            for profiles_dir in self.profiles_dirs[::-1]:
                json_path = profiles_dir / self.name / "profile.json"
                _LOGGER.debug("Loading %s", json_path)
                if json_path.is_file():
                    try:
                        with open(json_path, "r") as profile_file:
                            profile_json = json.load(profile_file)
                    except Exception as e:
                        _LOGGER.warning(str(e))

                        # Fall back to json5
                        with open(json_path, "r") as profile_file:
                            profile_json = json5.load(profile_file)

                    Profile.recursive_update(self.json, profile_json)

    def read_path(self, *path_parts: str) -> Path:
        """Get first readable path in user then system directories."""
        for profiles_dir in self.profiles_dirs:
            # Try to find in the user profile first
            full_path = profiles_dir.joinpath(self.name, *path_parts)

            if full_path.exists():
                return full_path

        # Use base dir
        return Path("profiles") / self.name / path_parts[-1]

    def read_paths(self, *path_parts: str) -> typing.List[Path]:
        """Get all readable paths in user then system directories."""
        return_paths: typing.List[Path] = []

        for profiles_dir in self.profiles_dirs:
            # Try to find in the runtime profile first
            full_path = profiles_dir.joinpath(self.name, *path_parts)

            if full_path.exists():
                return_paths.append(full_path)

        return return_paths

    def write_path(self, *path_parts: str) -> Path:
        """Get first writeable path in user then system directories."""
        # Try to find in the runtime profile first
        for profiles_dir in self.profiles_dirs:
            full_path = profiles_dir.joinpath(self.name, *path_parts)

            try:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                return full_path
            except Exception:
                _LOGGER.exception("Unable to write to %s", full_path)

        # Use base dir
        full_path = Path("profiles").joinpath(self.name, *path_parts)
        full_path.parent.mkdir(parents=True, exist_ok=True)

        return full_path

    def write_dir(self, *dir_parts: str) -> Path:
        """Get first writeable directory in user then system directories."""
        # Try to find in the runtime profile first
        for profiles_dir in self.profiles_dirs:
            dir_path = profiles_dir.joinpath(self.name, *dir_parts)

            try:
                dir_path.mkdir(parents=True, exist_ok=True)
                return dir_path
            except Exception:
                _LOGGER.exception("Unable to create %s", dir_path)

        # Use base dir
        dir_path = Path("profiles").joinpath(self.name, *dir_parts)
        dir_path.mkdir(parents=True, exist_ok=True)

        return dir_path

    @classmethod
    def recursive_update(
        cls,
        base_dict: typing.Dict[typing.Any, typing.Any],
        new_dict: typing.Mapping[typing.Any, typing.Any],
    ) -> None:
        """Recursively overwrites values in base dictionary with values from new dictionary"""
        for k, v in new_dict.items():
            if isinstance(v, collections.Mapping) and (k in base_dict):
                Profile.recursive_update(base_dict[k], v)
            else:
                base_dict[k] = v
