FROM golang:1.26.9-bookworm@sha256:d9c68c2c51161e12fd77e4c6320687c9cd86e1af1e3ad6e6cd63ff970641453c AS gosu-builder

WORKDIR /src
ADD https://codeload.github.com/tianon/gosu/tar.gz/6456aaa0f3c854d199d0f037f068eb97515b7513 /tmp/gosu.tar.gz
RUN echo '33d7537d588ea49458b9509bcf4554bdf5ceacc66da71e5caa1058ea3b689c3b  /tmp/gosu.tar.gz' | sha256sum -c - \
    && tar -xzf /tmp/gosu.tar.gz --strip-components=1 \
    && go mod download \
    && go mod verify \
    && CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags='-s -w' -o /out/gosu .

FROM pgvector/pgvector:0.8.7-pg16-bookworm@sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a

ARG DEBIAN_SNAPSHOT=20261018T000000Z
# The upstream database image has no CA bundle. Bootstrap HTTPS trust from the
# pinned builder, then install Debian's managed certificate package below.
COPY --from=gosu-builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY containers/refresh-debian.sh /tmp/refresh-debian.sh
RUN sh /tmp/refresh-debian.sh "$DEBIAN_SNAPSHOT" \
    && rm /tmp/refresh-debian.sh /usr/local/bin/gosu
COPY --from=gosu-builder --chmod=0755 /out/gosu /usr/local/bin/gosu
RUN gosu --version \
    && gosu nobody id \
    && test "$(postgres --version)" = 'postgres (PostgreSQL) 16.15 (Debian 16.15-1.pgdg12+2)'

LABEL org.opencontainers.image.title="Fruitcake PostgreSQL 16 / pgvector 0.8.7" \
      io.fruitcake.debian-snapshot="20261018T000000Z" \
      io.fruitcake.gosu-source="6456aaa0f3c854d199d0f037f068eb97515b7513"
