PROJECT=producervault

.PHONY: run setup push

run:
	python3 app.py

setup:
	pip3 install -r requirements.txt
	cp -n .env.example .env || true

push:
	git add .
	git commit -m "$(m)"
	git push origin main
