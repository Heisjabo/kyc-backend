#!/bin/sh
# Start the service.
#
# Migrations run before anything is served, unless RUN_MIGRATIONS is set to false.
#
# This used to be the other way round, opt-in, on the reasoning that a deploy should
# not quietly reshape a database holding people's identity documents. That reasoning
# was wrong in one direction and it cost a production outage: a merge deploys, so the
# new code was already answering requests while the column it inserts did not exist
# yet, and every POST /v1/verifications returned 500 while the health check stayed
# green. The schema and the code that needs it have to arrive together, and the only
# place that can be guaranteed is here, before uvicorn binds.
#
# Our migrations are additive. If one ever is not, that is the moment to deploy it
# with RUN_MIGRATIONS=false and run it by hand:
#
#   docker compose exec kyc-api alembic upgrade head
set -e

if [ "${RUN_MIGRATIONS}" = "false" ]; then
  echo "skipping migrations (RUN_MIGRATIONS=false)"
else
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
