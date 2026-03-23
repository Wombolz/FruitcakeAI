FROM python:3.11-slim

WORKDIR /srv

COPY mcp_shell_server /srv/mcp_shell_server

ENTRYPOINT ["python", "-m", "mcp_shell_server.server"]
