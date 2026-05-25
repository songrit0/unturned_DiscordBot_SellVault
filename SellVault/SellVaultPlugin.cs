using System;
using System.Collections.Generic;
using System.Threading;
using Rocket.Core.Logging;
using Rocket.Core.Plugins;
using Rocket.Unturned;
using Rocket.Unturned.Chat;
using Rocket.Unturned.Player;
using SDG.Unturned;
using Steamworks;
using UnityEngine;
using Action = System.Action;
using Logger = Rocket.Core.Logging.Logger;

namespace SellVault
{
    /// <summary>
    /// In-game side of the unified market. No /sell command: admins register storage barricades as
    /// "sell boxes" with /setsellbox. When a player closes a sell box, items that exist in the market
    /// list are sold (coins = price x (100% - commission), stock +amount), and items not in the list
    /// are returned to the player. Also handles Discord account linking + welcome pack + /coins.
    /// DB on background threads; item handling on the Unity main thread.
    /// </summary>
    public sealed class SellVaultPlugin : RocketPlugin<SellVaultConfiguration>
    {
        public static SellVaultPlugin Instance { get; private set; }
        public SellDatabase Database { get; private set; }

        private Dictionary<ushort, double> _prices = new Dictionary<ushort, double>();
        private HashSet<string> _boxKeys = new HashSet<string>();
        private readonly List<SellVaultComponent> _components = new List<SellVaultComponent>();
        private readonly object _lock = new object();
        private readonly Queue<Action> _main = new Queue<Action>();

        private struct SaleEntry { public ushort Id; public int Amount; public long Coins; }

        protected override void Load()
        {
            Instance = this;
            DatabaseSection db = Configuration.Instance.Database;
            Database = new SellDatabase(db.ConnectionString, db.TablePrefix);
            _prices = Database.LoadMarketPrices();
            _boxKeys = new HashSet<string>(Database.LoadSellBoxKeys());

            U.Events.OnPlayerConnected += OnConnected;
            U.Events.OnPlayerDisconnected += OnDisconnected;
            foreach (SteamPlayer sp in Provider.clients)
                if (sp?.player != null) AttachComponent(sp.player);

            Logger.Log("SellVault loaded. Market=" + _prices.Count + " items, SellBoxes=" + _boxKeys.Count +
                       ", Commission=" + Configuration.Instance.BaseCommissionPercent + "%.");
        }

        protected override void Unload()
        {
            U.Events.OnPlayerConnected -= OnConnected;
            U.Events.OnPlayerDisconnected -= OnDisconnected;
            foreach (SellVaultComponent c in _components.ToArray())
                if (c != null) UnityEngine.Object.Destroy(c);
            _components.Clear();
            lock (_lock) _main.Clear();
            Database = null;
            Instance = null;
            Logger.Log("SellVault unloaded.");
        }

        // ---- component lifecycle ----

        private void OnConnected(UnturnedPlayer p) => AttachComponent(p.Player);

        private void OnDisconnected(UnturnedPlayer p)
        {
            SellVaultComponent c = p.Player?.gameObject.GetComponent<SellVaultComponent>();
            if (c != null) { _components.Remove(c); UnityEngine.Object.Destroy(c); }
        }

        private void AttachComponent(Player player)
        {
            if (player == null || player.gameObject.GetComponent<SellVaultComponent>() != null) return;
            SellVaultComponent c = player.gameObject.AddComponent<SellVaultComponent>();
            c.Init(player);
            _components.Add(c);
        }

        // ---- main-thread dispatch ----

        public void Enqueue(Action action)
        {
            if (action == null) return;
            lock (_lock) _main.Enqueue(action);
        }

        private void FixedUpdate()
        {
            while (true)
            {
                Action a = null;
                lock (_lock) { if (_main.Count > 0) a = _main.Dequeue(); }
                if (a == null) break;
                try { a(); } catch (Exception ex) { Logger.LogException(ex, "[SellVault] main action"); }
            }
        }

        // ---- sell boxes ----

        private static string PosKey(Vector3 p)
        {
            return Mathf.RoundToInt(p.x * 10f) + "_" + Mathf.RoundToInt(p.y * 10f) + "_" + Mathf.RoundToInt(p.z * 10f);
        }

        public void SetSellBox(UnturnedPlayer up)
        {
            Player p = up.Player;
            if (p == null) return;
            SellVaultConfiguration cfg = Configuration.Instance;
            InteractableStorage storage = p.inventory != null ? p.inventory.storage : null;
            if (storage == null) { Say(p, cfg.MsgNeedOpenBox); return; }

            string key = PosKey(storage.transform.position);
            _boxKeys.Add(key);
            ulong by = up.CSteamID.m_SteamID;
            ThreadPool.QueueUserWorkItem(_ => Database?.AddSellBox(key, by));
            Say(p, cfg.MsgSellBoxSet);
        }

        public void OnStorageClosed(Player player, InteractableStorage storage)
        {
            if (player == null || storage == null || _boxKeys == null) return;
            if (!_boxKeys.Contains(PosKey(storage.transform.position))) return;
            ProcessSellBox(player, storage);
        }

