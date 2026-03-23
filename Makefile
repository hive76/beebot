VERSION := $(shell cat VERSION)

.PHONY: build deploy sync logs restart release test scan scan-vulns scan-secrets

## Build the Docker image, tagged with version and latest
build:
	docker build -t beebot:$(VERSION) -t beebot:latest .

## Deploy (or redeploy) the bot container with the latest image
deploy:
	docker compose up -d --force-recreate beebot

## Run a one-off knowledge base sync
sync:
	docker compose run --rm beebot-sync

## Follow live bot logs
logs:
	docker logs -f beebot

## Restart the bot container (does NOT pull a new image — use deploy for that)
restart:
	docker compose restart beebot

## Run unit tests inside a fresh Docker image
test:
	docker run --rm beebot:latest python -m pytest tests/ -v

## Scan image for CVEs (HIGH/CRITICAL fail the build)
## Requires trivy: https://trivy.dev/docs/getting-started/installation/
scan-vulns:
	trivy image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed beebot:latest

## Scan image and source files for embedded secrets (any finding fails the build)
scan-secrets:
	trivy image --exit-code 1 --scanners secret beebot:latest
	trivy fs   --exit-code 1 --scanners secret --skip-dirs .git .

## Run all security scans
scan: scan-secrets scan-vulns

## Full release pipeline: build → test → scan → deploy
release: build test scan deploy
