# SPDX-License-Identifier: GPL-3.0-or-later
# `sudo make install` gives a root-owned install; `make install PREFIX=$$HOME/.local` a user one.
PREFIX ?= /usr/local
LIBDIR  = $(PREFIX)/lib/loqctl
BINDIR  = $(PREFIX)/bin

install:
	rm -rf "$(DESTDIR)$(LIBDIR)"
	find loqctl -name __pycache__ -prune -o -type f -name '*.py' -print | while read -r f; do \
		install -D -m 644 "$$f" "$(DESTDIR)$(LIBDIR)/$$f" || exit 1; done
	install -D -m 755 loqctl.py "$(DESTDIR)$(LIBDIR)/loqctl.py"
	python3 -m compileall -q -d "$(LIBDIR)" "$(DESTDIR)$(LIBDIR)"
	install -d -m 755 "$(DESTDIR)$(BINDIR)"
	ln -sf "$(LIBDIR)/loqctl.py" "$(DESTDIR)$(BINDIR)/loqctl"

uninstall:
	rm -f "$(DESTDIR)$(BINDIR)/loqctl"
	rm -rf "$(DESTDIR)$(LIBDIR)"

test:
	python3 -m unittest discover -s tests

.PHONY: install uninstall test
