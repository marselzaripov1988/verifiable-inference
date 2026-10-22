#!/bin/bash
# Отпечаток ВЫБОРА ядер, а не байтов кеша. Признак найден опытом
# 22 октября: различающиеся файлы кеша совпадению не мешают, а вот
# выбранная конфигурация решает всё. Поле time_taken_ms выбрасывается:
# оно различается у совпадающих карт (31 против 30) и ничего не значит.
cd /root/.cache/vllm 2>/dev/null || { echo "КЕША НЕТ"; exit 0; }
find . -name '*.best_config' | sort | while read -r f; do
  printf '%s %s\n' "$f" "$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
d.pop('time_taken_ms', None)
print(json.dumps(d, sort_keys=True))
" "$f")"
done