        private void ProcessSellBox(Player player, InteractableStorage storage)
        {
            Items v = storage.items;
            if (v == null) return;

            SellVaultConfiguration cfg = Configuration.Instance;
            ulong steamId = player.channel.owner.playerID.steamID.m_SteamID;
            Vector3 pos = player.transform.position;

            long total = 0;
            int sold = 0, returned = 0;
            List<SaleEntry> log = new List<SaleEntry>();

            foreach (ItemJar jar in new List<ItemJar>(v.items))
            {
                if (jar?.item == null) continue;
                ushort id = jar.item.id;
                int amt = jar.item.amount < 1 ? 1 : jar.item.amount;

                if (_prices.TryGetValue(id, out double price))
                {
                    long pay = Payout(price, amt, cfg);
                    total += pay;
                    sold++;
                    log.Add(new SaleEntry { Id = id, Amount = amt, Coins = pay });
                }
                else
                {
                    if (!player.inventory.tryAddItem(jar.item, true))
                        ItemManager.dropItem(jar.item, pos, true, true, true);
                    returned++;
                }
            }

            v.clear();

            if (sold > 0)
            {
                Say(player, cfg.MsgSold, total, sold);
                long credit = total;
                ThreadPool.QueueUserWorkItem(_ =>
                {
                    SellDatabase db = Database;
                    if (db == null) return;
                    db.AddCoins(steamId, credit);
                    foreach (SaleEntry e in log)
                    {
                        db.AddStock(e.Id, e.Amount);
                        db.LogSale(steamId, e.Id, e.Amount, e.Coins);
                    }
                });
            }
            if (returned > 0)
                Say(player, cfg.MsgReturned, 0, returned);
        }

        private static long Payout(double price, int amount, SellVaultConfiguration cfg)
        {
            double rate = 1.0 - cfg.BaseCommissionPercent / 100.0;
            if (rate < 0) rate = 0;
            long r = (long)Math.Floor(price * amount * rate);
            return r < 0 ? 0 : r;
        }

        // ---- reload (market + boxes) ----

        public void Reload(Rocket.API.IRocketPlayer caller)
        {
            ThreadPool.QueueUserWorkItem(_ =>
            {
                SellDatabase db = Database;
                if (db == null) return;
                Dictionary<ushort, double> prices = db.LoadMarketPrices();
                List<string> boxes = db.LoadSellBoxKeys();
                Enqueue(() =>
                {
                    _prices = prices;
                    _boxKeys = new HashSet<string>(boxes);
                    Message m = Configuration.Instance.MsgReloaded;
                    UnturnedChat.Say(caller, (m?.Text ?? "reloaded {count}").Replace("{count}", prices.Count.ToString()),
                        UnturnedChat.GetColorFromName(m?.Color, Color.green));
                });
            });
        }

        // ---- coins ----

        public void ShowBalance(UnturnedPlayer up)
        {
            Player player = up.Player;
            if (player == null) return;
            ulong steamId = up.CSteamID.m_SteamID;
            ThreadPool.QueueUserWorkItem(_ =>
            {
                SellDatabase db = Database;
                if (db == null) return;
                long bal = db.GetCoins(steamId);
                Enqueue(() =>
                {
                    Player pl = PlayerTool.getPlayer(new CSteamID(steamId));
                    if (pl != null) Say(pl, Configuration.Instance.MsgBalance, bal < 0 ? 0 : bal);
                });
            });
        }

        // ---- account linking ----

        public void LinkAccount(UnturnedPlayer up, string code)
        {
            Player player = up.Player;
            if (player == null) return;
            ulong steamId = up.CSteamID.m_SteamID;
            SellVaultConfiguration cfg = Configuration.Instance;

            ThreadPool.QueueUserWorkItem(_ =>
            {
                SellDatabase db = Database;
                if (db == null) return;
                LinkResult res = db.ConsumeLinkCode(code, steamId, out ulong _did);
                if (res == LinkResult.Linked && cfg.WelcomePackCoins != 0)
                    db.AddCoins(steamId, cfg.WelcomePackCoins);

                Enqueue(() =>
                {
                    Player p = PlayerTool.getPlayer(new CSteamID(steamId));
                    if (p == null) return;
                    switch (res)
                    {
                        case LinkResult.Linked:
                            GiveWelcomeItems(p, cfg);
                            Say(p, cfg.MsgLinked, cfg.WelcomePackCoins);
                            break;
                        case LinkResult.SteamAlreadyLinked:
                        case LinkResult.DiscordAlreadyLinked:
                            Say(p, cfg.MsgAlreadyLinked);
                            break;
                        case LinkResult.CodeInvalid:
                            Say(p, cfg.MsgLinkInvalid);
                            break;
                        default:
                            Say(p, cfg.MsgError);
                            break;
                    }
                });
            });
        }

        private static void GiveWelcomeItems(Player p, SellVaultConfiguration cfg)
        {
            if (cfg.WelcomePackItems == null) return;
            foreach (WelcomeItem wi in cfg.WelcomePackItems)
            {
                if (wi == null || wi.Id == 0) continue;
                if (Assets.find(EAssetType.ITEM, wi.Id) == null) continue;
                int amt = wi.Amount < 1 ? 1 : wi.Amount;
                for (int i = 0; i < amt; i++)
                {
                    Item item = new Item(wi.Id, true);
                    if (!p.inventory.tryAddItem(item, true))
                        ItemManager.dropItem(item, p.transform.position, true, true, true);
                }
            }
        }

        // ---- chat ----

        public static void Say(Player player, Message msg, long coins = 0, int count = 0)
        {
            if (player == null || msg == null || string.IsNullOrEmpty(msg.Text)) return;
            string text = msg.Text.Replace("{coins}", coins.ToString()).Replace("{count}", count.ToString());
            Color color = UnturnedChat.GetColorFromName(msg.Color, Color.white);
            ChatManager.serverSendMessage(text, color, null, player.channel.owner, EChatMode.SAY, null, true);
        }
    }
}
