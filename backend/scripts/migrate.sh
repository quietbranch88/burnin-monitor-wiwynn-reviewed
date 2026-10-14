#!/bin/sh
# One-shot deploy step, run by the Compose `migrate` service before the API starts.
# Both commands are safe to rerun: Alembic applies only missing revisions, and the seed
# creates only missing accounts and imports samples only into an empty table.
set -eu
alembic upgrade head
python -m app.seed
