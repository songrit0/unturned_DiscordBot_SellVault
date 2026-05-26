using System.Xml.Serialization;
using Rocket.API;

namespace SellVault
{
    public sealed class Message
    {
        [XmlAttribute] public string Text;
        [XmlAttribute] public string Color;
        public Message() { }
        public Message(string text, string color) { Text = text; Color = color; }
    }

    public sealed class DatabaseSection
    {
        public string ConnectionString;
        /// <summary>Prefix shared with the bot: &lt;p&gt;market, &lt;p&gt;coins, &lt;p&gt;market_log, &lt;p&gt;sellboxes, &lt;p&gt;links, &lt;p&gt;link_codes.</summary>
        public string TablePrefix;
    }

    public sealed class WelcomeItem
    {
        [XmlAttribute] public ushort Id;
        [XmlAttribute] public ushort Amount;
        public WelcomeItem() { }
        public WelcomeItem(ushort id, ushort amount) { Id = id; Amount = amount; }
    }

    public sealed class SellVaultConfiguration : IRocketPluginConfiguration
    {
        public DatabaseSection Database;

        /// <summary>Commission % taken when a player sells (payout = price x (100% - this)). Match the bot's SELL_COMMISSION.</summary>
        public double BaseCommissionPercent;

        /// <summary>Item IDs that bypass commission entirely (cash bills, vouchers, etc). Sold at full face value.</summary>
        [XmlArray("NoCommissionItemIds")]
        [XmlArrayItem("Id")]
        public ushort[] NoCommissionItemIds;

        public string CoinName;

        /// <summary>Item id of the storage barricade used as the virtual /sell vault. Default 328 (Wood Crate).</summary>
        public ushort SellVaultStorageId;

        /// <summary>Coins granted once when a player links their Discord (welcome pack).</summary>
        public long WelcomePackCoins;

        /// <summary>Coins granted per online interval. Set 0 to disable.</summary>
        public long OnlineRewardCoins;
        /// <summary>Seconds between online reward grants.</summary>
        public int OnlineIntervalSeconds;
        /// <summary>If no movement for this many seconds, treat as AFK (no online reward). Set 0 to disable AFK check.</summary>
        public int AfkSeconds;
        /// <summary>Tell the player in chat when an online reward is granted.</summary>
        public bool NotifyOnlineReward;

        // activity rewards (set 0 to disable individual category)
        public long ActivityZombieKill;
        public long ActivityMegaZombieKill;
        public long ActivityPlayerKill;
        public long ActivityAnimalKill;
        public long ActivityBuildPlaced;
        public long ActivityResourceHarvested;
        /// <summary>Cooldown in seconds between PvP kill rewards for the same killer→victim pair (anti-farm).</summary>
        public int PvpRewardCooldownSeconds;

        [XmlArray("WelcomePackItems")]
        [XmlArrayItem("Item")]
        public WelcomeItem[] WelcomePackItems;

        // link messages
        public Message MsgLinkUsage;
        public Message MsgLinked;        // {coins}
        public Message MsgLinkInvalid;
        public Message MsgAlreadyLinked;

        // sell box messages
        public Message MsgSold;          // {coins} {count}
        public Message MsgReturned;      // {count} - items not in the market list, bounced back
        public Message MsgSellBoxSet;
        public Message MsgNeedOpenBox;
        public Message MsgNotInSafeZone;
        public Message MsgVaultOpenFailed;

        public Message MsgBalance;       // {coins}
        public Message MsgReloaded;      // {count}
        public Message MsgError;
        public Message MsgOnlineReward;  // {coins}

        public void LoadDefaults()
        {
            Database = new DatabaseSection
            {
                ConnectionString = "SERVER=localhost;DATABASE=unturned;UID=root;PASSWORD=123456",
                TablePrefix = "sv_"
            };
            BaseCommissionPercent = 40.0;
            NoCommissionItemIds = new ushort[] { 4254, 4255, 4256, 4257, 4258 };
            CoinName = "Coin";
            SellVaultStorageId = 328;
            WelcomePackCoins = 100;
            WelcomePackItems = new WelcomeItem[0];

            OnlineRewardCoins = 10;
            OnlineIntervalSeconds = 300;
            AfkSeconds = 600;
            NotifyOnlineReward = true;

            ActivityZombieKill = 1;
            ActivityMegaZombieKill = 5;
            ActivityPlayerKill = 5;
            ActivityAnimalKill = 2;
            ActivityBuildPlaced = 2;
            ActivityResourceHarvested = 1;
            PvpRewardCooldownSeconds = 120;

            MsgLinkUsage = new Message(
                "ใช้: /link <code> (กดปุ่ม Welcome Pack ใน Discord เพื่อรับ code) | Use: /link <code> from Discord", "yellow");
            MsgLinked = new Message(
                "✅ เชื่อม Discord สำเร็จ! รับ Welcome Pack +{coins} Coin | Linked! +{coins} Coin", "green");
            MsgLinkInvalid = new Message("code ไม่ถูกต้อง/หมดอายุ | Invalid or expired code", "red");
            MsgAlreadyLinked = new Message("บัญชีนี้เชื่อมแล้ว | Already linked", "red");

            MsgSold = new Message(
                "✅ ขายได้ {coins} Coin ({count} ชิ้น) | Sold for {coins} Coin ({count} items)", "green");
            MsgReturned = new Message(
                "ของที่ขายไม่ได้ถูกคืน ({count}) | {count} item(s) not sellable were returned", "yellow");
            MsgSellBoxSet = new Message(
                "✅ ตั้งกล่องนี้เป็นกล่องขายแล้ว | This storage is now a sell box", "green");
            MsgNeedOpenBox = new Message(
                "เล็งไปที่กล่อง storage แล้วพิมพ์ /setsellbox (ระยะ 6m) | Look at a storage barricade within 6m, then /setsellbox", "red");
            MsgNotInSafeZone = new Message(
                "ใช้ /sell ได้ที่ Safe Zone เท่านั้น | /sell can only be used in a Safe Zone", "red");
            MsgVaultOpenFailed = new Message(
                "เปิด vault ไม่สำเร็จ ลองใหม่ | Failed to open vault, try again", "red");

            MsgBalance = new Message("ยอด Coin: {coins} | Balance: {coins} Coin", "green");
            MsgReloaded = new Message("[SellVault] โหลด market ใหม่ {count} รายการ | reloaded {count} items", "green");
            MsgError = new Message("เกิดข้อผิดพลาด ลองใหม่ | Something went wrong", "red");
            MsgOnlineReward = new Message("⏱ ออนไลน์รับ +{coins} Coin | Playtime reward +{coins} Coin", "green");
        }
    }
}
