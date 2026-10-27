# Стенд: axm-node v2.2.1 с тремя правками, которые просим у площадки.
# Образ собирает axmd и держит при себе всё, чем поднимается цепочка.
# bookworm, а не bullseye: у bullseye-security пул уже отдаёт 404 на
# пакеты из своего же индекса, и apt-get install падает.
FROM golang:1.23-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        git make gcc jq curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /axm-node
COPY src/ ./

# CGO обязателен: wasmvm 1.5.0 — библиотека на Rust.
RUN CGO_ENABLED=1 go build -trimpath \
        -ldflags "-X github.com/cosmos/cosmos-sdk/version.Name=axmd \
                  -X github.com/cosmos/cosmos-sdk/version.AppName=axmd \
                  -X github.com/cosmos/cosmos-sdk/version.Version=v2.2.1+stargate+burner" \
        -o /usr/local/bin/axmd ./cmd/axmd

# libwasmvm ищется при запуске, а лежит в кеше модулей.
RUN find /go/pkg/mod -name 'libwasmvm.x86_64.so' -exec cp {} /usr/lib/ \; \
    && ldconfig \
    && axmd version --long 2>&1 | head -5

EXPOSE 26657 1317 9090
CMD ["axmd", "start"]
