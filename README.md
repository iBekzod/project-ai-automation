# Xonsaroy AI PM Bot

Telegram boti — **Xonsaroy** Laravel backend uchun avtomatik xatolik tahlili,
tashxis qo'yish va tuzatish quvuri (pipeline). Guruhdagi xodimlar
shikoyatlarini va shaxsiy DM'larni o'qiydi, **Claude Code CLI** orqali
tahlil qiladi va kerak bo'lsa kodga tuzatish kiritib, PR ochib stage'ga
merge qiladi.

> Backend repozitoriya: `iBekzod/xonsaroy` (yoki o'zgartirilishi mumkin).
> Bot host: Windows / Linux. Claude CLI: Max plan auth tavsiya qilinadi.

---

## ✨ Asosiy imkoniyatlar

### 🔍 Ikki bosqichli xabar tahlili
- **1-bosqich (klassifikator):** xabar bizning loyihaga aloqasi bormi?
  `OUR_PROBLEM` / `OUT_OF_SCOPE` / `CHAT` deb tasniflanadi (~10–15 s).
- **2-bosqich (yechuvchi):** Claude CLI repozitoriyaning to'liq kontekstini
  (CLAUDE.md, `.ruflo/agents/*.yaml`, 107 ta modul) o'qib, `file:line`
  darajasida tashxis qaytaradi (~3–10 daqiqa).
- Tashxis kategoriyalari: `backend_bug`, `frontend_bug`, `infra_issue`,
  `user_error`, `unclear` — faqat `backend_bug` avtomatik tuzatish quvuriga
  tushadi.

### 💬 Multi-session chat (claude.ai uslubida)
- Bir vaqtning o'zida **bir nechta chat** parallel ishlaydi.
- Har bir chat o'z `session_id`'siga ega — Claude `--resume` bilan kontekst
  saqlanadi.
- `/chatlist` orqali chatlar o'rtasida bir bosishda o'tish.
- `chats.json`'da saqlanadi — bot qayta ishga tushganda ham yo'qolmaydi.
- **Multi-developer:** har bir dasturchining o'z chatlari alohida saqlanadi
  (user_id orqali izolyatsiya), tashxis kartalari esa barcha dasturchilarga
  yuboriladi — kim birinchi tugmani bossa, o'sha amalga oshiradi.

### 🧠 Avtomatik intent klassifikator
DM yozsangiz bot avtomatik tushunadi:
- **ASK** — savol-javob (tezkor, kodga tegmaydi)
- **TASK** — tuzatish so'rovi (tasdiqlash so'raydi)
- **CHAT** — aktiv chatda davom (multi-turn)

Maxsus prefikslar:
- `!xabar` — TASK sifatida majburlash
- `?xabar` — ASK sifatida majburlash

### 🖼 Screenshot tahlili
Telegram orqali yuborilgan rasmlar avtomatik yuklab olinadi va Claude'ning
**Read** tooli orqali o'qiladi. Xato ekrani yuborilsa odatda `backend_bug` deb
tasniflanadi.

### 🚀 Deploy va rollback
- `/push stage|dev|prod` — branch'ga merge va push
- `/rollback [issue_id]` — bot tomonidan qilingan commitni `git revert`
- Har bir destruktiv operatsiya **inline tasdiqlash** so'raydi
- `DRY_RUN=true` — push/PR/merge'larni mahalliy darajada qoldiradi

### 🎛 Telegram tugmali interfeys
- 5×3 doimiy reply keyboard (Uzbek + emoji yorliqlar):

  ```
  📊 Holat       💬 Chatlar      📖 Yordam
  ✅ Qabul       🔄 Qayta        ⏭ O'tkazib
  🚀 Stage       🚀 Dev          🚀 Prod
  📤 Chiqarish   ↩️ Qaytarish    ⏹ To'xtatish
  ⏸ Hammasini    ⚙ Menyu         🩺 Ping
  ```

- Inline tugmalar tashxis kartasi va tasdiqlash uchun
- `/status` — bir qarashda barcha holat (rejim, chatlar, ochiq muammolar,
  branchlar)

### 🛡 Xavfsizlik qatlamlari
| Qatlam | Vazifa |
|---|---|
| `--disallowedTools Edit,Write,...` | Chat rejimida fayl o'zgartirish bloklangan |
| `DRY_RUN` | Push / PR / merge'larni o'tkazib yuboradi |
| `ensure_clean_worktree()` | Git operatsiyalarini ifloslangan worktree'dan himoya qiladi |
| `TELEGRAM_DEVELOPER_IDS` | Faqat ro'yxatdagi dasturchilar buyruqlarni ishlata oladi (bir nechta dasturchi qo'llab-quvvatlanadi) |
| Inline confirmation | Har bir destruktiv operatsiya uchun |
| `MAX_PARALLEL_CLAUDE` | Bir vaqtda 5 ta Claude'dan ortiq ishlamaydi |
| Retry isolation | Har bir dasturchining `Qayta` so'rovi faqat o'zining DM'idan kelgan izohga javob beradi |

