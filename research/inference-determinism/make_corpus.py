#!/usr/bin/env python3
"""Собрать корпус запросов для замеров детерминизма.

Зачем он понадобился. Вся программа замеров — фаза 1, суточный прогон,
фаза проверяющих, перемер порога — стояла на наборе из **40** запросов.
В 6 000 записанных заданий попало всего 499 различных текстов из 37
запросов, и это обнаружилось, когда отбор «лучших текстов для пробы»
выбрал один и тот же текст, повторённый 55 раз. На сорока запросах любой
отбор — подгонка, а крайние значения (худшее честное, минимум подлога)
опираются на втрое меньше независимых наблюдений, чем кажется.

Почему генератор, а не готовый файл. Корпус — часть измерительного
прибора, как `probe/probe.json`, и его свойства надо не обещать, а
проверять: что запросы РАЗЛИЧНЫ, что длины разложены по полосам, что
набор воспроизводим из этого файла. Генератор это и делает, а на
несовпадении падает.

Чего корпус НЕ даёт. Запросы собраны по шаблонам «тема × задача», значит
они различны, но не независимы по строю: однотипные формулировки могут
вести себя похоже. Это лучше сорока запросов, но не замена живому потоку.
Прежние 40 оставлены целиком, иначе старые замеры стало бы не с чем
сравнивать.

    python3 make_corpus.py            # собрать и проверить
    python3 make_corpus.py --write    # записать prompts-corpus.json
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OLD = os.path.join(HERE, "prompts-large.json")
OUT = os.path.join(HERE, "prompts-corpus.json")

# Темы: по каждой есть и общеизвестное, и узкое, чтобы ответы отличались
# не только формой, но и длиной.
TOPICS_RU = [
    ("фотосинтез", "биология"), ("кровообращение", "биология"),
    ("деление клетки", "биология"), ("иммунный ответ", "биология"),
    ("закон Ома", "физика"), ("сохранение импульса", "физика"),
    ("ядерный распад", "физика"), ("интерференция света", "физика"),
    ("ионная связь", "химия"), ("катализатор", "химия"),
    ("электролиз", "химия"), ("водородный показатель", "химия"),
    ("простое число", "математика"), ("производная", "математика"),
    ("матрица поворота", "математика"), ("теория вероятностей", "математика"),
    ("хеш-таблица", "программирование"), ("рекурсия", "программирование"),
    ("сборка мусора", "программирование"), ("индекс в базе данных",
                                            "программирование"),
    ("инфляция", "экономика"), ("процентная ставка", "экономика"),
    ("спрос и предложение", "экономика"), ("двойная запись", "экономика"),
    ("приливы", "география"), ("тектоника плит", "география"),
    ("муссон", "география"), ("вечная мерзлота", "география"),
    ("падение Константинополя", "история"),
    ("изобретение книгопечатания", "история"),
    ("Великий шёлковый путь", "история"), ("отмена крепостного права",
                                           "история"),
    ("наследование по закону", "право"), ("исковая давность", "право"),
    ("презумпция невиновности", "право"), ("доверенность", "право"),
    ("вакцинация", "медицина"), ("антибиотик", "медицина"),
    ("группа крови", "медицина"), ("анестезия", "медицина"),
]

TOPICS_EN = [
    ("photosynthesis", "biology"), ("blood circulation", "biology"),
    ("mitosis", "biology"), ("the immune response", "biology"),
    ("Ohm's law", "physics"), ("conservation of momentum", "physics"),
    ("nuclear decay", "physics"), ("light interference", "physics"),
    ("ionic bonding", "chemistry"), ("a catalyst", "chemistry"),
    ("electrolysis", "chemistry"), ("the pH scale", "chemistry"),
    ("prime numbers", "mathematics"), ("the derivative", "mathematics"),
    ("a rotation matrix", "mathematics"), ("probability", "mathematics"),
    ("a hash table", "programming"), ("recursion", "programming"),
    ("garbage collection", "programming"), ("a database index",
                                            "programming"),
    ("inflation", "economics"), ("interest rates", "economics"),
    ("supply and demand", "economics"), ("double-entry bookkeeping",
                                         "economics"),
    ("tides", "geography"), ("plate tectonics", "geography"),
    ("monsoons", "geography"), ("permafrost", "geography"),
    ("the fall of Constantinople", "history"),
    ("the printing press", "history"),
    ("the Silk Road", "history"), ("the abolition of serfdom", "history"),
    ("inheritance law", "law"), ("the statute of limitations", "law"),
    ("presumption of innocence", "law"), ("power of attorney", "law"),
    ("vaccination", "medicine"), ("antibiotics", "medicine"),
    ("blood types", "medicine"), ("anaesthesia", "medicine"),
]

# Задачи задают ДЛИНУ ответа: без этого все ответы упираются в лимит
# токенов и длины векторов сбиваются в одну полосу.
TASKS_RU = [
    ("Что такое {t}? Ответь одним предложением.", "короткая"),
    ("Объясни, что такое {t}, в двух предложениях.", "короткая"),
    ("Назови три признака, по которым узнают {t}.", "средняя"),
    ("Опиши по шагам, как работает {t}.", "средняя"),
    ("Чем {t} отличается от смежного понятия той же области? "
     "Дай развёрнутый ответ.", "длинная"),
    ("Объясни {t} так, как объяснил бы человеку без подготовки, "
     "с примером.", "длинная"),
    ("Перечисли пять утверждений о {t} и укажи, какие из них неверны.",
     "длинная"),
]

TASKS_EN = [
    ("What is {t}? Answer in one sentence.", "короткая"),
    ("Explain {t} in two sentences.", "короткая"),
    ("Name three signs by which {t} is recognised.", "средняя"),
    ("Describe step by step how {t} works.", "средняя"),
    ("How does {t} differ from a neighbouring idea in the same field? "
     "Answer at length.", "длинная"),
    ("Explain {t} as you would to someone with no background, "
     "with an example.", "длинная"),
    ("List five statements about {t} and say which of them are false.",
     "длинная"),
]

# Средние запросы: короткое данное плюс задача. Без них полоса длин
# проваливается — остаются одни «что такое X» и одни длинные условия.
MEDIUM = [
    ("ru", "Дано: узел оценивает текст и получает вектор вероятностей. "
           "Два узла на разных картах дали векторы, различающиеся в "
           "последнем знаке.", "Назови три причины такого различия."),
    ("ru", "Дано: залог исполнителя сжигается при обвинении, а "
           "проверяющим платят независимо от исхода.",
           "Объясни, зачем так сделано."),
    ("ru", "Дано: порог отделяет честное расхождение от подлога, и обе "
           "полосы замерены на сорока запросах.",
           "В чём слабость такого замера?"),
    ("ru", "Дано: контракт отсекает задания, в которых ответ короче "
           "тридцати двух токенов.", "Чем обоснована такая отсечка?"),
    ("ru", "Дано: цена газа в сети фиксирована, а расход зависит от "
           "размера загружаемого кода.",
           "Как оценить плату до отправки сделки?"),
    ("ru", "Дано: кошелёк выводит адреса двух сетей из одной сид-фразы, "
           "и обе сети используют один и тот же путь вывода.",
           "Что это означает для владельца?"),
    ("ru", "Дано: веса модели квантованы до четырёх бит, а заявлен "
           "шестнадцатибитный режим.", "Как это обнаружить со стороны?"),
    ("ru", "Дано: проверка узла требует побитового совпадения двух "
           "порождений, а под нагрузкой они совпадают в половине "
           "случаев.", "Почему такая проверка негодна?"),
    ("en", "Given: a node scores a text and gets a vector of "
           "probabilities. Two nodes on different cards produced vectors "
           "differing in the last digit.",
           "Name three causes of such a difference."),
    ("en", "Given: an executor's stake is burned on conviction, while "
           "checkers are paid regardless of the outcome.",
           "Explain why it is arranged this way."),
    ("en", "Given: a threshold separates honest divergence from forgery, "
           "and both bands were measured on forty prompts.",
           "What is weak about such a measurement?"),
    ("en", "Given: the contract rejects jobs whose answer is shorter "
           "than thirty-two tokens.", "What justifies that cutoff?"),
    ("en", "Given: the gas price in the network is fixed, while the "
           "amount spent depends on the size of the uploaded code.",
           "How would you estimate the fee before sending?"),
    ("en", "Given: a wallet derives addresses for two networks from one "
           "seed phrase, and both use the same derivation path.",
           "What does this mean for the owner?"),
    ("en", "Given: model weights are quantised to four bits while a "
           "sixteen-bit mode is declared.",
           "How can this be detected from outside?"),
    ("en", "Given: a node check demands two generations match bitwise, "
           "and under load they match half the time.",
           "Why is such a check unfit?"),
]

BRIEF = {"ru": " Ответь одним предложением.",
         "en": " Answer in one sentence."}

# Длинные запросы: своё условие внутри запроса. Нужны затем, что длина
# ВЕКТОРА это запрос плюс ответ, и без длинных запросов полоса длин
# упирается в лимит токенов ответа.
CONTEXTS = [
    ("ru", "Узел сети получил задание, выполнил его и опубликовал хэш "
     "результата. Другой узел пересчитал тот же результат и получил "
     "числа, отличающиеся в последнем знаке. Третий узел повторил "
     "вычисление и получил числа первого.",
     ["Кто из узлов ошибся и почему на этот вопрос нельзя ответить?",
      "Какие сведения нужны, чтобы различить ошибку и подлог?",
      "Опиши порядок разбора такого спора."]),
    ("ru", "В хранилище лежат три копии файла. Первая и вторая совпадают "
     "побитово, третья отличается одним байтом. Журнал записи потерян, "
     "времена изменения у всех трёх одинаковы.",
     ["Какую копию считать верной и на каком основании?",
      "Что надо было хранить, чтобы такой вопрос не возникал?",
      "Опиши проверку, которая различила бы эти случаи."]),
    ("ru", "Договор предусматривает оплату по факту приёмки. Работа сдана "
     "частями: первая принята, вторая принята с замечаниями, третья "
     "не принята. Заказчик оплатил только первую часть.",
     ["Чем обоснована или не обоснована такая оплата?",
      "Какие последствия наступают для второй части?",
      "Опиши порядок действий исполнителя."]),
    ("en", "A node took a job, ran it and published the hash of the result. "
     "A second node recomputed the same result and got numbers that "
     "differ in the last digit. A third node repeated the computation "
     "and got the first node's numbers.",
     ["Which node is wrong, and why can that not be answered?",
      "What information is needed to tell error from forgery?",
      "Describe how such a dispute should be settled."]),
    ("en", "Three copies of a file sit in storage. The first and second are "
     "bitwise identical, the third differs by one byte. The write log "
     "is lost and all three have the same modification time.",
     ["Which copy should be treated as correct, and on what grounds?",
      "What should have been stored so this question could not arise?",
      "Describe a check that would tell these cases apart."]),
]


def build():
    prompts = []
    with open(OLD, encoding="utf-8") as f:
        prompts += list(json.load(f))

    for topics, tasks in ((TOPICS_RU, TASKS_RU), (TOPICS_EN, TASKS_EN)):
        for topic, _field in topics:
            for form, _size in tasks:
                prompts.append(form.format(t=topic))

    for lang, given, task in MEDIUM:
        prompts.append(f"{given} {task}")
        prompts.append(f"{given} {task}{BRIEF[lang]}")

    for lang, ctx, questions in CONTEXTS:
        for q in questions:
            prompts.append(f"{ctx}\n\n{q}")
            # То же условие с требованием краткости: длина ЗАПРОСА та же,
            # а длина ОТВЕТА другая. Иначе длины векторов сбиваются в
            # одну полосу, и отбор текстов для пробы выбирает один текст.
            prompts.append(f"{ctx}\n\n{q}{BRIEF[lang]}")
    return prompts


def main():
    prompts = build()
    seen, dupes = set(), []
    for p in prompts:
        if p in seen:
            dupes.append(p)
        seen.add(p)
    print(f"запросов собрано: {len(prompts)}, различных: {len(seen)}")
    if dupes:
        print("ПОВТОРЫ (корпус негоден):")
        for p in dupes[:5]:
            print("  -", p[:80])
        return 1

    # Близкие повторы, а не только точные: прежний набор из 40 запросов
    # дал 37 различных начал по 60 символов, и именно это надо видеть.
    for n in (30, 60):
        pref = len({p[:n] for p in prompts})
        print(f"различных начал по {n} символов: {pref} "
              f"({100*pref/len(prompts):.0f}% от всех)")

    lens = sorted(len(p) for p in prompts)
    print(f"длина запроса в символах: от {lens[0]} до {lens[-1]}, "
          f"медиана {lens[len(lens)//2]}")
    bands = {}
    for p in prompts:
        b = min(len(p) // 100 * 100, 400)
        bands[b] = bands.get(b, 0) + 1
    print("по полосам длин:", dict(sorted(bands.items())))
    ru = sum(1 for p in prompts if any("а" <= c <= "я" for c in p.lower()))
    print(f"по-русски {ru}, по-английски {len(prompts) - ru}")

    if "--write" in sys.argv:
        with open(OUT, "w", encoding="utf-8", newline="\n") as f:
            json.dump(prompts, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("записано:", OUT)
    else:
        print("(запись не делалась; --write чтобы записать)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
