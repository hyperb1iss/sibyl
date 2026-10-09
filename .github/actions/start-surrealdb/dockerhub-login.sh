#!/usr/bin/env bash
# Log in to Docker Hub for authenticated pulls, best effort.
#
# Docker Hub's registry sometimes answers the login ping with a 5xx for a
# few minutes. The login is retried, and if it still fails the job goes on
# with anonymous pulls, which is what it did before credentials existed.
set -uo pipefail

: "${DOCKERHUB_USERNAME:?}" "${DOCKERHUB_TOKEN:?}"
for attempt in 1 2 3 4 5; do
  if printf '%s' "$DOCKERHUB_TOKEN" |
    docker login docker.io --username "$DOCKERHUB_USERNAME" --password-stdin; then
    exit 0
  fi
  echo "Docker Hub login attempt ${attempt} failed; retrying in $((attempt * 10))s"
  sleep $((attempt * 10))
done
echo "::warning::Docker Hub login failed five times; pulling anonymously."
exit 0
