# Прокси агента одним образом: OpenAI API, оплата кошельком агента в AXM.
#
# Внутри только Python и код протокола. Ни axmd, ни libwasmvm, ни связки:
# сделки подписывает сам прокси (protocol/daemon/wallet.py), и агенту
# хватает файла с ключом.
#
# Собирается из корня репозитория:
#   docker build -f protocol/agent/Dockerfile -t vi-agent .
#
# База прибита по сумме: образ держит ключ от кошелька, и молча сменившаяся
# база была бы чужим кодом рядом с деньгами.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

WORKDIR /app
# Ровно то, что прокси импортирует: демоны протокола, раздача текстов из
# бота и сам прокси. Ни контракта, ни образа узла.
COPY protocol/daemon/*.py protocol/daemon/
COPY protocol/telegram/bot.py protocol/telegram/
COPY protocol/agent/*.py protocol/agent/

# Проверки — при сборке: образ, который собрался, но подписывает не так,
# как цепочка, не нужен.
RUN python3 protocol/daemon/test_wallet.py >/dev/null \
 && python3 protocol/agent/test_proxy.py >/dev/null \
 && find /app -name __pycache__ -prune -exec rm -rf {} +

# В контейнере API слушает все адреса, поэтому прокси потребует API_TOKEN:
# порт контейнера можно случайно открыть наружу. Ключ — файлом, только на
# чтение; база и тексты — в /data.
ENV PYTHONUNBUFFERED=1 \
    SIGNER=python \
    SIGN_KEY=/key/agent.key \
    DB=/data/agent.sqlite \
    PROMPTS_DIR=/data/prompts \
    API_HOST=0.0.0.0 \
    API_PORT=8088 \
    PROMPT_PORT=8090
VOLUME /data
EXPOSE 8088 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8088/healthz', timeout=4)" || exit 1

ENTRYPOINT ["python3", "-u", "protocol/agent/proxy.py"]
