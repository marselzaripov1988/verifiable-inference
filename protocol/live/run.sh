#!/bin/bash
# Командный путь демонов против НАСТОЯЩЕЙ модели. Впервые.
#
# До этого проверялись математика, канон и проба — но не сами демоны:
# executor.py и checker.py ни разу не говорили с живым vLLM.
#
# У каждого шага положительный признак.
set -u
cd /root/protocol/daemon
BASE=http://127.0.0.1:8000/v1
MODEL=Qwen/Qwen2.5-7B-Instruct
PROMPT="Опиши по шагам, что происходит при отправке транзакции в блокчейне."

say() { echo; echo "===== $* ====="; }
fail() { echo "ПРОВАЛ: $*" >&2; exit 1; }

say "1. проба на эталонном векторе против живой модели"
# Эталон снят на 4090 13 октября; это ДРУГОЙ 4090. Если проба верна,
# расхождение должно быть почти нулевым — куда ниже предела 25 000.
python3 -u conformance.py --base-url "$BASE" --model "$MODEL" > /root/conf.json 2> /root/conf.err
rc=$?
cat /root/conf.err | tail -3
[ $rc -eq 0 ] || fail "проба не прошла"
python3 - <<'PY' || fail "сообщение пробы не то"
import json
d = json.load(open("/root/conf.json", encoding="utf-8"))["submit_conformance"]
assert set(d) == {"divergence_micro", "logprobs_hash"}, d
assert len(d["logprobs_hash"]) == 64
print(f'  расхождение с эталоном: {d["divergence_micro"]} микродолей (предел 25 000)')
assert d["divergence_micro"] < 25_000, "другой 4090 не прошёл пробу"
print('  поля ровно те, что ждёт контракт')
PY

say "2. демон исполнителя: породить, обязаться, собрать сообщение"
python3 -u executor.py --base-url "$BASE" --model "$MODEL" --job-id 1 \
    --prompt "$PROMPT" --max-tokens 200 --out-vector /root/vec.json \
    > /root/exec.json 2> /root/exec.err
rc=$?
tail -2 /root/exec.err
[ $rc -eq 0 ] || fail "исполнитель не отработал"
python3 - <<'PY' || fail "сообщение исполнителя не то"
import json, sys
sys.path.insert(0, "/root/protocol/daemon")
import canon
m = json.load(open("/root/exec.json", encoding="utf-8"))["submit_answer"]
v = json.load(open("/root/vec.json", encoding="utf-8"))
assert set(m) == {"job_id", "answer_hash", "logprobs_hash", "answer_tokens"}, m
assert m["job_id"] == 1
assert len(m["answer_hash"]) == 64 and len(m["logprobs_hash"]) == 64
assert m["answer_tokens"] >= 32, f'ответ {m["answer_tokens"]} токенов: контракт отклонит'
# Обязательство обязано сходиться с вектором: иначе проверяющий откажется.
assert m["logprobs_hash"] == canon.commit_hash(v["vector"]), "хэш не сходится с вектором"
print(f'  токенов ответа: {m["answer_tokens"]}, элементов вектора: {len(v["vector"])}')
print(f'  обязательство: {m["logprobs_hash"][:16]}… — сходится с вектором по канону')
PY

say "3. демон проверяющего: тот же узел"
# На одной карте в одном процессе расхождение обязано быть нулевым.
python3 - <<'PY' || fail "ответ не восстановлен"
# Исполнитель печатает хэш ответа, а не сам ответ: он уходит заказчику.
# Для проверки нужен текст, поэтому порождаем его тем же клиентом — на
# детерминированном узле это тот же самый ответ.
import json, sys
sys.path.insert(0, "/root/protocol/daemon")
import vllm_client as vllm
a, t = vllm.generate("http://127.0.0.1:8000/v1", "Qwen/Qwen2.5-7B-Instruct",
                     "Опиши по шагам, что происходит при отправке транзакции в блокчейне.",
                     200)
