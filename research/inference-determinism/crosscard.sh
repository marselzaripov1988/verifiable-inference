#!/bin/bash
# Межкарточный разброс: единственный замер, блокировавший написание протокола.
#
# Порог 0,0378 намерен в фазе 4 на одной 4090. В протоколе порог один на
# всех участников, значит надо знать, насколько расходятся РАЗНЫЕ карты на
# одном и том же тексте. Если сильнее подлога — общий порог невозможен:
# либо он свой для каждого класса железа, либо классы придётся разделять.
#
# Как мерится. Берём эталон 4090 (его тексты и его оценки) и на этой карте
# оцениваем ТЕ ЖЕ тексты. Разница оценок — межкарточный разброс, посчитанный
# ровно тем же способом, что и расхождение подлога, иначе числа несравнимы.
#
# Один прогон даёт обе нужные величины:
#   * scores[0] против эталона      -> межкарточный разброс (без нагрузки);
#   * scores_loaded против scores[0] -> собственный шум этой карты.
#
# Нагрузка с меняющимся составом: при одинаковом составе повторы выходят
# побитово одинаковыми и новых сведений не дают (замерено в фазе 4).
set -uo pipefail

BASE=http://127.0.0.1:8000/v1
MODEL=Qwen/Qwen2.5-7B-Instruct
REF=/root/ref-4090.json
PROMPTS=/root/prompts-large.json
OUT=${OUT:-/root/cross.json}
LOG=/root/serve-cross.log
LOADED="${LOADED:-12}"
N="${N:-128}"

test -s "$REF" || { echo "нет эталона $REF"; exit 1; }
test -s "$PROMPTS" || { echo "нет набора $PROMPTS"; exit 1; }
COUNT=$(python3 -c "import json;print(len(json.load(open('$PROMPTS',encoding='utf-8'))))")
[ "$COUNT" -ge 20 ] || { echo "набор мал ($COUNT)"; exit 1; }

echo "карта: $(nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader)"
echo "запросов: $COUNT"

pkill -9 -f "[Q]wen2.5-7B" 2>/dev/null
pkill -9 -f "VLLM::EngineCore" 2>/dev/null
for i in $(seq 1 20); do
    sleep 3
    nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits \
        | awk '{ if ($1 < 2000) exit 0; else exit 1 }' && break
done

cat > /root/serve-now.sh <<EOF
#!/bin/bash
exec setsid nohup vllm serve $MODEL \\
  --max-model-len 4096 --gpu-memory-utilization 0.92 \\
  --no-enable-prefix-caching --seed 0 --host 127.0.0.1 --port 8000 --dtype bfloat16 \\
  > $LOG 2>&1 < /dev/null
EOF
chmod +x /root/serve-now.sh
nohup /root/serve-now.sh >/dev/null 2>&1 &

for i in $(seq 1 100); do
    sleep 15
    curl -s -m 5 "$BASE/models" 2>/dev/null | grep -q Qwen && {
        echo "сервер готов за $(( i * 15 )) с"; break; }
    [ $(( i % 20 )) -eq 0 ] && echo "ждём $(( i * 15 )) с: $(tail -1 "$LOG" | cut -c1-70)"
done
curl -s -m 5 "$BASE/models" | grep -q Qwen || {
    echo "сервер не поднялся"; tail -5 "$LOG"; exit 1; }

python3 /root/measure.py --base-url "$BASE" --model "$MODEL" \
    --label "$(nvidia-smi --query-gpu=name --format=csv,noheader | tr ' ' '-')" \
    --prompts-file "$PROMPTS" --reference "$REF" --vary-load \
    --batch-neighbours "$N" --loaded-repeats "$LOADED" \
    --out "$OUT" 2>&1 | tail -6

test -s "$OUT" || { echo "файла результата нет"; exit 1; }
python3 - "$OUT" "$REF" <<'PY'
import json, statistics, sys, collections
out, ref = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])

def lp(rec, i=0):
    s = rec.get("scores") or []
    if len(s) <= i: return None
    v = [x for x in s[i]["logprobs"] if x is not None]
    return v or None

def spread(a, b):
    n = min(len(a), len(b))
    return statistics.fmean(abs(a[i]-b[i]) for i in range(n)) if n else None

byp = {r["prompt"]: r for r in ref["prompts"]}
cross = []
for r in out["prompts"]:
    o = byp.get(r["prompt"])
    if not o: continue
    a, b = lp(o), lp(r)
    if a and b:
        m = spread(a, b)
        if m is not None: cross.append(m)

own = [s["mean"] for s in out.get("summary", {}).get("solo_vs_batched", [])]

print()
print("############ ИТОГ ############")
if cross:
    print(f"МЕЖКАРТОЧНЫЙ разброс (эта карта против 4090), запросов {len(cross)}:")
    print(f"  среднее {statistics.fmean(cross):.5f}  медиана {statistics.median(cross):.5f}"
          f"  МАКСИМУМ {max(cross):.5f}")
if own:
    c = collections.Counter(round(x, 8) for x in own)
    print(f"собственный шум карты: наблюдений {len(own)}, разных {len(c)}")
    print(f"  среднее {statistics.fmean(own):.5f}  максимум {max(own):.5f}")
print()
print("для сравнения, фаза 4 на 4090: честное 0,01459 (макс 0,04466),")
print("                              подлог FP8 минимум 0,03778, среднее 0,06139")
if cross:
    mx = max(cross)
    print()
    if mx < 0.03778:
        print(f"ВЫВОД: межкарточный максимум {mx:.5f} НИЖЕ минимума подлога — общий порог возможен")
    else:
        print(f"ВЫВОД: межкарточный максимум {mx:.5f} ДОСТИГАЕТ подлога — общий порог под вопросом")
    print(f"  запросов выше минимума подлога: {sum(1 for x in cross if x >= 0.03778)}/{len(cross)}")
PY
echo
echo "ГОТОВОCROSS"
