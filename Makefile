# SPDX-License-Identifier: GPL-3.0-or-later
# `sudo make install` gives a root-owned install; `make install PREFIX=$$HOME/.local` a user one.
PREFIX ?= /usr/local
LIBDIR  = $(PREFIX)/lib/loqctl
BINDIR  = $(PREFIX)/bin
NEWDIR  = $(DESTDIR)$(LIBDIR).new

install:
	@case "$(PREFIX)" in /*) ;; *) echo "PREFIX must be an absolute path, got '$(PREFIX)'" >&2; exit 1;; esac
	rm -rf "$(NEWDIR)"
	{ find loqctl -name __pycache__ -prune -o -type f -name '*.py' -print | while read -r f; do \
		install -D -m 644 "$$f" "$(NEWDIR)/$$f" || exit 1; done \
	&& install -D -m 755 loqctl.py "$(NEWDIR)/loqctl.py" \
	&& python3 -m compileall -q -d "$(LIBDIR)" "$(NEWDIR)" \
	&& install -d -m 755 "$(DESTDIR)$(BINDIR)"; } || { rm -rf "$(NEWDIR)"; exit 1; }
	rm -rf "$(DESTDIR)$(LIBDIR)"
	mv "$(NEWDIR)" "$(DESTDIR)$(LIBDIR)"
	ln -sf "$(LIBDIR)/loqctl.py" "$(DESTDIR)$(BINDIR)/loqctl"

uninstall:
	@if [ -L "$(DESTDIR)$(BINDIR)/loqctl" ] && [ "$$(readlink "$(DESTDIR)$(BINDIR)/loqctl")" = "$(LIBDIR)/loqctl.py" ]; then \
		rm -f "$(DESTDIR)$(BINDIR)/loqctl"; \
	elif [ -e "$(DESTDIR)$(BINDIR)/loqctl" ] || [ -L "$(DESTDIR)$(BINDIR)/loqctl" ]; then \
		echo "$(DESTDIR)$(BINDIR)/loqctl is not loqctl's symlink, leaving it" >&2; \
	fi
	rm -rf "$(DESTDIR)$(LIBDIR)"

test:
	python3 -m unittest discover -s tests

.PHONY: install uninstall test
