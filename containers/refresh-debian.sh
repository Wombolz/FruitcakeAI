#!/bin/sh
# Refresh only Debian OS packages from a reviewed, signed snapshot.
set -eu
snapshot="$1"
case "$snapshot" in
    ????????T??????Z) ;;
    *) echo 'Expected a Debian snapshot timestamp' >&2; exit 1 ;;
esac
sed -i \
    -e "s|http://deb.debian.org/debian$|https://snapshot.debian.org/archive/debian/$snapshot/|" \
    -e "s|http://deb.debian.org/debian-security$|https://snapshot.debian.org/archive/debian-security/$snapshot/|" \
    /etc/apt/sources.list.d/debian.sources
# Keep the pinned upstream PostgreSQL binaries; do not resolve changing PGDG
# releases while patching the OS beneath them.
rm -f /etc/apt/sources.list.d/pgdg.list /etc/apt/sources.list.d/pgdg.sources
apt-get -o Acquire::Check-Valid-Until=false -o APT::Update::Error-Mode=any update
DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Check-Valid-Until=false \
    install -y --no-install-recommends ca-certificates
DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Check-Valid-Until=false \
    upgrade -y --no-install-recommends
apt-get clean
rm -rf /var/lib/apt/lists/*
