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

        public string CoinName;

        /// <summary>Coins granted once when a player links their Discord (welcome pack).</summary>
        public long WelcomePackCoins;

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

        public Message MsgBalance;       // {coins}
        public Message MsgReloaded;      // {count}
        public Message MsgError;

        public void LoadDefaults()
        {
            Database = new DatabaseSection
            {
                ConnectionString = "SERVER=localhost;DATABASE=unturned;UID=root;PASSWORD=123456",
                TablePrefix = "sv_"
            };
            BaseCommissionPercent = 40.0;
            CoinName = "Coin";
            WelcomePackCoins = 100;
            WelcomePackItems = new WelcomeItem[0];

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
                "เปิดกล่อง storage ที่จะตั้งก่อน แล้วพิมพ์ /setsellbox | Open the storage first, then /setsellbox", "red");

            MsgBalance = new Message("ยอด Coin: {coins} | Balance: {coins} Coin", "green");
            MsgReloaded = new Message("[SellVault] โหลด market ใหม่ {count} รายการ | reloaded {count} items", "green");
            MsgError = new Message("เกิดข้อผิดพลาด ลองใหม่ | Something went wrong", "red");
        }
    }
}
