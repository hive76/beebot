VERSION := $(shell cat VERSION)

.PHONY: build deploy sync logs restart release

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

## Build and deploy in one step
release: build deploy
