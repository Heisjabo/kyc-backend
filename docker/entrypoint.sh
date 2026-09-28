#!/bin/sh
# Start the service.
#
# Migrations are not run unless RUN_MIGRATIONS is true. A deploy that quietly
# changes the shape of a database holding people's identity documents is not
# something to find out about afterwards, so it is opt-in and normally run by hand:
#
#   docker compose exec kyc-api alembic upgrade head
set -e

if [ "${RUN_MIGRATIONS}" = "true" ]; then
  echo "running migrations"
  alembic upgrade head
fi

# One worker by default. Each one loads SCRFD, ArcFace and the liveness mesh into
# its own memory, so a worker costs a few hundred megabytes on a machine already
# running a dozen containers. Raise WEB_CONCURRENCY when the traffic asks for it.
#
# --proxy-headers and --forwarded-allow-ips matter here: the service sits behind
# nginx, and without them every session link and QR would be built with http and
# the container's own host instead of the real one.
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers "${WEB_CONCURRENCY:-1}" \
  --proxy-headers \
  --forwarded-allow-ips '*'
