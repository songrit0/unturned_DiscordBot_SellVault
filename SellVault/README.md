# 💰 SellVault — Unturned Market / Sell Box (MySQL)

> ฝั่งในเกมของระบบเศรษฐกิจ (ใช้ MySQL ร่วมกับ Discord bot)
> **ไม่มี `/sell` แล้ว** — แอดมินตั้ง "กล่อง sell" ผู้เล่นเอาของมาวางในกล่องแล้วปิด → ขายได้ Coin

![Game](https://img.shields.io/badge/game-Unturned-2f9e44)
![Framework](https://img.shields.io/badge/framework-RocketMod-blue)
![DB](https://img.shields.io/badge/db-MySQL-orange)

## How it works
- **list เดียว (`sv_market`)** จัดการจาก Discord bot: `item_id, name, price, amount(stock), image`
- **ขาย (ในเกม):** ผู้เล่นเอาของวางในกล่อง sell → **ปิดกล่อง** →
  - ของที่อยู่ใน market → ขาย: ได้ `price × (100% − commission)` ต่อชิ้น, **stock +จำนวน**, log
  - ของที่ **ไม่อยู่ใน list → เด้งคืน** ผู้เล่น (กระเป๋าเต็มวางที่พื้น)
- **ซื้อ** ทำที่ Discord (`/shop`) → ได้ redeem code → ในเกม `/code`
- จับ "ปิดกล่อง" ด้วย event `onInventoryResized` (STORAGE → 0×0) แบบเดียวกับ RFVault

## Commands
| Command | Permission | หน้าที่ |
|---------|------------|---------|
| `/setsellbox` | `sellvault.setsellbox` | เปิดกล่อง storage ค้างไว้ แล้วพิมพ์ → ตั้งเป็นกล่องขาย (admin) |
| `/link <code>` | `sellvault.link` | เชื่อม Discord (code จากปุ่ม Welcome Pack) + รับ welcome pack |
| `/coins` | `sellvault.coins` | เช็คยอด Coin |
| `/sellreload` | `sellvault.reload` | โหลด market + กล่อง sell ใหม่จาก DB (หลังแก้ราคาในบอท) |

## ตั้งกล่อง sell (admin)
1. วาง storage (เช่น Locker/Crate) บนแมพ
2. **เปิดกล่องนั้นค้างไว้** แล้วพิมพ์ `/setsellbox` → กล่องนี้กลายเป็นกล่องขาย (จำตำแหน่งใน `sv_sellboxes`)
3. ผู้เล่นเอาของมาวาง → ปิด → ขาย

## Config (`SellVault.configuration.xml`)
| Field | Default | ความหมาย |
|-------|---------|----------|
| `Database.ConnectionString` | `SERVER=localhost;...` | MySQL (ตัวเดียวกับบอท) |
| `Database.TablePrefix` | `sv_` | prefix (ให้ตรงกับบอท) |
| `BaseCommissionPercent` | `40` | % หักตอนขาย (ให้ตรงกับ `SELL_COMMISSION` ของบอท) |
| `CoinName` | `Coin` | ชื่อสกุลเงิน |
| `WelcomePackCoins` / `WelcomePackItems` | `100` / ว่าง | ของแถมตอนเชื่อมบัญชีครั้งแรก |
| `Msg*` | EN/TH | ข้อความ (placeholder `{coins}`, `{count}`) |

## Install
1. เซิร์ฟมี `MySql.Data.dll` อยู่แล้ว (จากปลั๊กอิน MySQL อื่น)
2. วาง `bin/SellVault.dll` ที่ `Rocket/Plugins/SellVault/`
3. สตาร์ท 1 ครั้ง → ใส่ `ConnectionString` → รีสตาร์ท
4. permission: `sellvault.setsellbox` (admin), `sellvault.link`, `sellvault.coins`, `sellvault.reload`
5. ตั้งรายการ market + ราคา ที่ Discord bot (`/setup` → ห้อง admin)

## ตารางที่ใช้ (ร่วมกับบอท)
`sv_market` (บอทจัดการ) · `sv_coins` · `sv_market_log` · `sv_sellboxes` · `sv_links` · `sv_link_codes`

## ⚠️ ต้องเทสในเกม
ส่วน "กล่อง sell" (ตรวจกล่อง storage + อ่านของตอนปิด) compile ผ่าน (API มีจริง) และอิงแพทเทิร์น onInventoryResized ที่ใช้งานจริง **แต่ผมรันในเกมไม่ได้** — เทสบนเซิร์ฟ:
- `/setsellbox` ตอนเปิดกล่อง → ขึ้นว่าตั้งกล่องแล้ว
- เอาของใน market วาง → ปิด → ได้ Coin + ของหายจากกล่อง
- เอาของนอก list วาง → ปิด → เด้งคืนกระเป๋า
- เช็ค stock ใน market เพิ่มขึ้น

Built by imaximum.tech.
