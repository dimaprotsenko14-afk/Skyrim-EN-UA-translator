# Skyrim SE/AE Universal English-to-Ukrainian Mod Translator (`Skyrim-EN-UA-translator`)

Універсальний бінарний інструмент з відкритим кодом (GUI та CLI) для автоматизованого та безпечного перекладу модифікацій **The Elder Scrolls V: Skyrim Special Edition / Anniversary Edition (1.6.1170+)** українською мовою за стандартами канонічної термінології **ОПН** (Офіційний переклад назв) та **ТПН** (Традиційний переклад назв).

---

## Ключові можливості (Core Features)

1. **Прямий бінарний парсер та ребілдер плагінів (`.esp` / `.esm` / `.esl`):**
   - Повна підтримка локалізованих (`0x00000080` uint32 StringID) та нелокалізованих (inline UTF-8/CP1252) плагінів Creation Engine.
   - Автоматичне розпакування та повторне стиснення `zlib`-записів (`0x00040000`).
   - Підтримка розширених 32-бітних підзаписів `XXXX` для великих книг (`> 65 535` байтів) із автоматичним перерахунком розмірів вкладених груп `GRUP`.
2. **Crash-Proof Raw `GRUP` Byte-Slice Pass-Through:**
   - Суворий білий список (Whitelist) текстових записів: блоки геометрії, навігаційних сіток, освітлення та морфів облич (`CELL`, `WRLD`, `HDPT`, `RACE`, `NAVM`, `LAND`, `REFR`, `ACHR`, `WTHR`) передаються **байт-у-байт без декомпресії**, що гарантує 100% захист від вильотів (CTD).
3. **Вбудований екстрактор архівів Bethesda `.bsa` (v104 zlib та v105 SSE LZ4):**
   - Автоматичне видобування запакованих таблиць рядків (`Strings/*_english.*strings`) та файлів локалізації інтерфейсу (`Interface/Translations/*_english.txt`) із захистом від Path Traversal (`target_path.resolve().is_relative_to(output_root)`).
4. **Компілятор бінарних таблиць `.strings`, `.dlstrings`, `.ilstrings` та MCM:**
   - Генерація бінарних таблиць локалізації з дедуплікацією зміщень.
   - Переклад файлів SkyUI MCM (`Interface/Translations/*_english.txt` у кодуванні **UTF-16 LE BOM**) та конфігурацій **MCM Helper** (`SKSE/Plugins/MCM/Config/*/config.json`) зі збереженням службових ключів та опціональним збереженням англійських назв модів у списку MCM (`keep_english` / `bilingual` / `translate`).
5. **Інтелектуальний Book Pagebreak Chunker (`BOOK:DESC`):**
   - Безпечне розбиття довгих книг за тегами `[pagebreak]`, `<pagebreak>` та абзацами (`<= 800` символів) через HTTP POST із повним екрануванням тегів рушія (`<Alias=...>`, `<Global=...>`, `<font ...>`, `<mag>`, `<dur>`, `<area>`, `%s`).
6. **3-рівнева гібридна архітектура перекладу (3-Tier Hybrid Pipeline):**
   - **Tier 1 (Exact Translation Memory):** Миттєвий пошук (`< 0.3 мс`) у SQLite + FTS5 базі пам'яті перекладів (`100 184` канонічних пар ОПН/ТПН/SSE-AT) та глосарії власних назв (`16 511` термінів).
   - **Tier 2 (Glossary-Enforced Neural MT):** Багатопотоковий пакетний переклад через Google Translate POST API з інжекцією канонічного глосарію та морфологічним пост-процесором (автоматичне виправлення русизмів і кальок).
   - **Tier 3 (Local LLM Fallback):** Опціональний локальний переклад через **Ollama (`qwen2.5:3b`)** з RAG-інжекцією термінів Всесвіту Сувоїв.
7. **Інтерактивні HTML-словники (`INDEX_DICTIONARY.html`):**
   - Для кожного перекладеного мода автоматично генерується інтерактивний темний HTML-словник із миттєвим пошуком за FormID, EditorID, типом запису, оригіналом та перекладом.

---

## Дотримання авторських прав та джерела (Copyright & Attribution)

Цей проєкт спирається на працю спільноти локалізаторів та розробників інструментів моддингу:

