.PHONY: verify test web-build web-dev install clean

install:
	cd apps/bot && pip install -r requirements.txt
	npm install

test:
	pytest

web-build:
	npm --workspace @delfi/web run build

web-dev:
	npm --workspace @delfi/web run dev

verify: test web-build

clean:
	rm -rf apps/web/.next apps/web/node_modules node_modules
	find . -type d -name __pycache__ -not -path "*/node_modules/*" -not -path "*/.venv/*" -exec rm -rf {} +
