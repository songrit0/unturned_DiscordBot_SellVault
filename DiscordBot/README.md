# 🤖 Unturned Economy Discord Bot

บอท Discord (Python / discord.py) ที่ต่อ **MySQL ตัวเดียวกับเซิร์ฟเกม** — ทำงานคู่กับปลั๊กอิน **SellVault** + **RedeemCode**

## โมเดล (สำคัญ)
- **list เดียว (market)**: ทุกไอเทมมี `id, ชื่อ, price(ราคาเริ่มต้น), amount(stock หมุนเวียน), รูป`
  - **ซื้อชิ้นเดี่ยว** ใน Discord → stock −1 · **ขายในเกม** (กล่อง sell) → stock +1 · `amount=0` = ซ่อน
  - ขายได้ = `price − คอม%` (`SELL_COMMISSION`) · ซื้อ = เต็ม price
- **package**: ชุดไอเทมซื้อด้วย Coin (ไม่มี stock)
- **ขายในเกม:** ไม่มี `/sell` แล้ว → แอดมินตั้ง "กล่อง sell" ผู้เล่นเอาของวางแล้วปิดกล่อง (ของนอก list เด้งคืน) — *ทำในปลั๊กอิน (เฟส B)*

## ทำอะไรได้
- 🎁 **Welcome Pack / เชื่อมบัญชี** — ปุ่ม → code → เกม `/link <code>` → ผูก + welcome pack
- 💰 **/coins** · 🏆 **/top** · 💸 **/pay @user amount** (โอน Coin)
- 🏷️ **/market** & 🛒 **/shop** — แสดง **ทุกรายการ** แต่ละชิ้นมีปุ่ม **➕ ใส่ตะกร้า** → **🧺 Checkout → ✅ ยืนยันซื้อ** ทีเดียว (หักรวม, ลด stock ทุกชิ้น, สร้าง redeem code เดียว) → เกม `/code`
- *(เอา package และ buy-ชิ้นเดี่ยวออกแล้ว — เหลือ flow ตะกร้าอย่างเดียว)*
- 🛠️ **admin UI:** ห้อง **🛠️-admin** ปุ่มกดเด้ง popup — ตั้ง/แก้/ลบรายการ market, เพิ่ม/ลบแพ็กเกจ, สร้างโค้ด, ให้ Coin, ดูรายการ+id
- 🛠️ **admin commands:** `/setup`, `/adminpanel`, `/setuplink`, `/createcode`, `/givecoins`

## ⚡ Quick start (อัตโนมัติ)
รันบอท → พิมพ์ **`/setup`** ในเซิร์ฟ Discord → บอทสร้างให้เลย:
- category **🎮 Game Economy**
- **🎁-welcome**, **💰-my-coins**, **🛒-shop** (ซื้อชิ้น+แพ็กเกจ), **🏷️-market** (ดูรายการ+รูป), **🏆-leaderboard**, **📖-commands**, **🛠️-admin** (เห็นเฉพาะแอดมิน)
- ตั้งสิทธิ์ห้องให้คนทั่วไปพิมพ์ไม่ได้ (กดปุ่มได้)

> ต้องให้บอทมีสิทธิ์ **Manage Channels** + **Manage Messages** (รัน /setup ซ้ำได้ จะล้างแผงเก่าแล้วโพสต์ใหม่)

## Flow การเชื่อมบัญชี
```
[Discord] กดปุ่ม Welcome Pack → ได้ code (ephemeral)
        ↓
[เกม] /link <code>  → ผูก discord_id ↔ steam_id + รับ welcome pack (coin/ของ)
        ↓
ใช้ /coins /shop ใน Discord ได้เลย
```

## Flow การซื้อของ (shop)
```
[Discord] /shop → กดซื้อ → หัก coin → บอทสร้าง redeem code → ได้ code
        ↓
[เกม] /code <code> → รับของ
```

## ติดตั้ง
```bash
cd unturned_DiscordBot
python -m venv venv && venv\Scripts\activate     # (ตัวเลือก)
pip install -r requirements.txt
copy .env.example .env        # แล้วแก้ค่าในไฟล์ .env
python bot.py
```

### ตั้งค่า `.env`
- `DISCORD_TOKEN` — โทเคนบอท (Discord Developer Portal)
- `GUILD_ID` — id เซิร์ฟ Discord (slash command sync ทันที)
- `ADMIN_ROLE_ID` — role ที่ใช้คำสั่ง admin ได้ (แอดมินจริงใช้ได้อยู่แล้ว)
- `DB_*` — MySQL **ตัวเดียวกับเซิร์ฟเกม**
- `SV_PREFIX` / `RC_PREFIX` — ให้ตรงกับ config ปลั๊กอิน (`sv_`, `rc_`)

> ต้องเปิด **Server Members Intent** ให้บอทใน Developer Portal (สำหรับ `/givecoins` ที่อ้าง member)

## ตารางที่ใช้ (ใช้ร่วมกับปลั๊กอิน)
- `sv_coins`, `sv_sell_items`, `sv_links`, `sv_link_codes` — SellVault
- `sv_shop` — สินค้าในร้าน (บอทสร้าง)
- `rc_codes`, `rc_code_items` — RedeemCode

บอทสร้างตารางทั้งหมด `IF NOT EXISTS` ตอนเริ่ม (รันก่อน/หลังปลั๊กอินก็ได้)

## หมายเหตุ
- ทุก DB query รันใน thread แยก (`asyncio.to_thread`) ไม่บล็อกบอท
- ปุ่ม Welcome Pack เป็น persistent (รอดรีสตาร์ท)
- แก้ราคาขายผ่าน `/setprice` แล้ว ในเกมพิมพ์ `/sellreload` ให้มีผลทันที

Built by imaximum.tech.
