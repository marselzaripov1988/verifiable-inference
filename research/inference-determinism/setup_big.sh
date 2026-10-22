#!/bin/bash
# Поднять узел с выключенным выбором ядра по замеру и снять 6 000
# векторов плюс 34 текста пробы.
set -u
NAME="$1"; HOST="$2"; PORT="$3"
SP="/c/Users/x/AppData/Local/Temp/claude/C--Users-x-Desktop-merkabah/e7ab72a3-32d3-4f71-ac90-98a6eca72076/scratchpad"
K=~/.ssh/id_ed25519
O="-i $K -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=25 -o ServerAliveInterval=30"
say() { echo "[$NAME] $*"; }

say "выкладываю"
scp $O -P "$PORT" -q "$SP/daemon.tgz" "$SP/texts6k.json" "$SP/bigscore.py" \
    "root@$HOST:/root/" || { say "ОШИБКА выкладки"; exit 1; }

ssh $O -p "$PORT" "root@$HOST" 'bash -s' <<'REMOTE' || { say "ОШИБКА распаковки"; exit 1; }
set -e
cd /root
tar xzf daemon.tgz --no-same-owner
# Закреплённые параметры + отказ от выбора ядра ПО ЗАМЕРУ. Без последнего
# две пересборки на одной карте дают 0 побитовых совпадений из 200.
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
echo "сервер пущен"
REMOTE

say "жду сервер"
for i in $(seq 1 120); do
  R=$(ssh $O -p "$PORT" "root@$HOST" \
      'curl -s -m 5 http://127.0.0.1:8000/v1/models | head -c 60' 2>/dev/null)
  case "$R" in *Qwen*) say "готов за $((i*20)) с"; break;; esac
  if [ "$i" = 120 ]; then
    say "СЕРВЕР НЕ ПОДНЯЛСЯ:"
    ssh $O -p "$PORT" "root@$HOST" 'tail -12 /root/vllm.log' 2>&1 | sed "s/^/[$NAME] /"
    exit 1
  fi
  sleep 20
done

ssh $O -p "$PORT" "root@$HOST" \
  "grep -oE \"combo_kernels.: [A-Za-z]+|benchmark_combo_kernel.: [A-Za-z]+|enable_flashinfer_autotune=[A-Za-z]+\" /root/vllm.log | sort -u" \
  | sed "s/^/[$NAME] флаг: /"

say "снимаю векторы (6 000 + 34)"
ssh $O -p "$PORT" "root@$HOST" "cd /root && CFG=$NAME python3 -u bigscore.py 2>&1 | tail -6" \
  | sed "s/^/[$NAME] /"
mkdir -p "$SP/big"
scp $O -P "$PORT" -q "root@$HOST:/root/vectors-$NAME.json.gz" "$SP/big/" \
  && say "забрано: $(ls -la "$SP/big/vectors-$NAME.json.gz" | awk '{print $5}') байт"
