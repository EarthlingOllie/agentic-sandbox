#!/bin/sh
# Init hook: runs inside the set's (shared) mysql once it is healthy, on every `up`,
# so it must be idempotent.
set -eu
mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS web_app"
