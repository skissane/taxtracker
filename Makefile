# This project's actual build tool is `just` (see justfile). This Makefile is just a
# forwarding shim so `make <target>` habits keep working, by redirecting to `just <target>`.
ifeq ($(MAKECMDGOALS),)
.DEFAULT_GOAL := default_forward
default_forward: --notice
	@just
.PHONY: default_forward

else

FIRST_TARGET := $(word 1, $(MAKECMDGOALS))
REST_TARGETS := $(wordlist 2, $(words $(MAKECMDGOALS)), $(MAKECMDGOALS))

$(FIRST_TARGET): --notice
	@just $(MAKECMDGOALS)

$(REST_TARGETS):
	@:

.PHONY: $(MAKECMDGOALS)
endif

--notice:
	@echo >&2 "NOTICE: This project uses just, so your command has automatically been redirected to: $(strip just $(MAKECMDGOALS))"
.PHONY: --notice