# Узел протокола одним образом: vLLM, кеш ядер, демоны, axmd.
#
# Что здесь ЕСТЬ и чего нарочно НЕТ.
#
# Есть кеш ядер всех трёх классов — 2,4 МБ архивом каждый, на фоне
# базового образа (10,1 ГБ сжатым) это ничто. Кеш обязан стоять ДО
# первого запуска сервера: vLLM выбирает ядра Triton замером скорости на
# машине, и при другом выборе векторы расходятся, а вердикт выносится
# равенством хэшей. Поэтому он в образе, а не в инструкции.
#
# Весов модели здесь НЕТ, и это замер, а не лень: 29 октября они пришли с
# HuggingFace за 1–3 минуты на всех трёх арендованных картах. Запечь их —
# утроить образ (плюс 15 ГБ) и платить за свой трафик ради того, что и
# так быстро.
#
# axmd берётся ОФИЦИАЛЬНЫМ выпуском Axiome и сверяется по sha256: иначе
# оператор доверял бы моей сборке узла цепочки.
FROM vllm/vllm-openai:v0.31.0

ARG AXMD_URL=https://github.com/axiome-pro/axm-node/releases/download/v2.2.1/axmd-linux-amd64-2.2.1
# Суммы подставляются при сборке. Пустая означает «не сверять», и сборка
# на этом ОТКАЗЫВАЕТСЯ: молча поставить непроверенный бинарь, которым
# подписываются сделки с залогом, нельзя.
ARG AXMD_SHA256=""
# axmd не статический: без libwasmvm он не стартует вовсе —
# «error while loading shared libraries». Берётся официальным выпуском
# CosmWasm той версии, что у цепочки (1.5.0), и сверяется по их же
# checksums.txt.
ARG WASMVM_URL=https://github.com/CosmWasm/wasmvm/releases/download/v1.5.0/libwasmvm.x86_64.so
ARG WASMVM_SHA256=372c4fe957549939d1b2f6368628e38c9abfa85b9b93cfc5eeebbeaa3a2ababf

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN set -eu; \
    [ -n "$AXMD_SHA256" ] || { echo "ОТКАЗ: нужен --build-arg AXMD_SHA256=<сумма выпуска>"; exit 1; }; \
    curl -fsSL "$WASMVM_URL" -o /usr/local/lib/libwasmvm.x86_64.so; \
    echo "$WASMVM_SHA256  /usr/local/lib/libwasmvm.x86_64.so" | sha256sum -c -; \
    ldconfig; \
    curl -fsSL "$AXMD_URL" -o /usr/local/bin/axmd; \
    echo "$AXMD_SHA256  /usr/local/bin/axmd" | sha256sum -c -; \
    chmod +x /usr/local/bin/axmd; \
    axmd version

# Кеш ядер по классам и эталоны пробы. Распаковывает не сборка, а запуск:
# класс выбирается по карте, которой при сборке ещё нет.
COPY daemon/kernel-cache/sm_89/kernel-cache.tar.gz   /opt/node/cache/sm_89.tar.gz
COPY daemon/kernel-cache/sm_86/kernel-cache.tar.gz   /opt/node/cache/sm_86.tar.gz
COPY daemon/kernel-cache/sm_120/kernel-cache.tar.gz  /opt/node/cache/sm_120.tar.gz
COPY daemon/kernel-cache/sm_89/SHA256SUMS            /opt/node/cache/sm_89.sha256
COPY daemon/kernel-cache/sm_86/SHA256SUMS            /opt/node/cache/sm_86.sha256
COPY daemon/kernel-cache/sm_120/SHA256SUMS           /opt/node/cache/sm_120.sha256

COPY daemon/*.py        /opt/node/daemon/
COPY daemon/probe/      /opt/node/daemon/probe/
COPY node-image/start.sh /opt/node/start.sh

RUN set -eu; \
    rm -f /opt/node/daemon/test_*.py; \
    chmod +x /opt/node/start.sh; \
    python3 -c "import sys; sys.path.insert(0,'/opt/node/daemon'); import loop, client, chain, conformance" \
      || { echo "ОТКАЗ: демоны не импортируются"; exit 1; }

# Связка модель/рантайм/dtype прибита настройкой контракта; здесь только
# то, что нужно запуску. Адрес реестра и класс не прибиваются: один образ
# на все три класса.
ENV NODE_HOME=/root \
    VLLM_PORT=8000 \
    BUNDLE_PORT=8081 \
    PYTHONUNBUFFERED=1

EXPOSE 8081
ENTRYPOINT ["/opt/node/start.sh"]
