# 🎮 Unturned Economy — SellVault + Discord Bot

ระบบเศรษฐกิจ Unturned ที่ทำงานคู่กันผ่าน **MySQL ฐานข้อมูลเดียวกัน**

| โฟลเดอร์ | คืออะไร | ภาษา |
|----------|---------|------|
| [`SellVault/`](SellVault/) | ปลั๊กอินในเกม (RocketMod) — กล่อง sell, coin, เชื่อมบัญชี | C# (.NET 4.8) |
| [`DiscordBot/`](DiscordBot/) | บอท Discord — market, ตะกร้า, redeem code, admin panel | Python (discord.py) |

## ภาพรวมการทำงาน
```
รายการตลาด (sv_market): item_id, ชื่อ, price, amount(stock), รูป  ← แกนกลาง
  • Discord bot  : จัดการรายการ/ราคา/สต็อก, ผู้เล่นใส่ตะกร้า→ซื้อ (stock−), redeem code
  • SellVault    : ผู้เล่นเอาของวางกล่อง sell→ปิด→ขาย (stock+, coin = price−คอม)

เชื่อมบัญชี:  [Discord] ปุ่ม Welcome Pack → code → [เกม] /link <code>
ซื้อ:        [Discord] กด ➕ ใส่ตะกร้า → 🧺 ยืนยันซื้อ → code → [เกม] /code
ขาย:        [เกม] วางของในกล่อง sell → ปิด → Coin
เงิน:        coins อยู่ใน sv_coins (ใช้ทั้งเกมและ Discord)
```

## ติดตั้ง (ย่อ)
1. **SellVault**: `SellVault/bin/SellVault.dll` → `Rocket/Plugins/SellVault/` → ตั้ง `ConnectionString` → ดู [SellVault/README.md](SellVault/README.md)
2. **DiscordBot**: `cd DiscordBot && pip install -r requirements.txt` → คัดลอก `.env.example` เป็น `.env` ใส่ token + DB เดียวกัน → `python bot.py` → `/setup` → ดู [DiscordBot/README.md](DiscordBot/README.md)

## ต้องตรงกัน
- `ConnectionString` / `DB_*` ชี้ MySQL ตัวเดียวกัน
- table prefix `sv_` / `rc_` ตรงกันทั้งสองฝั่ง
- `BaseCommissionPercent` (plugin) = `SELL_COMMISSION` (bot)

> ⚠️ ฝั่ง "กล่อง sell" ในเกมต้องเทสบนเซิร์ฟจริง (รันที่อื่นไม่ได้) — ดูหมายเหตุใน SellVault/README.md

Built by imaximum.tech.
