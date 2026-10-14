#!/bin/bash
# Фаза 4: сверка вероятностей на сорока запросах вместо четырёх.
#
# Зачем. Фаза 2 закрыла сверку вероятностей целиком: подлог FP8 по весам
# расходился на 0,0326, честные узлы между собой — на 0,0385. Жулик
# отличается от эталона МЕНЬШЕ честного, порога не существует. Вывод
# сильный, и на нём стоит выбор способа проверки.
#
# Но мерен он на ЧЕТЫРЁХ встроенных запросах, и два из них фаза 3б уже
# показала нетипичными в разные стороны: «capital of France» не даёт
# сигнала вовсе (подлог 0%), «reverses a list» даёт идеальное разделение
# (0% против 100%). То есть половина выборки — крайние случаи, и на
# сорока запросах величины могут разойтись в любую сторону.
#
# Здесь то же самое на prompts-large.json. Три исхода, и все полезны:
#   * инверсия подтвердилась  -> вывод фазы 2 верен, сверка вероятностей
#     мертва окончательно, и мы это знаем не с четырёх запросов;
#   * инверсия исчезла        -> фаза 2 ошибалась на малой выборке,
#     сверка вероятностей жива, и это меняет форму протокола;
#   * запас появился, но узкий -> как в фазе 1, решать по цене.
#
# Соседей берём только 128: это рабочий уровень, где исполнитель и будет
# считать. Повторов на запрос меньше, чем в фазе 2 (6 против 30) —
# при сорока запросах разнообразие запросов важнее, чем повторы одного,
# а общее число наблюдений под нагрузкой всё равно выше: 240 против 120.
set -uo pipefail

BASE=http://127.0.0.1:8000/v1
MODEL=Qwen/Qwen2.5-7B-Instruct
W=/root/phase4
PROMPTS=/root/prompts-large.json
N="${N:-128}"
LOADED="${LOADED:-6}"
REPEATS="${REPEATS:-3}"

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
    for i in $(seq 1 100); do
        sleep 15
        curl -s -m 5 "$BASE/models" 2>/dev/null | grep -q Qwen && {
            echo "  сервер готов за $(( i * 15 )) с"; return 0; }
        [ $(( i % 20 )) -eq 0 ] && echo "  ждём $(( i * 15 )) с, последнее в журнале: $(tail -1 "$logf" | cut -c1-80)"
    done
    echo "  сервер не поднялся, последние строки:"; tail -5 "$logf"; return 1
}