- **Спільнота українських локалізаторів Skyrim SE/AE (автори перекладів ОПН — *Офіційний переклад назв* та ТПН — *Традиційний переклад назв* на Nexus Mods та Steam Workshop):**  https://www.nexusmods.com/skyrimspecialedition/mods/35733
  Першоджерело бази пам'яті перекладів ванільної гри, офіційних доповнень (Dawnguard, Hearthfire, Dragonborn) та плагінів Creation Club (Anniversary Edition), а також канонічного стандарту української термінології Всесвіту Сувоїв (The Elder Scrolls).
- **SSE Auto Translator (SSE-AT):**  
  Еталон структури мапінгу бінарних записів плагінів (`string_records.json`), правил захисту внутрішніх тегів Creation Engine та словникової бази.
- **xTranslator & SSEEdit / xEdit Team:**  
  За відкриту технічну документацію бінарних специфікацій формату плагінів `.esp` / `.esm` / `.esl` та архівів `.bsa` рушія Bethesda Creation Engine.
- **Google Translate API** та **Ollama (`Qwen2.5`):**  
  Нейронні рушії, що використовуються для перекладу нових користувацьких модифікацій із застосуванням глосарію ОПН/ТПН.

> **Правове застереження (Legal Disclaimer & 100% Code-Only Distribution):**  
> У цьому репозиторії **не розповсюджуються** жодні захищені авторським правом бінарні ресурси гри Bethesda Softworks (`.esm`, `.bsa`, `.exe`, `.swf`), готові бази даних перекладу чи сторонні модифікації. Інструмент є повністю відкритим кодом (Code-Only). Користувач самостійно завантажує українську локалізацію (еталон ОПН/ТПН) з Nexus Mods або Steam Workshop, після чого вбудований аналізатор (`core/tm_builder.py` або Крок 1 у GUI) за лічені секунди створює персональну локальну базу пам'яті перекладів `data/skyrim_tm.db` та глосарій `data/glossary_uk.json` на ПК користувача.

---

## Швидкий старт (Quick Start)

### 1. Вимоги та встановлення
Потрібен **Python 3.10+** (Windows / Linux).

```bash
git clone https://github.com/dimaprotsenko14-afk/Skyrin-EN-UA-translator.git
cd Skyrin-EN-UA-translator
pip install requests lz4 tqdm
```

### 2. Крок 1: Одноразова генерація локальної бази зі зразка (Reference Localization)
Завантажте українську локалізацію гри (ОПН або ТПН) з [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition) чи Steam Workshop.

**Через графічний інтерфейс (GUI):**
Запустіть `START_TRANSLATOR_GUI.bat` (або `python skyrim_translator_gui.py`):
1. У блоці **«Крок 1: Ініціалізація бази зі зразка перекладу»**:
   - Оберіть шлях до англійської гри (`Skyrim Special Edition/Data`).
   - Оберіть шлях до завантаженої папки українського перекладу (з файлами `Strings/*` або `.bsa`).
   - Натисніть **«Проаналізувати еталон та створити локальну базу (skyrim_tm.db)»**.
   - Програма автоматично співставить StringID, витягне канонічний глосарій `data/glossary_uk.json` та створить FTS5 SQLite базу.

**Або через командний рядок (CLI):**
```bash
python main.py --build-db --en-source "C:/Games/Skyrim Special Edition/Data" --uk-reference "C:/Downloads/Skyrim_UA_OPN"
```

### 3. Переклад модифікацій (GUI & CLI)
Після створення локальної бази ви можете перекладати будь-які моди:
- **У GUI:** оберіть цільовий `.esp`/`.esm`/`.esl` або папку мода/MO2 у блоці **«Крок 2»** та натисніть **«▶ Start Translation»**.
- **У CLI:**
```bash
# Переклад окремого плагіна або теки мода:
python main.py --target "./mods/MyMod/MyMod.esp" --output "./output/[UA] Skyrim Translation Patch"

# Сканування теки модів Mod Organizer 2:
python main.py --scan-mods "./mods"

# Пакетний переклад усіх хвиль (Wave 1..4) із синхронізацією в MO2:
python main.py --batch-wave all

# Перевірка статусу бази даних та контрольних точок:
python main.py --status
```

---

## Технічна специфікація дозволених та заборонених записів (Whitelist vs Blacklist)

Детальний опис бінарної архітектури наведено у файлі [`TRANSLATION_SPECIFICATION_UA.md`](TRANSLATION_SPECIFICATION_UA.md).

### Дозволені записи (Strict Safe Whitelist)

