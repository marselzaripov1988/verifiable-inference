#!/bin/bash
# Поднять узел, снять 300 векторов и отпечаток выбора ядер.
set -u
NAME="$1"; HOST="$2"; PORT="$3"
SP="/c/Users/x/AppData/Local/Temp/claude/C--Users-x-Desktop-merkabah/e7ab72a3-32d3-4f71-ac90-98a6eca72076/scratchpad"
O="-i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=25 -o ServerAliveInterval=30"
say() { echo "[$NAME] $*"; }
mkdir -p "$SP/cls"

scp $O -P "$PORT" -q "$SP/daemon.tgz" "$SP/texts300.json" "$SP/score_corpus.py" \
    "$SP/fingerprint.sh" "root@$HOST:/root/" || { say "ОШИБКА выкладки"; exit 1; }
# БЕЗ -n: этим потоком и передаётся скрипт настройки. С -n сервер не
# запускается вовсе, а ssh возвращает успех.
ssh $O -p "$PORT" "root@$HOST" 'bash -s' <<'REMOTE' || { say "ОШИБКА распаковки"; exit 1; }
set -e
cd /root && tar xzf daemon.tgz --no-same-owner
cp texts300.json corpus-texts.json
sed -i 's/\r$//' fingerprint.sh && chmod +x fingerprint.sh
cat > /root/serve.sh <<'EOS'
#!/bin/bash
exec vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 127.0.0.1 --port 8000 \
  --dtype bfloat16 --max-model-len 4096 \
  --gpu-memory-utilization 0.92 \
  --no-enable-prefix-caching --seed 0 \
  --no-enable-flashinfer-autotune \
  --compilation-config '{"inductor_compile_config": {"enable_auto_functionalized_v2": false, "combo_kernels": false, "benchmark_combo_kernel": false}}'
EOS
chmod +x /root/serve.sh
setsid nohup /root/serve.sh > /root/vllm.log 2>&1 < /dev/null &
echo пущен
REMOTE

say "жду сервер"
for i in $(seq 1 110); do
  R=$(ssh $O -n -p "$PORT" "root@$HOST" 'curl -s -m 5 http://127.0.0.1:8000/v1/models | head -c 60' 2>/dev/null)
  case "$R" in *Qwen*) say "готов за $((i*20)) с"; break;; esac
  if [ "$i" = 110 ]; then
    say "НЕ ПОДНЯЛСЯ"
    ssh $O -n -p "$PORT" "root@$HOST" 'tail -8 /root/vllm.log' 2>&1 | sed "s/^/[$NAME] /"
    exit 1
  fi
  sleep 20
done

say "снимаю векторы"
ssh $O -n -p "$PORT" "root@$HOST" "cd /root && CFG=$NAME python3 -u score_corpus.py 2>&1 | tail -2" | sed "s/^/[$NAME] /"
ssh $O -n -p "$PORT" "root@$HOST" 'bash /root/fingerprint.sh' > "$SP/cls/fp-$NAME.txt" 2>&1
ssh $O -n -p "$PORT" "root@$HOST" 'nvidia-smi --query-gpu=name,driver_version --format=csv,noheader; python3 -c "import torch,vllm;print(torch.__version__, vllm.__version__)"' > "$SP/cls/env-$NAME.txt" 2>&1
scp $O -P "$PORT" -q "root@$HOST:/root/corpus-vectors-$NAME.json.gz" "$SP/cls/"
say "готово: $(head -1 "$SP/cls/env-$NAME.txt") | конфигураций $(wc -l < "$SP/cls/fp-$NAME.txt")"
