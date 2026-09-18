# OpenFile: test locally, then deploy to a DevKit over SSH.

DEVKIT_HOST ?= openhome.local
DEVKIT_USER ?= openhome
DEVKIT_DIR  ?= /home/$(DEVKIT_USER)/openhome_devkit/local_capabilities/openfile
INSTALL_FLAGS ?=
PYTHON ?= python3
SSH = ssh $(DEVKIT_USER)@$(DEVKIT_HOST)
SHIPPED = devkit_functions.py main.py config.json requirements.txt install.sh README.md CHANGELOG.md LICENSE __init__.py bin device systemd samba avahi tests

.PHONY: all lint test validate package release-asset deploy install uninstall stress-test device-test status ports clean

all: lint test validate

lint:
	@$(PYTHON) -m py_compile devkit_functions.py main.py device/web_portal.py tests/*.py
	@bash -n install.sh bin/openfile
	@echo "Syntax OK."

test:
	@$(PYTHON) -m unittest discover -s tests -p "test_*.py"

validate:
	@openhome validate .

# The six files OpenHome accepts for an upload, and nothing else.
package: lint test
	@rm -rf dist/openfile-upload && mkdir -p dist/openfile-upload
	@cp main.py devkit_functions.py requirements.txt config.json README.md dist/openfile-upload/
	@: > dist/openfile-upload/__init__.py
	@echo "Upload these: dist/openfile-upload/"

# The device files release asset for this version, and the SHA-256 to pin in devkit_functions.py.
release-asset:
	@$(PYTHON) tools/release_asset.py $$($(PYTHON) -c "import re;print(re.search(r'^VERSION = \"([^\"]+)\"', open('devkit_functions.py').read(), re.M).group(1))")

deploy: lint test
	@$(SSH) "mkdir -p $(DEVKIT_DIR)"
	@COPYFILE_DISABLE=1 tar --no-xattrs -cf - --exclude __pycache__ $(SHIPPED) 2>/dev/null | $(SSH) "tar -xf - -C $(DEVKIT_DIR)"
	@echo "Copied to $(DEVKIT_HOST):$(DEVKIT_DIR)"

install: deploy
	@$(SSH) -t "sudo $(DEVKIT_DIR)/install.sh $(INSTALL_FLAGS)"

uninstall:
	@$(SSH) -t "sudo $(DEVKIT_DIR)/install.sh --uninstall"

stress-test:
	@$(SSH) "$(PYTHON) $(DEVKIT_DIR)/tests/device_stress_test.py"

device-test:
	@$(SSH) "cd $(DEVKIT_DIR) && $(PYTHON) -m unittest discover -s tests -p 'test_*.py'"

status:
	@$(SSH) "openfile status"

ports:
	@$(SSH) "openfile ports"

clean:
	@rm -rf __pycache__ tests/__pycache__