m = json.load(open("/root/exec.json", encoding="utf-8"))["submit_answer"]
import hashlib
same = hashlib.sha256(a.encode()).hexdigest() == m["answer_hash"]
print(f'  ответ повторился побитово: {same}')
assert same, "узел не повторяет сам себя — порог для него недействителен"
open("/root/answer.txt", "w", encoding="utf-8").write(a)
PY
H=$(python3 -c "
import json; print(json.load(open('/root/exec.json',encoding='utf-8'))['submit_answer']['logprobs_hash'])")
python3 -u checker.py --base-url "$BASE" --model "$MODEL" --job-id 1 \
    --prompt "$PROMPT" --answer "$(cat /root/answer.txt)" \
    --producer-vector /root/vec.json --producer-hash "$H" \
    --threshold-micro 36000 > /root/chk.json 2> /root/chk.err
rc=$?
tail -2 /root/chk.err
[ $rc -eq 0 ] || fail "проверяющий не отработал"
python3 - <<'PY' || fail "проверка не та"
import json
m = json.load(open("/root/chk.json", encoding="utf-8"))["submit_check"]
assert set(m) == {"job_id", "divergence_micro", "logprobs_hash"}, m
print(f'  расхождение на своём же узле: {m["divergence_micro"]} (порог 36 000)')
assert m["divergence_micro"] == 0, "один узел обязан совпасть с собой точно"
PY

say "4. проверяющий отказывается от ЧУЖОГО вектора"
python3 - <<'PY'
import json
v = json.load(open("/root/vec.json", encoding="utf-8"))
v["vector"][0] += 50_000
json.dump(v, open("/root/vec-bad.json", "w"))
PY
python3 -u checker.py --base-url "$BASE" --model "$MODEL" --job-id 1 \
    --prompt "$PROMPT" --answer "$(cat /root/answer.txt)" \
    --producer-vector /root/vec-bad.json --producer-hash "$H" \
    --threshold-micro 36000 > /dev/null 2> /root/bad.err
[ $? -ne 0 ] || fail "подменённый вектор был принят"
grep -q "не сходится с обязательством" /root/bad.err || { cat /root/bad.err; fail "отказ не по той причине"; }
echo "  отказ: $(grep ОТКАЗ /root/bad.err | head -1 | cut -c1-70)"

say "5. ЧЕСТНАЯ МЕЖКАРТОЧНАЯ пара через командный путь"
# Вектор исполнителя берётся из записанного замера A100, а оценивает его
# этот 4090. Это и есть честное расхождение между картами, посчитанное
# не оснасткой, а самим демоном проверяющего.
python3 - <<'PY' || fail "эталонная пара не собрана"
import json, sys
sys.path.insert(0, "/root/protocol/daemon")
import canon
d = json.load(open("/root/research/inference-determinism/results/crosscard/a100-vs-4090.json",
                   encoding="utf-8"))
# Берём запись с ответом не короче 32 токенов: короче контракт отклоняет.
best = None
for p in d["prompts"]:
    if not p.get("scores"):
        continue
    sc = p["scores"][0]
    text = "".join(sc["tokens"])
    answer = text[len(p["prompt"]):]
    if len(sc["logprobs"]) >= 80:
        best = (p["prompt"], answer, sc["logprobs"])
        break
assert best, "подходящей записи нет"
prompt, answer, lp = best
vec = canon.to_micro(lp)
json.dump({"job_id": 2, "vector": vec}, open("/root/vec-a100.json", "w"))
open("/root/a100-prompt.txt", "w", encoding="utf-8").write(prompt)
open("/root/a100-answer.txt", "w", encoding="utf-8").write(answer)
open("/root/a100-hash.txt", "w").write(canon.commit_hash(vec))
print(f'  запись A100: {len(vec)} элементов, запрос {prompt[:40]!r}')
PY
python3 -u checker.py --base-url "$BASE" --model "$MODEL" --job-id 2 \
    --prompt "$(cat /root/a100-prompt.txt)" --answer "$(cat /root/a100-answer.txt)" \
    --producer-vector /root/vec-a100.json \
    --producer-hash "$(cat /root/a100-hash.txt)" \
    --threshold-micro 36000 > /root/chk2.json 2> /root/chk2.err
rc=$?
tail -2 /root/chk2.err
[ $rc -eq 0 ] || fail "межкарточная проверка не отработала"
python3 - <<'PY' || fail "межкарточное расхождение не то"
import json
m = json.load(open("/root/chk2.json", encoding="utf-8"))["submit_check"]
d = m["divergence_micro"]
print(f'  расхождение A100 против этого 4090: {d} микродолей')
print(f'  порог 36 000, минимум подлога 37 780, честное среднее 12 700')
assert d < 36_000, "честная межкарточная пара превысила порог"
PY

say "ГОТОВО: демоны отработали против настоящей модели"
