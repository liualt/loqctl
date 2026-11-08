# SPDX-License-Identifier: GPL-3.0-or-later
# `sudo make install` gives a root-owned install; `make install PREFIX=$$HOME/.local` a user one.
PREFIX ?= /usr/local
LIBDIR  = $(PREFIX)/lib/loqctl
BINDIR  = $(PREFIX)/bin
OLDDIR  = $(DESTDIR)$(LIBDIR).old
NEWDIR  = $(DESTDIR)$(LIBDIR).new
SHAREDIR = $(DESTDIR)$(PREFIX)/share
BASHCOMP = $(SHAREDIR)/bash-completion/completions/loqctl
ZSHCOMP  = $(SHAREDIR)/zsh/site-functions/_loqctl
FISHCOMP = $(SHAREDIR)/fish/vendor_completions.d/loqctl.fish

install:
	@case "$(PREFIX)" in /*) ;; *) echo "PREFIX must be an absolute path, got '$(PREFIX)'" >&2; exit 1;; esac
	@if { [ -e "$(DESTDIR)$(BINDIR)/loqctl" ] || [ -L "$(DESTDIR)$(BINDIR)/loqctl" ]; } \
		&& { [ ! -L "$(DESTDIR)$(BINDIR)/loqctl" ] || [ "$$(readlink "$(DESTDIR)$(BINDIR)/loqctl")" != "$(LIBDIR)/loqctl.py" ]; }; then \
		echo "$(DESTDIR)$(BINDIR)/loqctl is not loqctl's symlink, refusing to replace it" >&2; exit 1; fi
	rm -rf "$(NEWDIR)"
	{ find loqctl -name __pycache__ -prune -o -type f -name '*.py' -print | while read -r f; do \
		install -D -m 644 "$$f" "$(NEWDIR)/$$f" || exit 1; done \
	&& install -D -m 755 loqctl.py "$(NEWDIR)/loqctl.py" \
	&& python3 -m compileall -q -d "$(LIBDIR)" "$(NEWDIR)" \
	&& install -d -m 755 "$(DESTDIR)$(BINDIR)"; } || { rm -rf "$(NEWDIR)"; exit 1; }
	rm -rf "$(OLDDIR)"
	@if [ -e "$(DESTDIR)$(LIBDIR)" ]; then mv "$(DESTDIR)$(LIBDIR)" "$(OLDDIR)" || exit 1; fi
	mv "$(NEWDIR)" "$(DESTDIR)$(LIBDIR)" || { [ ! -e "$(OLDDIR)" ] || mv "$(OLDDIR)" "$(DESTDIR)$(LIBDIR)"; exit 1; }
	rm -rf "$(OLDDIR)"
	ln -sf "$(LIBDIR)/loqctl.py" "$(DESTDIR)$(BINDIR)/loqctl"
	install -D -m 644 contrib/completions/loqctl.bash "$(BASHCOMP)"
	install -D -m 644 contrib/completions/_loqctl "$(ZSHCOMP)"
	install -D -m 644 contrib/completions/loqctl.fish "$(FISHCOMP)"

uninstall:
	@case "$(PREFIX)" in /*) ;; *) echo "PREFIX must be an absolute path, got '$(PREFIX)'" >&2; exit 1;; esac
	@if [ -L "$(DESTDIR)$(BINDIR)/loqctl" ] && [ "$$(readlink "$(DESTDIR)$(BINDIR)/loqctl")" = "$(LIBDIR)/loqctl.py" ]; then \
		rm -f "$(DESTDIR)$(BINDIR)/loqctl"; \
	elif [ -e "$(DESTDIR)$(BINDIR)/loqctl" ] || [ -L "$(DESTDIR)$(BINDIR)/loqctl" ]; then \
		echo "$(DESTDIR)$(BINDIR)/loqctl is not loqctl's symlink, leaving it" >&2; \
	fi
	rm -rf "$(DESTDIR)$(LIBDIR)"
	rm -f "$(BASHCOMP)" "$(ZSHCOMP)" "$(FISHCOMP)"

test:
	python3 -m unittest discover -s tests

.PHONY: install uninstall test
