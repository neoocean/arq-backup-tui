# Convenience targets. The project is stdlib-only, so none of these
# require a virtualenv beyond Python 3.9+.

PYTHON ?= python3

.PHONY: test test-fast test-verbose discover-from-tests-dir clean help

help:
	@echo "Common targets:"
	@echo "  make test          Run the full unittest suite"
	@echo "  make test-fast     Stop at first failure"
	@echo "  make test-verbose  Show every test name as it runs"
	@echo "  make clean         Remove __pycache__ + *.pyc"

test:
	$(PYTHON) -m unittest discover

test-fast:
	$(PYTHON) -m unittest discover --failfast

test-verbose:
	$(PYTHON) -m unittest discover --verbose

# Sanity check: discovery starting from inside tests/ must also work
# (some IDEs and CI setups invoke unittest this way).
discover-from-tests-dir:
	$(PYTHON) -m unittest discover -s tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete
