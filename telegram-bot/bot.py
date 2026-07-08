import os
import random
import threading
import aiosqlite
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

DB_PATH = os.path.join(os.path.dirname(__file__), "message_counts.db")
BOT_OWNER_ID = 8642391507


def current_week_start() -> str:
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


def current_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def seconds_until_next_monday() -> float:
    now = datetime.now(timezone.utc)
    days_ahead = 7 - now.weekday()
    next_monday = (now + timedelta(days=days_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (next_monday - now).total_seconds()


def seconds_until_daily_announce() -> float:
    now = datetime.now(timezone.utc)
    target = now.replace(hour=23, minute=59, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS message_counts (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                count INTEGER DEFAULT 0,
                last_seen TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS weekly_counts (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                week_start TEXT NOT NULL,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id, week_start)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_counts (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                day TEXT NOT NULL,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id, day)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL UNIQUE,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rank_announcements (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rank_level INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id, rank_level)
            )
        """)
        await db.commit()


async def record_message(chat_id: int, user_id: int, username: str, full_name: str) -> int:
    week = current_week_start()
    day = current_day()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO message_counts (chat_id, user_id, username, full_name, count, last_seen)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                count = count + 1,
                username = excluded.username,
                full_name = excluded.full_name,
                last_seen = excluded.last_seen
        """, (chat_id, user_id, username, full_name, datetime.now(timezone.utc)))
        await db.execute("""
            INSERT INTO weekly_counts (chat_id, user_id, username, full_name, week_start, count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id, week_start) DO UPDATE SET
                count = count + 1,
                username = excluded.username,
                full_name = excluded.full_name
        """, (chat_id, user_id, username, full_name, week))
        await db.execute("""
            INSERT INTO daily_counts (chat_id, user_id, username, full_name, day, count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, user_id, day) DO UPDATE SET
                count = count + 1,
                username = excluded.username,
                full_name = excluded.full_name
        """, (chat_id, user_id, username, full_name, day))
        await db.commit()
        async with db.execute(
            "SELECT count FROM message_counts WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else 1


async def get_leaderboard(chat_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT full_name, username, count
            FROM message_counts
            WHERE chat_id = ?
            ORDER BY count DESC LIMIT ?
        """, (chat_id, limit)) as cursor:
            return await cursor.fetchall()


async def get_weekly_leaderboard(chat_id: int, limit: int = 10):
    week = current_week_start()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT full_name, username, count
            FROM weekly_counts
            WHERE chat_id = ? AND week_start = ?
            ORDER BY count DESC LIMIT ?
        """, (chat_id, week, limit)) as cursor:
            return await cursor.fetchall()


async def get_daily_top(chat_id: int, day: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT full_name, username, count
            FROM daily_counts
            WHERE chat_id = ? AND day = ?
            ORDER BY count DESC LIMIT 1
        """, (chat_id, day)) as cursor:
            return await cursor.fetchone()


async def get_all_active_chat_ids() -> list[int]:
    day = current_day()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT DISTINCT chat_id FROM daily_counts WHERE day = ?
        """, (day,)) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def get_user_rank(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT count,
                   (SELECT COUNT(*) + 1 FROM message_counts
                    WHERE chat_id = ? AND count > mc.count) AS rank
            FROM message_counts mc
            WHERE chat_id = ? AND user_id = ?
        """, (chat_id, chat_id, user_id)) as cursor:
            return await cursor.fetchone()


async def get_group_total(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT SUM(count), COUNT(*) FROM message_counts WHERE chat_id = ?
        """, (chat_id,)) as cursor:
            return await cursor.fetchone()


UNVANLAR = [
    (4000, "VIP"),
    (3000, "Veteran"),
    (2500, "Uzman"),
    (2000, "Pro"),
    (1500, "Çapkın"),
    (1000, "Çaylak"),
]

UNVAN_EMOJILERI = {
    "VIP": "💎",
    "Veteran": "🎖️",
    "Uzman": "🧠",
    "Pro": "⚡",
    "Çapkın": "😈",
    "Çaylak": "🌱",
}


def get_unvan(count: int) -> str | None:
    for esik, unvan in UNVANLAR:
        if count >= esik:
            return unvan
    return None


def display_name(full_name: str, username: str | None, count: int = 0) -> str:
    unvan = get_unvan(count)
    emoji = UNVAN_EMOJILERI.get(unvan, "") if unvan else ""
    tag = f" {emoji}{unvan}" if unvan else ""
    if username:
        return f"{full_name}{tag} (@{username})"
    return f"{full_name}{tag}"


async def check_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, user.id)
    if member.status in ("administrator", "creator"):
        return True
    await update.message.reply_text("⛔ Bu komut sadece grup yöneticileri tarafından kullanılabilir.")
    return False


async def daily_announce_and_reset(context: ContextTypes.DEFAULT_TYPE):
    today = current_day()
    chat_ids = await get_all_active_chat_ids()

    print(f"Daily job fired for {today} — {len(chat_ids)} active group(s)")

    for chat_id in chat_ids:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT full_name, username, count
                FROM daily_counts
                WHERE chat_id = ? AND day = ?
                ORDER BY count DESC
            """, (chat_id, today)) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            continue

        lines = [f"🏁 *GÜNÜN SONU! ({today})*\n"]
        for i, (full_name, username, puan) in enumerate(rows):
            lines.append(f"{i + 1}. {full_name} — {puan:,} Puan")

        lines.append(f"\n🥇 Günün şampiyonu haftalık listeye işlendi!")

        mesaj = "\n".join(lines)
        if len(mesaj) > 4000:
            mesaj = mesaj[:3997] + "..."
        try:
            await context.bot.send_message(chat_id=chat_id, text=mesaj, parse_mode="Markdown")
        except Exception as e:
            print(f"Günlük duyuru gönderilemedi {chat_id}: {e}")

    print(f"Daily announce done for {today}")


async def weekly_reset_job(context: ContextTypes.DEFAULT_TYPE):
    prev_monday = (datetime.now(timezone.utc).date() - timedelta(days=7))
    prev_sunday = datetime.now(timezone.utc).date() - timedelta(days=1)
    week_label = f"{prev_monday.isoformat()} / {prev_sunday.isoformat()}"

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT DISTINCT chat_id FROM weekly_counts
            WHERE week_start = ?
        """, (prev_monday.isoformat(),)) as cursor:
            rows = await cursor.fetchall()
    chat_ids = [r[0] for r in rows]

    print(f"Weekly job fired — {len(chat_ids)} grup için haftalık özet gönderiliyor")

    for chat_id in chat_ids:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT full_name, username, count FROM weekly_counts
                WHERE chat_id = ? AND week_start = ?
                ORDER BY count DESC
            """, (chat_id, prev_monday.isoformat())) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            continue

        lines = [f"🏁 *HAFTANIN SONU! ({week_label})*\n"]
        for i, (full_name, username, puan) in enumerate(rows):
            lines.append(f"{i + 1}. {full_name} — {puan:,} Puan")

        lines.append(f"\n🥇 Haftanın şampiyonu tebrikler!")

        mesaj = "\n".join(lines)
        if len(mesaj) > 4000:
            mesaj = mesaj[:3997] + "..."
        try:
            await context.bot.send_message(chat_id=chat_id, text=mesaj, parse_mode="Markdown")
        except Exception as e:
            print(f"Haftalık özet gönderilemedi {chat_id}: {e}")

    print(f"Haftalık özet tamamlandı")


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for user in update.message.new_chat_members:
        if user.is_bot:
            continue
        name = user.full_name
        text = f"👋 Hoş geldin çaylak, *{name}*! Sen de bizle zaman geçirip yaramaz kızları avlayan çapkınlardan biri olabilirsin 😏 Eğlenmene bak, çekinme, sohbete katıl! 🎉"
        await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    msg = update.message
    if not msg.reply_to_message:
        await msg.reply_text("⚠️ Banlamak istediğin kişinin mesajına yanıt vererek /ban yaz.")
        return
    target = msg.reply_to_message.from_user
    if target.is_bot:
        await msg.reply_text("🤖 Bota ban atamazsın.")
        return
    chat = update.effective_chat
    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await msg.reply_text("⚠️ Botun yönetici yetkisi yok, ban atamıyor.")
            return
        await context.bot.ban_chat_member(chat.id, target.id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM message_counts WHERE chat_id = ? AND user_id = ?", (chat.id, target.id))
            await db.execute("DELETE FROM weekly_counts WHERE chat_id = ? AND user_id = ?", (chat.id, target.id))
            await db.execute("DELETE FROM daily_counts WHERE chat_id = ? AND user_id = ?", (chat.id, target.id))
            await db.commit()
        await msg.reply_text(f"🔨 *{target.full_name}* gruptan yasaklandı ve verileri silindi.", parse_mode="Markdown")
    except Exception as e:
        await msg.reply_text(f"❌ Ban atılamadı: {e}")


async def remove_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.left_chat_member
    if not user or user.is_bot:
        return
    chat_id = update.message.chat_id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM message_counts WHERE chat_id = ? AND user_id = ?", (chat_id, user.id))
        await db.execute("DELETE FROM weekly_counts WHERE chat_id = ? AND user_id = ?", (chat_id, user.id))
        await db.execute("DELETE FROM daily_counts WHERE chat_id = ? AND user_id = ?", (chat_id, user.id))
        await db.commit()
    print(f"{user.full_name} gruptan ayrıldı, veriler silindi.")


async def count_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    if msg.chat.type == "private":
        return
    user = msg.from_user
    chat_id = msg.chat_id
    new_count = await record_message(
        chat_id=chat_id,
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT rank_level FROM rank_announcements WHERE chat_id = ? AND user_id = ?",
            (chat_id, user.id)
        ) as cursor:
            duyurulan = {row[0] for row in await cursor.fetchall()}

    for esik, unvan in UNVANLAR:
        if new_count >= esik and esik not in duyurulan:
            emoji = UNVAN_EMOJILERI.get(unvan, "🎉")
            lakap = f"{emoji} {unvan}"
            tebrik = (
                f"{emoji} *Tebrikler {user.full_name}!*\n"
                f"Yeni rütben: *{unvan}* 🎊\n"
                f"_{esik:,} puana ulaştın! Gruba yönetici unvanı verildi._ 💪"
            )
            try:
                await context.bot.promote_chat_member(
                    chat_id=chat_id,
                    user_id=user.id,
                    can_manage_chat=True,
                    can_delete_messages=False,
                    can_manage_video_chats=False,
                    can_restrict_members=False,
                    can_promote_members=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False,
                )
                await context.bot.set_chat_administrator_custom_title(
                    chat_id=chat_id,
                    user_id=user.id,
                    custom_title=lakap,
                )
            except Exception as e:
                print(f"Yöneticilik verilemedi {user.id}: {e}")
            try:
                await msg.reply_text(tebrik, parse_mode="Markdown")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT OR IGNORE INTO rank_announcements (chat_id, user_id, rank_level) VALUES (?, ?, ?)",
                        (chat_id, user.id, esik)
                    )
                    await db.commit()
            except Exception:
                pass
            break


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Mesaj Sayar Bot* aktif!\n\n"
        "Gruptaki her mesajı sayıyorum. Her mesaj = *+1 puan!*\n\n"
        "👥 *Herkese açık komutlar:*\n"
        "/level — Kendi puanın ve rütben\n"
        "/puan — Kendi puanın ve rütben\n"
        "/kural — Grup kuralları\n"
        "/rutbeler — Rütbe sistemi\n"
        "/start — Bu yardım mesajı\n\n"
        "🔐 *Yönetici komutları:*\n"
        "/top — Tüm zamanların en aktif 10 üyesi\n"
        "/week — Bu haftanın liderlik tablosu\n"
        "/members — Tüm üyeler ve puanları\n"
        "/quiet — En sessiz üyeler 🤫\n"
        "/today — Bugünün liderlik tablosu\n"
        "/yesterday — Dünün liderlik tablosu\n"
        "/stats — Kişisel istatistikler\n"
        "/total — Grubun toplam mesaj sayısı\n"
        "/rutbeguncelle — Eksik rütbeleri toplu güncelle\n"
        "/ban — Kullanıcıyı banla (mesajına yanıtla)\n"
        "/reset — Tüm sayaçları sıfırla ⚠️\n\n"
        "👑 *Sahip komutları (DM):*\n"
        "/panel — Bot istatistikleri\n"
        "/gorseller — Kayıtlı görsel sayısı\n"
        "/gorselgonder — Gruplara görsel gönder\n"
        "/gorseltemizle — Tüm görselleri sil\n\n"
        "🏆 *Rütbe Sistemi:*\n"
        "🌱 1.000 puan → Çaylak\n"
        "😈 1.500 puan → Çapkın\n"
        "⚡ 2.000 puan → Pro\n"
        "🧠 2.500 puan → Uzman\n"
        "🎖️ 3.000 puan → Veteran\n"
        "💎 4.000 puan → VIP\n\n"
        "🏁 Her gece 23:59 UTC'de günlük sıralama duyurulur!\n"
        "📅 Her Pazartesi haftalık sıralama duyurulur!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    chat_id = update.effective_chat.id
    rows = await get_leaderboard(chat_id)

    if not rows:
        await update.message.reply_text("Henüz mesaj sayılmadı. Sohbete başlayın!")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["*🏆 Tüm Zamanların En Aktif Üyeleri*\n"]
    for i, (full_name, username, count) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i + 1}."
        name = display_name(full_name, username, count)
        lines.append(f"{medal} {name} — *{count:,}* puan")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    chat_id = update.effective_chat.id
    rows = await get_weekly_leaderboard(chat_id)

    week = current_week_start()
    week_dt = datetime.fromisoformat(week)
    week_end = (week_dt + timedelta(days=6)).strftime("%-d %b")
    week_label = f"{week_dt.strftime('%-d %b')} – {week_end}"

    if not rows:
        await update.message.reply_text(
            f"📅 Bu hafta henüz mesaj yok ({week_label}). Sohbete başlayın!"
        )
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"*📅 Bu Haftanın Liderlik Tablosu*\n_{week_label}_\n"]
    for i, (full_name, username, count) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i + 1}."
        name = display_name(full_name, username)
        lines.append(f"{medal} {name} — *{count:,}* puan")

    lines.append("\n_Her Pazartesi gece yarısı (UTC) sıfırlanır_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    chat_id = update.effective_chat.id
    day = current_day()
    today_label = datetime.now(timezone.utc).strftime("%-d %B %Y")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT full_name, username, count
            FROM daily_counts
            WHERE chat_id = ? AND day = ?
            ORDER BY count DESC LIMIT 10
        """, (chat_id, day)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await update.message.reply_text(f"📅 Bugün ({today_label}) henüz mesaj yok!")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"*📅 Bugünün Liderlik Tablosu*\n_{today_label}_\n"]
    for i, (full_name, username, count) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i + 1}."
        name = display_name(full_name, username)
        lines.append(f"{medal} {name} — *{count:,}* puan")

    lines.append("\n_Her gece 23:59'da günün birincisi açıklanır_ 🌟")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    chat_id = update.effective_chat.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT full_name, username, count
            FROM message_counts
            WHERE chat_id = ?
            ORDER BY count ASC
            LIMIT 10
        """, (chat_id,)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await update.message.reply_text("Henüz hiç mesaj kaydedilmedi.")
        return

    lines = ["😴 *Grubun En Sessiz Üyeleri*\n_Hadi biraz konuşun!_ 👇\n"]
    for i, (full_name, username, count) in enumerate(rows):
        name = display_name(full_name, username, count)
        lines.append(f"{i + 1}. {name} — *{count:,}* puan 🤫")

    lines.append("\n💬 _Yazmayan kaybolur, hadi katılın!_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    chat_id = update.effective_chat.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT full_name, username, count
            FROM message_counts
            WHERE chat_id = ?
            ORDER BY count DESC
        """, (chat_id,)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await update.message.reply_text("Henüz hiç mesaj kaydedilmedi.")
        return

    toplam = sum(r[2] for r in rows)
    sirala_emojileri = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = [f"📊 *Bugüne kadar {len(rows)} kişi toplam {toplam:,} puan attı, işte sıralama!* 🔥\n"]
    for i, (full_name, username, count) in enumerate(rows):
        name = display_name(full_name, username, count)
        emoji = sirala_emojileri[i] if i < len(sirala_emojileri) else f"{i + 1}."
        lines.append(f"{emoji} {name} — *{count:,}* puan")

    mesaj = "\n".join(lines)
    if len(mesaj) > 4000:
        mesaj = mesaj[:3997] + "..."

    await update.message.reply_text(mesaj, parse_mode="Markdown")