stop_server() {
    # Шаблон, которого НЕТ в этой командной строке, и отдельно потомок
    # VLLM::EngineCore: он переименовывает себе cmdline и по имени модели
    # не находится, а карту держит.
    pkill -9 -f "[Q]wen2.5-7B" 2>/dev/null
    pkill -9 -f "VLLM::EngineCore" 2>/dev/null
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
    echo "способность ниже 8.9 — аппаратного FP8 нет, подлог по весам замерится неверно"; exit 1
fi

# Набор обязан быть на месте и быть большим: молча свалиться на встроенные
# четыре запроса — ровно та ошибка, которую этот прогон и проверяет.
test -s "$PROMPTS" || { echo "нет файла запросов $PROMPTS"; exit 1; }
COUNT=$(python3 -c "import json;print(len(json.load(open('$PROMPTS',encoding='utf-8'))))")
echo "запросов в наборе: $COUNT"
[ "$COUNT" -ge 20 ] || { echo "набор мал ($COUNT) — прогон не имеет смысла"; exit 1; }

log "1. честный 7B: повторяемость и граница при $N соседях, $COUNT запросов"
serve /root/serve-bf16.log --dtype bfloat16 || exit 1
python3 /root/measure.py --base-url "$BASE" --model "$MODEL" \
    --label "bf16-n$N" --prompts-file "$PROMPTS" \
    --batch-neighbours "$N" --loaded-repeats "$LOADED" \
    --repeats "$REPEATS" --out "$W/bf16.json" 2>&1 | tail -6
test -s "$W/bf16.json" || { echo "честный прогон не дал файла — дальше бессмысленно"; exit 1; }

log "2. подлог: FP8 по ВЕСАМ (тот, что перевернул вывод в фазе 2)"
stop_server || exit 1
serve /root/serve-w8.log --quantization fp8 || exit 1
grep -oiE "quantization=[a-z0-9]+" /root/serve-w8.log | head -1
python3 /root/measure.py --base-url "$BASE" --model "$MODEL" \
    --label "w8" --prompts-file "$PROMPTS" --reference "$W/bf16.json" \
    --batch-neighbours 0 --out "$W/w8.json" 2>&1 | tail -6

log "3. подлог: KV-кеш в fp8"
stop_server || exit 1
serve /root/serve-kv8.log --dtype bfloat16 --kv-cache-dtype fp8 || exit 1
grep -oiE "kv_cache_dtype=[a-z0-9]+" /root/serve-kv8.log | head -1
python3 /root/measure.py --base-url "$BASE" --model "$MODEL" \
    --label "kv8" --prompts-file "$PROMPTS" --reference "$W/bf16.json" \
    --batch-neighbours 0 --out "$W/kv8.json" 2>&1 | tail -6

log "4. сводка: держится ли инверсия фазы 2"
python3 - "$W" <<'PY'
import json, os, statistics, sys
W = sys.argv[1]

def load(p):
    with open(p, encoding="utf-8") as f: return json.load(f)

def lp(rec, i=0):
    s = rec.get("scores") or []
    if len(s) <= i: return None
    v = [x for x in s[i]["logprobs"] if x is not None]
    return v or None

def spread(a, b):
    n = min(len(a), len(b))
    return statistics.fmean(abs(a[i]-b[i]) for i in range(n)) if n else None

ref = load(os.path.join(W, "bf16.json"))

# Честная граница: оценка того же текста в одиночку против оценки в пачке.
honest = []
for s in ref.get("summary", {}).get("solo_vs_batched", []):
    honest.append(s["mean"])
if honest:
    print(f"честных наблюдений под нагрузкой: {len(honest)}")
    print(f"  честное расхождение: среднее {statistics.fmean(honest):.5f}, "
          f"медиана {statistics.median(honest):.5f}, худшее {max(honest):.5f}")

# Подлог: оценка ЧУЖОГО текста своей моделью против оценки его автором.
by_prompt = {r["prompt"]: r for r in ref["prompts"]}
for name, label in (("w8.json", "FP8 по весам"), ("kv8.json", "KV-кеш fp8")):
    p = os.path.join(W, name)
    if not os.path.exists(p):
        print(f"\n{label}: файла нет"); continue
    d = load(p)
    vals = []
    for r in d["prompts"]:
        o = by_prompt.get(r["prompt"])
        if o is None: continue
        a, b = lp(o), lp(r)
        if not a or not b: continue
        m = spread(a, b)
        if m is not None: vals.append(m)
    if not vals:
        print(f"\n{label}: нечего сравнить"); continue
    print(f"\n{label}: запросов {len(vals)}")
    print(f"  расхождение подлога: среднее {statistics.fmean(vals):.5f}, "
          f"медиана {statistics.median(vals):.5f}, минимум {min(vals):.5f}")
    if honest:
        h = statistics.fmean(honest)
        print(f"  ЗАПАС по средним: {statistics.fmean(vals)/h:.2f}x"
              f"   (фаза 2 на 4 запросах: {'0.85x — инверсия' if 'FP8' in label else '8.05x'})")
        worse = sum(1 for v in vals if v > max(honest))
        print(f"  запросов, где подлог превысил ХУДШЕГО честного: {worse}/{len(vals)}")
print()
print("фаза 2 на четырёх запросах: честная 0,0385, FP8 по весам 0,0326 (инверсия), KV-fp8 запас 8,05x")
PY
echo
echo "ГОТОВО"
