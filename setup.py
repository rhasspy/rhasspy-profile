"""Setup script for rhasspy-profile package"""
import os
from pathlib import Path

import setuptools

this_dir = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(this_dir, "README.md"), "r") as readme_file:
    long_description = readme_file.read()

with open("requirements.txt", "r") as requirements_file:
    requirements = requirements_file.read().splitlines()

with open("VERSION", "r") as version_file:
    version = version_file.read().strip()

profiles_dir = Path(this_dir) / "rhasspyprofile" / "profiles"
profile_files = [str(f) for f in profiles_dir.glob("**/*")]

setuptools.setup(
    name="rhasspy-profile",
    version=version,
    author="Michael Hansen",
    author_email="hansen.mike@gmail.com",
    url="https://github.com/rhasspy/rhasspy-profile",
    packages=setuptools.find_packages(),
    package_data={"rhasspyprofile": ["py.typed"] + profile_files},
    install_requires=requirements,
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "License :: OSI Approved :: MIT License",
    ],
    long_description=long_description,
    long_description_content_type="text/markdown",
    python_requires=">=3.6",
)