async def cmd_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    chat_id = update.effective_chat.id
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    yesterday_label = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%-d %B %Y")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT full_name, username, count
            FROM daily_counts
            WHERE chat_id = ? AND day = ?
            ORDER BY count DESC LIMIT 10
        """, (chat_id, yesterday)) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await update.message.reply_text(
            f"📅 Dün ({yesterday_label}) için kayıtlı veri bulunamadı."
        )
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"*📅 Dünün Liderlik Tablosu*\n_{yesterday_label}_\n"]
    for i, (full_name, username, count) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i + 1}."
        name = display_name(full_name, username)
        lines.append(f"{medal} {name} — *{count:,}* puan")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    user = update.effective_user
    chat_id = update.effective_chat.id

    row = await get_user_rank(chat_id, user.id)
    if not row or row[0] is None:
        await update.message.reply_text("Henüz hiç mesaj atmadınız!")
        return

    week = current_week_start()
    day = current_day()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT count FROM weekly_counts
            WHERE chat_id = ? AND user_id = ? AND week_start = ?
        """, (chat_id, user.id, week)) as cursor:
            week_row = await cursor.fetchone()
        async with db.execute("""
            SELECT count FROM daily_counts
            WHERE chat_id = ? AND user_id = ? AND day = ?
        """, (chat_id, user.id, day)) as cursor:
            day_row = await cursor.fetchone()

    count, rank = row
    week_count = week_row[0] if week_row else 0
    day_count = day_row[0] if day_row else 0
    name = display_name(user.full_name, user.username, count)
    unvan = get_unvan(count)
    unvan_satir = f"Ünvan: *{UNVAN_EMOJILERI.get(unvan, '')} {unvan}*\n" if unvan else "Ünvan: _Henüz yok (250 puana ulaş)_\n"
    text = (
        f"📊 *{name} İstatistikleri*\n\n"
        f"{unvan_satir}"
        f"Bugün: *{day_count:,}* puan\n"
        f"Bu hafta: *{week_count:,}* puan\n"
        f"Tüm zamanlar: *{count:,}* puan\n"
        f"Genel sıralama: *#{rank}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    chat_id = update.effective_chat.id
    row = await get_group_total(chat_id)

    total, members = row if row else (0, 0)
    total = total or 0
    members = members or 0

    text = (
        f"📈 *Grup Mesaj İstatistikleri*\n\n"
        f"Toplam puan: *{total:,}*\n"
        f"Aktif üye sayısı: *{members}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if chat.type == "private":
        await update.message.reply_text("ℹ️ /level komutunu grupta yaz — kendi puanını ve rütbeni göreceğsin.")
        return

    chat_id = chat.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT count FROM message_counts WHERE chat_id = ? AND user_id = ?",
            (chat_id, user.id)
        ) as cursor:
            row = await cursor.fetchone()

    if not row or row[0] is None:
        await update.message.reply_text("Henüz hiç mesaj atmadınız! Sohbete katılın 💬")
        return

    count = row[0]
    unvan = get_unvan(count)

    if unvan:
        rutbe_satir = f"🏆 Rütbe: ⭐ *{unvan}* ⭐"
    else:
        sonraki = next((e for e, _ in reversed(UNVANLAR) if e > count), 250)
        kalan = sonraki - count
        rutbe_satir = f"🏆 Rütbe: _Henüz yok_ ({kalan} puan daha kazan!)"

    text = (
        f"👤 *{user.full_name}*\n"
        f"📊 Puan: *{count:,}*\n"
        f"{rutbe_satir}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_kural(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "✨ *GRUP KURALLARIMIZ VE HAKKIMIZDA* ✨\n\n"
        "♥️ Grubumuzun düzenini korumak ve herkese keyifli bir ortam sunmak adına kurallarımız aşağıda maddeler halinde belirtilmiştir;\n\n"
        "💰 *1. Ücretsiz Katılım:* Sohbet grubumuz tamamen ücretsizdir. Üyelerden herhangi bir ad altında ücret talep edilemez.\n\n"
        "🛡️ *2. Güvenlik ve Yardımlaşma:* Grubumuz; c2 uygulaması üzerinden anlık ST gönderen hesapları paylaşıp fikir alışverişinde bulunarak, üyelerimizin yaşayabileceği mağduriyetleri en aza indirmeyi amaçlayan bir topluluktur.\n\n"
        "💬 *3. Sohbete Katılım:* Grubumuzda çekinmeden, saygı çerçevesi dahilinde her zaman sohbete katılabilir ve aktif olabilirsiniz.\n\n"
        "⭐ *4. Puanlar Hakkında:* İleriki zamanlarda çekilişlere katılabilir veya puanlı oyun sisteminde kullanarak daha fazla puana sahip olabilirsiniz.\n\n"
        "🏆 *5. Rütbe ve Aktivite Sistemi:* Oyunlar, çekilişler ve sohbetteki aktiflik durumunuza göre grupta rütbe atlayabileceğiniz bir Puan sistemimiz mevcuttur.\n\n"
        "🎉 _Grup içerisindeki her mesajınız için +1 puan kazanırsınız!_\n"
        "🌱 1.000 puan → Çaylak | 😈 1.500 → Çapkın | ⚡ 2.000 → Pro\n"
        "🧠 2.500 → Uzman | 🎖️ 3.000 → Veteran | 💎 4.000 → VIP 🎉\n\n"
        "✅ *Komutlar:* /puan — /rutbeler — /level\n\n"
        "⚠️ _Not: Grup düzenini bozan, üslubuna dikkat etmeyen kişiler uyarılmaksızın gruptan uzaklaştırılır. Keyifli sohbetler dileriz.._ 👊"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_rutbeler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏅 *Montana Tayfa Rütbe Sistemi*\n\n"
        "Her mesaj için *+1 puan* kazanırsın!\n"
        "Belirli puanlara ulaşınca otomatik rütbe alırsın:\n\n"
        "🌱 *Çaylak* — 1.000 puan\n"
        "😈 *Çapkın* — 1.500 puan\n"
        "⚡ *Pro* — 2.000 puan\n"
        "🧠 *Uzman* — 2.500 puan\n"
        "🎖️ *Veteran* — 3.000 puan\n"
        "💎 *VIP* — 4.000 puan\n\n"
        "📊 Kendi puanını görmek için: /puan"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_puan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if chat.type == "private":
        await update.message.reply_text("ℹ️ /puan komutunu grupta yaz — kendi puanını ve rütbeni göreceğsin.")
        return

    chat_id = chat.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT count FROM message_counts WHERE chat_id = ? AND user_id = ?",
            (chat_id, user.id)
        ) as cursor:
            row = await cursor.fetchone()

    if not row or row[0] is None:
        await update.message.reply_text("Henüz hiç mesaj atmadınız! Sohbete katılın ve puan kazanın 💬")
        return

    count = row[0]
    unvan = get_unvan(count)

    if unvan:
        emoji = UNVAN_EMOJILERI.get(unvan, "🏅")
        rutbe_satir = f"🏆 Rütbe: {emoji} *{unvan}*"
    else:
        sonraki = next((e for e, _ in reversed(UNVANLAR) if e > count), 250)
        kalan = sonraki - count
        rutbe_satir = f"🏆 Rütbe: _Henüz yok_ — {kalan} puan daha kazan! 💪"

    text = (
        f"👤 *{user.full_name}*\n"
        f"📊 Puan: *{count:,}*\n"
        f"{rutbe_satir}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_rutbeguncelle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    chat_id = update.effective_chat.id

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, full_name, username, count FROM message_counts WHERE chat_id = ? ORDER BY count DESC",
            (chat_id,)
        ) as cursor:
            uyeler = await cursor.fetchall()
        async with db.execute(
            "SELECT user_id, rank_level FROM rank_announcements WHERE chat_id = ?",
            (chat_id,)
        ) as cursor:
            duyurulan_rows = await cursor.fetchall()

    duyurulan = {}
    for user_id, rank_level in duyurulan_rows:
        duyurulan.setdefault(user_id, set()).add(rank_level)

    await update.message.reply_text("🔄 Rütbeler kontrol ediliyor, eksik duyurular yapılıyor...")

    toplam = 0
    for user_id, full_name, username, count in uyeler:
        kullanici_duyurulan = duyurulan.get(user_id, set())
        guncel_unvan = None
        for esik, unvan in UNVANLAR:
            if count >= esik:
                guncel_unvan = unvan
                break
        for esik, unvan in UNVANLAR:
            if count >= esik and esik not in kullanici_duyurulan:
                emoji = UNVAN_EMOJILERI.get(unvan, "🎉")
                mention = f"[{full_name}](tg://user?id={user_id})"
                tebrik = (
                    f"{emoji} *Tebrikler* {mention}*!*\n"
                    f"Rütben: *{unvan}* 🎊\n"
                    f"_{esik:,} puana ulaştın! Yönetici unvanı verildi._ 💪"
                )
                try:
                    await update.message.reply_text(tebrik, parse_mode="Markdown")
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "INSERT OR IGNORE INTO rank_announcements (chat_id, user_id, rank_level) VALUES (?, ?, ?)",
                            (chat_id, user_id, esik)
                        )
                        await db.commit()
                    toplam += 1
                except Exception as e:
                    print(f"Rütbe duyurusu gönderilemedi {user_id}: {e}")
        if guncel_unvan:
            emoji = UNVAN_EMOJILERI.get(guncel_unvan, "")
            lakap = f"{emoji} {guncel_unvan}"
            try:
                await context.bot.promote_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    can_manage_chat=True,
                    can_delete_messages=False,
                    can_manage_video_chats=False,
                    can_restrict_members=False,
                    can_promote_members=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False,
                )
                await context.bot.set_chat_administrator_custom_title(
                    chat_id=chat_id,
                    user_id=user_id,
                    custom_title=lakap,
                )
            except Exception as e:
                print(f"Rutbeguncelle yöneticilik verilemedi {user_id}: {e}")

    if toplam == 0:
        await update.message.reply_text("✅ Tüm rütbeler zaten güncel, eksik duyuru yok!")
    else:
        await update.message.reply_text(f"✅ Toplam *{toplam}* eksik rütbe duyurusu yapıldı!", parse_mode="Markdown")


