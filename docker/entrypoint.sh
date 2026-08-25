#!/bin/sh
set -eu

mkdir -p /app/data /models/huggingface

exec "$@"
