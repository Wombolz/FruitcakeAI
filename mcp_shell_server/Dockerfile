FROM python:3.11.17-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

ARG DEBIAN_SNAPSHOT=20261018T000000Z
COPY containers/refresh-debian.sh /tmp/refresh-debian.sh
RUN sh /tmp/refresh-debian.sh "$DEBIAN_SNAPSHOT" \
    && rm /tmp/refresh-debian.sh \
    && python -m pip uninstall --yes pip setuptools wheel packaging \
    && rm -rf /usr/local/lib/python3.11/ensurepip \
    && python -c 'import importlib.metadata; assert not list(importlib.metadata.distributions())' \
    && command -v timeout

LABEL org.opencontainers.image.title="Fruitcake offline stdlib-only shell" \
      io.fruitcake.debian-snapshot="20261018T000000Z"

WORKDIR /srv

COPY mcp_shell_server /srv/mcp_shell_server

ENTRYPOINT ["python", "-m", "mcp_shell_server.server"]