async def save_owner_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != BOT_OWNER_ID:
        return
    if update.effective_chat.type != "private":
        return
    photo = update.message.photo[-1]
    file_id = photo.file_id
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO bot_images (file_id) VALUES (?)", (file_id,))
            await db.commit()
            async with db.execute("SELECT COUNT(*) FROM bot_images") as cursor:
                toplam = (await cursor.fetchone())[0]
            await update.message.reply_text(f"✅ Görsel kaydedildi! Toplam kayıtlı görsel: *{toplam}*", parse_mode="Markdown")
        except Exception:
            await update.message.reply_text("⚠️ Bu görsel zaten kayıtlı.")


async def cmd_gorseller(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != BOT_OWNER_ID:
        await update.message.reply_text("⛔ Bu komuta erişim izniniz yok.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM bot_images") as cursor:
            toplam = (await cursor.fetchone())[0]
    await update.message.reply_text(
        f"🖼️ Kayıtlı görsel sayısı: *{toplam}*\n\n"
        f"Görsel eklemek için bana DM'den fotoğraf gönder.\n"
        f"Görselleri silmek için /gorseltemizle yaz.",
        parse_mode="Markdown"
    )


async def cmd_gorseltemizle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != BOT_OWNER_ID:
        await update.message.reply_text("⛔ Bu komuta erişim izniniz yok.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bot_images")
        await db.commit()
    await update.message.reply_text("🗑️ Tüm kayıtlı görseller silindi.")


async def cmd_gorselgonder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != BOT_OWNER_ID:
        await update.message.reply_text("⛔ Bu komuta erişim izniniz yok.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT file_id FROM bot_images ORDER BY RANDOM() LIMIT 2") as cursor:
            gorseller = [row[0] for row in await cursor.fetchall()]
        async with db.execute("SELECT DISTINCT chat_id FROM message_counts") as cursor:
            chat_ids = [row[0] for row in await cursor.fetchall()]

    if not gorseller:
        await update.message.reply_text("⚠️ Henüz kayıtlı görsel yok. Bana DM'den fotoğraf gönder.")
        return
    if not chat_ids:
        await update.message.reply_text("⚠️ Henüz aktif grup bulunamadı.")
        return

    await update.message.reply_text(f"📤 Gönderiliyor… {len(chat_ids)} grup × {len(gorseller)} görsel")

    basarili = 0
    basarisiz = 0
    for chat_id in chat_ids:
        for file_id in gorseller:
            try:
                await context.bot.send_photo(chat_id=chat_id, photo=file_id)
                basarili += 1
            except Exception as e:
                print(f"Görsel gönderilemedi {chat_id}: {e}")
                basarisiz += 1

    sonuc = f"✅ Gönderim tamamlandı!\n\n📨 Başarılı: *{basarili}*"
    if basarisiz:
        sonuc += f"\n❌ Başarısız: *{basarisiz}*"
    await update.message.reply_text(sonuc, parse_mode="Markdown")


async def otomatik_gorsel_gonder(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT file_id FROM bot_images ORDER BY RANDOM() LIMIT 2") as cursor:
            gorseller = [row[0] for row in await cursor.fetchall()]
        async with db.execute("SELECT DISTINCT chat_id FROM message_counts") as cursor:
            chat_ids = [row[0] for row in await cursor.fetchall()]

    if not gorseller:
        print("Otomatik görsel: kayıtlı görsel yok, atlanıyor.")
        return
    if not chat_ids:
        print("Otomatik görsel: aktif grup yok, atlanıyor.")
        return

    print(f"Otomatik görsel gönderiliyor — {len(chat_ids)} grup, {len(gorseller)} görsel")
    for chat_id in chat_ids:
        for file_id in gorseller:
            try:
                await context.bot.send_photo(chat_id=chat_id, photo=file_id)
            except Exception as e:
                print(f"Görsel gönderilemedi {chat_id}: {e}")


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != BOT_OWNER_ID:
        await update.message.reply_text("⛔ Bu komuta erişim izniniz yok.")
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("📊 Panel sadece bota özel mesajdan (DM) çalışır.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(DISTINCT chat_id) FROM message_counts") as cursor:
            grup_sayisi = (await cursor.fetchone())[0] or 0
        async with db.execute("SELECT SUM(count) FROM message_counts") as cursor:
            toplam_mesaj = (await cursor.fetchone())[0] or 0
        async with db.execute("SELECT COUNT(DISTINCT user_id) FROM message_counts") as cursor:
            toplam_uye = (await cursor.fetchone())[0] or 0
        async with db.execute("""
            SELECT chat_id, SUM(count) as total
            FROM message_counts GROUP BY chat_id ORDER BY total DESC LIMIT 1
        """) as cursor:
            en_aktif_grup = await cursor.fetchone()
        async with db.execute("""
            SELECT full_name, username, SUM(count) as total
            FROM message_counts GROUP BY user_id ORDER BY total DESC LIMIT 1
        """) as cursor:
            en_aktif_uye = await cursor.fetchone()
        today = current_day()
        async with db.execute("SELECT SUM(count) FROM daily_counts WHERE day = ?", (today,)) as cursor:
            bugun_mesaj = (await cursor.fetchone())[0] or 0

    en_aktif_grup_text = f"Grup ID: `{en_aktif_grup[0]}` — {en_aktif_grup[1]:,} puan" if en_aktif_grup else "—"
    en_aktif_uye_text = f"{display_name(en_aktif_uye[0], en_aktif_uye[1])} — {en_aktif_uye[2]:,} puan" if en_aktif_uye else "—"

    text = (
        f"🤖 *Bot Yönetici Paneli*\n"
        f"{'─' * 28}\n\n"
        f"🏘️ Aktif grup sayısı: *{grup_sayisi}*\n"
        f"👥 Toplam üye sayısı: *{toplam_uye}*\n"
        f"📩 Toplam puan (tüm zamanlar): *{toplam_mesaj:,}*\n"
        f"📅 Bugün atılan puan: *{bugun_mesaj:,}*\n\n"
        f"🏆 En aktif grup:\n    {en_aktif_grup_text}\n\n"
        f"👑 En aktif üye (genel):\n    {en_aktif_uye_text}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update, context):
        return
    chat_id = update.effective_chat.id

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM message_counts WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM weekly_counts WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM daily_counts WHERE chat_id = ?", (chat_id,))
        await db.commit()

    await update.message.reply_text("✅ Tüm sayaçlar (günlük, haftalık, tüm zamanlar) sıfırlandı.")


async def post_init(application: Application) -> None:
    await init_db()

    weekly_delay = seconds_until_next_monday()
    application.job_queue.run_repeating(
        weekly_reset_job,
        interval=timedelta(weeks=1),
        first=weekly_delay,
        name="weekly_reset",
    )
    print(f"Haftalık sıfırlama: {weekly_delay / 3600:.1f} saat sonra (Pazartesi UTC)")

    daily_delay = seconds_until_daily_announce()
    application.job_queue.run_repeating(
        daily_announce_and_reset,
        interval=timedelta(days=1),
        first=daily_delay,
        name="daily_announce",
    )
    print(f"Günlük duyuru: {daily_delay / 60:.1f} dakika sonra (23:59 UTC)")

    application.job_queue.run_repeating(
        otomatik_gorsel_gonder,
        interval=timedelta(hours=5),
        first=timedelta(hours=5),
        name="otomatik_gorsel",
    )
    print("Otomatik görsel: her 5 saatte bir gönderilecek")


def start_ping_server():
    class PingHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot calisiyor!")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    render_port = os.environ.get("PORT")
    if render_port:
        port = int(render_port)
        server = HTTPServer(("0.0.0.0", port), PingHandler)
        print(f"Ping sunucusu port {port} üzerinde çalışıyor (Render)")
        server.serve_forever()
        return

    for port in [5000, 5001, 5050, 9090, 9191]:
        try:
            server = HTTPServer(("0.0.0.0", port), PingHandler)
            print(f"Ping sunucusu port {port} üzerinde çalışıyor")
            server.serve_forever()
            break
        except OSError:
            continue


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN_SUFFIX", "")
    bot_id = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    if ":" not in token and bot_id and token:
        token = f"{bot_id}:{token}"

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN_SUFFIX ortam değişkeni ayarlanmamış")
    if ":" not in token:
        raise RuntimeError(f"Token geçersiz — tam token girilmeli (örn: 123456:ABCdef...). Mevcut uzunluk: {len(token)}")

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("members", cmd_members))
    app.add_handler(CommandHandler("quiet", cmd_quiet))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("yesterday", cmd_yesterday))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("total", cmd_total))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("level", cmd_level))
    app.add_handler(CommandHandler("rutbeguncelle", cmd_rutbeguncelle))
    app.add_handler(CommandHandler("kural", cmd_kural))
    app.add_handler(CommandHandler("rutbeler", cmd_rutbeler))
    app.add_handler(CommandHandler("puan", cmd_puan))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("gorseller", cmd_gorseller))
    app.add_handler(CommandHandler("gorseltemizle", cmd_gorseltemizle))
    app.add_handler(CommandHandler("gorselgonder", cmd_gorselgonder))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, remove_left_member))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, save_owner_photo))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND & (
            filters.TEXT | filters.PHOTO | filters.Sticker.ALL |
            filters.VOICE | filters.VIDEO | filters.ANIMATION |
            filters.AUDIO | filters.Document.ALL | filters.VIDEO_NOTE
        ),
        count_message
    ))

    ping_thread = threading.Thread(target=start_ping_server, daemon=True)
    ping_thread.start()

    print("Bot çalışıyor...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