### 📊 Monitoring va loglar
- **`bot.log`** — fayl ichidagi loglar (5 MB rotation, 3 backup).
- **`/status`** — real vaqt holati.
- **GUI Logs tab** — jonli oqim.
- **`bot.db`** (SQLite) — strukturali audit jurnali (`actions` jadvali) + ochiq muammolar + chat sessiyalari + pending tasklar bot qayta ishga tushganda yo'qolmaydi. WAL mode + qisqa muddatli connectionlar — concurrent_updates bilan to'qnashmaydi.

### ♻️ Auto-update (ixtiyoriy — default o'chirilgan)
Botni `git clone` qilib o'rnatganda hech qachon o'z-o'zidan GitHub'ga
murojaat qilmaydi — auto-update faqat yoqilganda ishlaydi.

**Qo'lda buyruqlar (har doim mavjud):**
- `/version` — mahalliy va GitHub'dagi commit'larni ko'rsatadi
- `/update` — git pull + bot qayta ishga tushish (hozir tahlil
  ishlamayotgan bo'lsa)

**Periodik tekshirishni yoqish (ixtiyoriy):**
- `/autoupdate on` — har 6 soatda tekshirib, yangi versiya bo'lsa DM
  yuboradi
- `/autoupdate hours 12` — boshqa interval (soatda)
- `/autoupdate apply on` — yangi versiya topilsa avtomatik pull + restart
- `/autoupdate off` — periodik va avto-apply'ni to'liq o'chirish
- `/autoupdate status` — joriy holatni ko'rsatish

Sozlamalar `bot.db` ning `settings` jadvalida:
- `update_repo_api` — GitHub API URL (default: `https://api.github.com/repos/iBekzod/project-ai-automation`)
- `update_branch` — kuzatiladigan branch (default: `main`)
- `update_check_hours` — tekshirish oraligi (default: `0` — o'chirilgan)
- `update_auto_apply` — `true` bo'lsa avto-pull + restart (default: `false`)

### 🖥 Desktop GUI (zamonaviy)
Win11-style **Sun Valley** mavzusi bilan Tkinter ilova:
- **📊 Dashboard** — bot holati, rejim/repo/guruhlar kartalari, to'liq konfiguratsiya
- **⚙ Settings** — Telegram / GitHub / Repository / Behaviour bo'limlariga ajratilgan
- **📜 Logs** — INFO / WARNING / ERROR rangli ajratilgan jonli oqim

**Background rejim:**
- Oynani yopish — system tray'ga minimallashtirib qo'yadi (bot ishlashda davom etadi)
- Tray ikonkasi rangi botning holatini ko'rsatadi (yashil = ishlamoqda, kulrang = to'xtagan)
- Tray menyu: **Show / Hide / Start bot / Stop bot / Quit**
- **Quit** botni xavfsiz to'xtatib, chiqib ketadi

PyInstaller orqali `.exe`'ga to'plash mumkin (`build.bat`).

---

## 📁 Loyiha tuzilishi

```
project-ai-automation/
├── main.py             # Telegram bot, command handlers, on_dm/on_group
├── claude_runner.py    # Claude CLI integratsiyasi (analyze, classify, chat)
├── chat_sessions.py    # Multi-session state — SQLite-backed
├── db.py               # SQLite layer: schema, settings, actions, issues, chats, projects, repos
├── updater.py          # Auto-update: GitHub API check, git pull, restart
├── bot_controller.py   # PTB lifecycle (Start/Stop/Pause/Resume)
├── bot_state.py        # Shared paused flag
├── git_ops.py          # apply_fix, push_to_branch, rollback_fix
├── github_ops.py       # PR creation va merge (PyGithub)
├── config.py           # .env loader, lazy reload
├── env_editor.py       # GUI orqali .env tahriri
├── gui.py              # Tkinter desktop GUI
├── XonsaroyBot.spec    # PyInstaller spec
├── build.bat           # Windows build skripti
├── requirements.txt    # Asosiy dependensiyalar
├── requirements-dev.txt# PyInstaller bilan
└── .env.example        # Konfiguratsiya namunasi
```

---

## 🛠 O'rnatish

### Talablar
- **Python 3.10+** (3.13 testdan o'tgan)
- **Claude Code CLI** — [claude.ai/code](https://claude.ai/code) (Max plan
  auth tavsiya qilinadi, `~/.claude/`'da saqlanadi)
- **Git** — target repozitoriya bilan ishlash uchun
- **Telegram bot tokeni** — [@BotFather](https://t.me/BotFather)'dan
- **GitHub PAT** — `repo` scope bilan
- (Ixtiyoriy) **Docker Compose** — target repoda integratsion testlar
  ishga tushirish uchun

### Qadamlar

1. **Klon olish:**
   ```bash
   git clone git@github.com:iBekzod/project-ai-automation.git
   cd project-ai-automation
   ```

2. **Virtual environment + dependensiyalar:**
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux/Mac:
   source .venv/bin/activate

   pip install -r requirements-dev.txt
   ```

3. **Konfiguratsiya:**
   ```bash
   cp .env.example .env
   ```
   `.env` faylini ochib quyidagilarni to'ldiring:

   | O'zgaruvchi | Qiymat | Qayerdan olish |
   |---|---|---|
   | `TELEGRAM_BOT_TOKEN` | bot tokeni | [@BotFather](https://t.me/BotFather) |
   | `TELEGRAM_DEVELOPER_IDS` | bir yoki bir nechta numeric ID, vergul bilan | [@userinfobot](https://t.me/userinfobot) — har bir dasturchi uchun |
   | `MONITORED_GROUP_IDS` | guruh chat_id'lari (vergul bilan) | botni guruhga qo'shing, `/whereami` yozing |
   | `GITHUB_TOKEN` | repo scope bilan PAT | [github.com/settings/tokens](https://github.com/settings/tokens) |
   | `GITHUB_REPO` | `owner/repo` formatida | masalan `iBekzod/xonsaroy` |
   | `REPO_PATH` | target Laravel repoga to'liq yo'l | `D:/projects/.../xonsaroy-latest` |
   | `STAGE_BRANCH` | stage branch nomi | odatda `stage` |
   | `PROD_BRANCH` | prod branch nomi | odatda `production` yoki `dev` |
   | `DRY_RUN` | xavfsiz rejim | dastlab `true` deb qoldiring |
   | `CLAUDE_TIMEOUT` | bir Claude chaqiruvi sekund | 900 (15 daqiqa) tavsiya |
   | `MAX_PARALLEL_CLAUDE` | parallel Claude chaqiruvlari | 5 yetarli |

4. **Bot privacy o'chirish (guruh xabarlarini o'qish uchun):**
   - [@BotFather](https://t.me/BotFather) → `/mybots` → botingiz →
     **Bot Settings** → **Group Privacy** → **Turn off**
   - Botni guruhdan olib tashlang va qayta qo'shing (yoki admin qiling)

5. **Ishga tushirish:**
   ```bash
   # GUI rejimida (tavsiya qilinadi)
   python gui.py

   # yoki CLI rejimida (Logs tab'siz)
   python main.py
   ```

6. **Telegram'da botga `/start`** yuboring — tugmali panel ochiladi.

---

## 🚀 Ishlatish

### Asosiy oqim

1. Xodim guruhda shikoyat yozadi (matn yoki screenshot).
2. Bot 1-bosqich klassifikator orqali tekshiradi.
3. `OUR_PROBLEM` bo'lsa: guruhga *"AI tahlil qilmoqda..."* yoziladi va
   2-bosqich tahlil boshlanadi (3–10 daqiqa).
4. Tahlil tugagach: tashxis kartasi sizning DM'ingizga keladi
   (Qabul / Qayta / O'tkazish tugmalari bilan).
5. **Qabul** bossangiz: bot fix branch yaratadi, commit qiladi, PR ochadi
   va stage'ga merge qiladi (DRY_RUN'da faqat mahalliy).
6. Stage'ga merge bo'lsa **Prodga chiqarish** tugmasi paydo bo'ladi.

### Tugma ↔ buyruq mosligi

| Tugma | Ekvivalent buyruq |
|---|---|
| 📊 Holat | `/status` |
| 💬 Chatlar | `/chatlist` |
| 📖 Yordam | `/help` |
| ✅ Qabul | `/accept` |
| 🔄 Qayta | `/retry` |
| ⏭ O'tkazib | `/skip` |
| 🚀 Stage / Dev / Prod | `/push <branch>` |
| 📤 Chiqarish | `/publish` |
| ↩️ Qaytarish | `/rollback` |
| ⏹ To'xtatish | `/stop` |
| ⏸ Hammasini | `/stopall` |
| ⚙ Menyu | `/menu` |
| 🩺 Ping | `/ping` |

### Yozma buyruqlar (argument bilan)
- `/chat <nom>` — yangi chat yaratish yoki o'tish
- `/ask <savol>` — bir martalik savol-javob (kodga tegmaydi)
- `/task <tavsif>` — to'liq tuzatish jarayoni
- `/retry [izoh]` — qayta tahlil (ixtiyoriy izoh bilan)
- `/push stage|dev|prod` — branch tanlash

### Live rejimga o'tish
1. Stage va prod branchlari real ekanligini tasdiqlang.
2. GitHub PAT'da `repo` scope borligini tekshiring.
3. `.env`'da `DRY_RUN=false` qiling (yoki `/status` orqali tugmasi orqali).
4. Botni Stop → Start qiling.

⚠️ **Xavfsizlik tavsiyalari:**
- Avval kichik bir test task bilan tekshirib ko'ring (masalan log satr qo'shish).
- Stage branch'da uncommitted o'zgarishlar bo'lmasligini ta'minlang.
- PROD_BRANCH ni har doim ikki marta tekshiring.

---

## 🏗 Arxitektura

### Two-stage Claude pipeline

```
DM yoki guruh xabari
        ↓
1-bosqich: classify_via_claude()        [bot dir cwd, ~15s]
        ├─ CHAT          → silent skip
        ├─ OUT_OF_SCOPE  → silent skip
        └─ OUR_PROBLEM   → "AI tahlil qilmoqda..." + 2-bosqich
        ↓
2-bosqich: analyze() — two pass         [REPO_PATH cwd, ~3-10 daq]
        ├─ Pass 1: investigation (free-form, file:line aniqlash)
        └─ Pass 2: format (strict JSON: category, files_to_change, ...)
        ↓
Kategoriya:
        ├─ backend_bug   → Accept/Retry/Skip DM
        └─ boshqalar     → faqat informatsion DM
        ↓
Qabul bosildi:
        ├─ git_ops.apply_fix()    → fix branch + commit + push
        ├─ github_ops.create_pr() → PR ochish + auto-merge to stage
        └─ Prodga chiqarish DM tugmasi
        ↓
Prodga chiqarish:
        └─ git merge stage → PROD_BRANCH + push + guruhga "Tuzatildi"
```

### Multi-session chat

Har bir chat:
- O'z `session_id`'si (Claude `--resume`'ga uzatiladi)
- O'z `asyncio.Task`'i — parallel ishlaydi
- O'z holati: `idle` / `busy`
- `chats.json`'da saqlanadi

`MAX_PARALLEL_CLAUDE` semafori barcha Claude chaqiruvlarini cheklaydi
(default: 5).

---

## 🔧 Buyruq-line argumentlari

GUI'siz ishga tushirsangiz:

```bash
python main.py
```

Bu CLI rejim — `bot.log` yoziladi, lekin GUI Logs tab yo'q. Ctrl+C bilan
to'xtatiladi.

PyInstaller bilan `.exe`'ga to'plash:

```bash
pip install -r requirements-dev.txt
build.bat
# Natija: dist/XonsaroyBot/XonsaroyBot.exe
```

`.exe` yonida `.env` faylini joylashtiring — bot uni avtomatik o'qiydi.

---

## 🧪 Sinov va xatolarni topish

### `bot.log` tekshirish
Asosiy diagnostika manbai. Quyidagi qatorlarni qidiring:
- `stage1 → ...` — 1-bosqich klassifikator natijasi
- `claude: category=...` — to'liq tahlil natijasi
- `<id>: DM sent to developer with ... keyboard` — tashxis yetkazildi
- `markdown send failed (...); retrying as plain text` — formatlash xatosi
- `claude call timed out` — `CLAUDE_TIMEOUT`'ni oshiring

### `/ping`
Claude CLI ulanishini tekshiradi. `Claude javobi: pong` qaytarsa OK.

### `/status`
Bot real-vaqt holatini ko'rsatadi: rejim, chatlar, ochiq muammolar.

---

## 📝 Litsenziya

Bekzod Erkinov uchun shaxsiy loyiha. Hech qanday ochiq litsenziya
ko'rsatilmagan — qayta ishlatish uchun avval mualliflar bilan maslahatlashing.

---

## 🤝 Manbalar

- [Claude Code CLI hujjatlari](https://docs.claude.com/en/docs/claude-code)
- [python-telegram-bot v20+](https://docs.python-telegram-bot.org/)
- [PyGithub](https://pygithub.readthedocs.io/)
- [Telegram Bot API](https://core.telegram.org/bots/api)

---

> Loyiha Claude Opus 4.7 yordamida yaratildi.
