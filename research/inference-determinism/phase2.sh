#!/bin/bash
# Фаза 2: три замера, которых не хватало перед написанием протокола.
#
# 1. ЧЕСТНАЯ ГРАНИЦА ПРИ РАБОЧЕЙ НАГРУЗКЕ. В фазе 1 она мерена при 1, 2,
#    8 и 16 соседях, а пропускная способность выходит на полку при 128 —
#    там исполнитель и будет работать, потому что там деньги (65x). Порог
#    ставится прямо по честной границе, то есть измерена она была при одной
#    восьмой реальной загрузки. Вывод «нагрузка меняет частоту, а не
#    величину» проверен до 16 и дальше не проверялся.
#
# 2. МОДЕЛЬ РАБОЧЕГО РАЗМЕРА. Вся фаза 1 прошла на 1,5B, а сервис будет
#    гонять 7B и больше. Глубже сеть — длиннее цепочка накопления ошибки,
#    и сдвинуться могут обе границы.
#
# 3. ВТОРОЙ ВИД ТОНКОГО ПОДЛОГА: квантование KV-кеша. FP8 по весам был
#    единственным тонким подлогом и почти закрыл зазор (1,8x по среднему).
#    KV-кеш в fp8 жулику дешевле и, вероятно, меньше видно — включается
#    одним флагом. Если расходится слабее, настоящий запас меньше нашего.
#
# Карта нужна с аппаратным FP8 (способность >= 8.9), то есть 4090, а не
# 3090: иначе квантование KV-кеша пойдёт запасным путём и замерит выбор
# ядра, а не подлог. Та же ловушка, что с FP8 по весам на A100.
set -uo pipefail

BASE=http://127.0.0.1:8000/v1
MODEL=Qwen/Qwen2.5-7B-Instruct
W=/root/phase2
LEVELS="${LEVELS:-8 32 128}"
REPEATS="${REPEATS:-30}"

log() { echo; echo "############ $* ############"; }

serve() {  # serve <файл-журнала> [доп. флаги]
    local logf=$1; shift
    cat > /root/serve-now.sh <<EOF
#!/bin/bash
exec setsid nohup vllm serve $MODEL \\
  --max-model-len 4096 --gpu-memory-utilization 0.92 \\
  --no-enable-prefix-caching --seed 0 --host 127.0.0.1 --port 8000 $* \\
  > $logf 2>&1 < /dev/null
EOF
    chmod +x /root/serve-now.sh
    nohup /root/serve-now.sh >/dev/null 2>&1 &
    # 7B грузится и компилируется дольше, чем 1,5B: десяти минут не
    # хватило, и прогон сорвался на моём же таймауте. Двадцать пять, и с
    # отметками в журнал, чтобы видеть, идёт ли дело или висит.
    for i in $(seq 1 100); do
        sleep 15
        curl -s -m 5 "$BASE/models" 2>/dev/null | grep -q Qwen && {
            echo "  сервер готов за $(( i * 15 )) с"; return 0; }
        [ $(( i % 20 )) -eq 0 ] && echo "  ждём $(( i * 15 )) с, последнее в журнале: $(tail -1 "$logf" | cut -c1-80)"
    done
    echo "  сервер не поднялся, последние строки:"; tail -5 "$logf"; return 1
}

stop_server() {
    # pkill по шаблону, которого НЕТ в этой командной строке: я уже трижды
    # убивал так собственную оболочку.
    pkill -9 -f "[Q]wen2.5-7B" 2>/dev/null
    for i in $(seq 1 20); do
        sleep 3
        nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits \
            | awk '{ if ($1 < 2000) exit 0; else exit 1 }' && return 0
    done
    echo "  память карты не освободилась: $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
    return 1
}

mkdir -p "$W"
echo "карта: $(nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader)"
CAP=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | tr -d ' .')
if [ "${CAP:-0}" -lt 89 ]; then
    echo "способность ниже 8.9 — аппаратного FP8 нет, замер 3 будет неверным"; exit 1
fi

