PYTHON_NAME = rhasspyprofile
SOURCE = $(PYTHON_NAME)
PYTHON_FILES = $(SOURCE)/*.py *.py
PIP_INSTALL ?= install

.PHONY: reformat check test venv dist

reformat:
	black .
	isort $(PYTHON_FILES)

check:
	flake8 $(PYTHON_FILES)
	pylint $(PYTHON_FILES)
	mypy $(PYTHON_FILES)
	black --check .
	isort --check-only $(PYTHON_FILES)
	yamllint .
	pip list --outdated

venv:
	scripts/create-venv.sh

dist:
	python3 setup.py sdist