| Тип запису (`Record`) | Підзаписи (`Subrecords`) | Опис у Creation Engine |
|---|---|---|
| `WEAP` | `FULL`, `DESC` | Назви та описи зброї |
| `ARMO` | `FULL`, `DESC` | Назви та описи броні / одягу |
| `ALCH`, `INGR`, `SCRL`, `SLGM`, `KEYM`, `MISC`, `AMMO` | `FULL`, `DESC` | Зілля, інгредієнти, сувої, камені душ, ключі, предмети, боєприпаси |
| `BOOK` | `FULL`, `DESC`, `CNAM` | Назви, повний текст книг/записок (з `XXXX` та `[pagebreak]`) і опис у інвентарі |
| `SPEL`, `MGEF`, `ENCH` | `FULL`, `DESC`, `DNAM` | Закляття, магічні ефекти та зачарування |
| `PERK` | `FULL`, `DESC`, `EPF2`, `EPFD` | Перки, описи сузір'їв та кнопки активації (`EPFT` у `2, 4, 7`) |
| `AVIF` | `FULL`, `DESC` | Дерева навичок та атрибути персонажа |
| `SHOU`, `WOOP` | `FULL`, `DESC`, `TNAM` | Крики (Ту'ум) та Слова Сили |
| `QUST` | `FULL`, `CNAM`, `NNAM` | Назви квестів, записи журналу та цілі квестів |
| `DIAL`, `INFO` | `FULL`, `NAM1`, `RNAM` | Гілки діалогів, субтитри реплік та короткі запитання |
| `MESG` | `FULL`, `DESC`, `ITXT` | Повідомлення інтерфейсу, спливаючі вікна та кнопки вибору |
| `CONT`, `ACTI`, `FLOR`, `FURN`, `LIGH`, `TACT`, `TREE` | `FULL`, `RNAM` | Контейнери, активатори, флора, меблі, джерела світла та підказки дії |
| `NPC_` | `FULL`, `SHRT` | Імена NPC (автоматично вимикається для косметичних модів та пресетів зовнішності) |
| `GMST` | `DATA` (`s*`) | Текстові налаштування гри (лише записи, де `EDID` починається з літери `s`) |
| `LSCR` | `DESC` | Тексти та підказки завантажувальних екранів |

### Заборонені записи та ресурси (Strict Blacklist — Byte-for-Byte Pass-Through)

| Категорія / Запис | Причина блокування |
|---|---|
| `CELL`, `WRLD`, `LCTN`, `REGN` | Клітинки світу, координати, освітлення та прив'язки локацій у скриптах |
| `NAVM`, `LAND` | Навігаційні сітки (Navmesh) та ландшафт — будь-яка зміна викликає миттєвий CTD |
| `HDPT`, `RACE`, `ARMA` | Частини голови (волосся, очі, брови), расові морфи та моделі броні (RaceMenu / FaceGen) |
| `REFR`, `ACHR`, `PGRE`, `PHZD` | Розміщені у світі референси об'єктів та NPC |
| `WATR`, `WTHR`, `IDLE`, `PACK`, `ANIO`, `STAT`, `SNDR`, `SOUN` | Погода, вода, анімації, пакети ШІ, статика та звукові дескриптори |
| `Scripts/*.pex`, `SKSE/Plugins/*.dll`, `*.swf` | Скомпільовані скрипти Papyrus, C++ бібліотеки SKSE та векторні файли Scaleform ніколи не модифікуються |

---

## Архітектура безпеки (Security & OPSEC Design)

Проєкт розроблено відповідно до стандартів безпечної розробки інструментів обробки бінарних даних:
- **Receive-an-Object, Return-an-Object (RORO):** Типізовані датакласи конфігурації та запитів (`TranslatorConfig`, `ModTranslationRequest`, `ModTranslationResult`).
- **Path Traversal Guard:** Усі операції запису та розпакування `.bsa` проходять перевірку `target_path.resolve().is_relative_to(output_root.resolve())`.
- **Мережева безпека:** Пул з'єднань `requests.Session` із примусовою перевіркою сертифікатів TLS (`verify=True`), жорсткими таймаутами сокетів `(3.0, 8.0)` секунд та шаблоном **Circuit Breaker**.
- **Атомарні контрольні точки та SIGINT:** Перехоплення сигналу `SIGINT` (`Ctrl+C`) гарантує безпечне збереження стану перекладу (`translation_checkpoint.json`) та скидання WAL-буферів SQLite.

---

## Ліцензія (License)

Розповсюджується за ліцензією **MIT License**. Див. файл [`LICENSE`](LICENSE).