log "1. честная модель 7B, порождение и граница при $LEVELS соседях"
FIRST=""
for N in $LEVELS; do
    if [ -z "$FIRST" ]; then
        serve /root/serve-bf16.log --dtype bfloat16 || exit 1
        python3 /root/measure.py --base-url "$BASE" --model "$MODEL" \
            --label "bf16-n$N" --batch-neighbours "$N" --loaded-repeats "$REPEATS" \
            --repeats 3 --out "$W/bf16-n$N.json" 2>&1 | tail -4
        FIRST="$W/bf16-n$N.json"
    else
        python3 /root/measure.py --base-url "$BASE" --model "$MODEL" \
            --label "bf16-n$N" --reference "$FIRST" \
            --batch-neighbours "$N" --loaded-repeats "$REPEATS" \
            --out "$W/bf16-n$N.json" 2>&1 | tail -4
    fi
done

log "2. подлог: тот же 7B, но KV-кеш в fp8"
stop_server || exit 1
serve /root/serve-kv8.log --dtype bfloat16 --kv-cache-dtype fp8 || exit 1
grep -oiE "kv_cache_dtype=[a-z0-9]+" /root/serve-kv8.log | head -1
python3 /root/measure.py --base-url "$BASE" --model "$MODEL" \
    --label "kv8-подлог" --reference "$FIRST" \
    --batch-neighbours 0 --out "$W/kv8.json" 2>&1 | tail -4

log "3. сводка"
python3 - "$W" $LEVELS <<'PY'
import json, statistics, sys, os
W = sys.argv[1]; levels = sys.argv[2:]

def load(p):
    with open(p, encoding="utf-8") as f: return json.load(f)

def mean_spread(a, b):
    n = min(len(a), len(b))
    return statistics.fmean(abs(a[i]-b[i]) for i in range(n)) if n else None

print(f"{'соседей':>8} {'расхождений':>13} {'худшее среднее':>15} {'худший max':>11}")
honest_worst = 0.0
for N in levels:
    p = os.path.join(W, f"bf16-n{N}.json")
    if not os.path.exists(p): print(f"{N:>8}  нет файла"); continue
    d = load(p); sb = d.get("summary", {}).get("solo_vs_batched", [])
    if not sb: print(f"{N:>8}  нет данных"); continue
    bad = [s for s in sb if not s.get("identical")]
    wm = max(s["mean"] for s in sb); wx = max(s["max"] for s in sb)
    honest_worst = max(honest_worst, wm)
    print(f"{N:>8} {len(bad):>6}/{len(sb):<6} {wm:>15.5f} {wx:>11.4f}")

# Подлог: сравниваем ОЦЕНКИ одного и того же текста.
ref = load(os.path.join(W, f"bf16-n{levels[0]}.json"))
kvp = os.path.join(W, "kv8.json")
if not os.path.exists(kvp):
    print("\nфайла подлога нет"); raise SystemExit
kv = load(kvp)
byp = {r["prompt"]: r for r in kv["prompts"] if r.get("scores")}
print(f"\n{'запрос':<34} {'ток':>5} {'KV-fp8 сред':>12} {'max':>9}")
fraud_min = None
for r in ref["prompts"]:
    o = byp.get(r["prompt"])
    if not o or not r.get("scores"): continue
    a = [x for x in r["scores"][0]["logprobs"] if x is not None]
    b = [x for x in o["scores"][0]["logprobs"] if x is not None]
    if not a or not b: continue
    n = min(len(a), len(b))
    diff = [abs(a[i]-b[i]) for i in range(n)]
    m = statistics.fmean(diff)
    fraud_min = m if fraud_min is None else min(fraud_min, m)
    print(f"{r['prompt'][:34]:<34} {n:>5} {m:>12.5f} {max(diff):>9.4f}")

print()
print(f"честная граница по среднему (худшая по уровням): {honest_worst:.5f}")
if fraud_min is not None:
    print(f"подлог KV-fp8, минимум по запросам:              {fraud_min:.5f}")
    if honest_worst > 0:
        print(f"ЗАПАС: {fraud_min/honest_worst:.1f}x")
        print()
        print("для сравнения, фаза 1 на 1,5B: честная 0,0407, FP8 по весам 0,0736, запас 1,8x")
PY
