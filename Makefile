.DEFAULT_GOAL := help
PORT ?= 8731
N ?= 5

.PHONY: help run verify ping deploy logs clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*##' '{printf "  \033[36m%-10s\033[0m%s\n", $$1, $$2}'

run: ## Run the app locally (http://127.0.0.1:$(PORT))
	uv run main.py

verify: ## Check resolution against uv on N random packages (make verify N=10)
	uv run verify.py $(N)

ping: ## Check Ansible can reach the server
	cd ansible && ANSIBLE_HOST_KEY_CHECKING=False ansible -i inventory.ini pysize -m ping

deploy: ## Deploy/update on the server (prompts for sudo password)
	cd ansible && ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventory.ini playbook.yml -K

logs: ## Tail the service logs on the server
	cd ansible && ANSIBLE_HOST_KEY_CHECKING=False ansible -i inventory.ini pysize -m command -a 'journalctl -u pysize -n 50 --no-pager'

clean: ## Remove the local sqlite cache
	rm -f pysize-cache.sqlite*
