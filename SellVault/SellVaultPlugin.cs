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
        private readonly Dictionary<string, float> _lastProcess = new Dictionary<string, float>();
        private HashSet<ushort> _noCommissionIds = new HashSet<ushort>();
        private sealed class VirtualVault { public string Key; public Transform Drop; public InteractableStorage Storage; }
        private readonly Dictionary<ulong, VirtualVault> _virtualVaults = new Dictionary<ulong, VirtualVault>();
        private const float ProcessCooldown = 2f;
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
            _noCommissionIds = new HashSet<ushort>(Configuration.Instance.NoCommissionItemIds ?? new ushort[0]);

            U.Events.OnPlayerConnected += OnConnected;
            U.Events.OnPlayerDisconnected += OnDisconnected;
            Rocket.Unturned.Events.UnturnedPlayerEvents.OnPlayerUpdateStat += OnAnyPlayerStat;
            foreach (SteamPlayer sp in Provider.clients)
                if (sp?.player != null) AttachComponent(sp.player);

            Logger.Log("SellVault loaded. Market=" + _prices.Count + " items, SellBoxes=" + _boxKeys.Count +
                       ", Commission=" + Configuration.Instance.BaseCommissionPercent + "%.");
        }

        protected override void Unload()
        {
            U.Events.OnPlayerConnected -= OnConnected;
            U.Events.OnPlayerDisconnected -= OnDisconnected;
            Rocket.Unturned.Events.UnturnedPlayerEvents.OnPlayerUpdateStat -= OnAnyPlayerStat;
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
            string source = storage != null ? "open" : "raycast";
            if (storage == null) storage = RaycastStorage(p, 8f, out source);
            if (storage == null)
            {
                SayFallback(p, cfg.MsgNeedOpenBox,
                    "เล็งไปที่กล่อง storage ระยะ 8m แล้วพิมพ์ /setsellbox (raycast: " + source + ")");
                return;
            }

            string key = PosKey(storage.transform.position);
            Logger.Log("[SellVault] SetSellBox source=" + source + " key=" + key + " pos=" + storage.transform.position);
            bool isNew = _boxKeys.Add(key);
            ulong by = up.CSteamID.m_SteamID;
            ThreadPool.QueueUserWorkItem(_ => Database?.AddSellBox(key, by));
            SayFallback(p, cfg.MsgSellBoxSet,
                (isNew ? "✅ ตั้งกล่องนี้เป็นกล่องขายแล้ว" : "✅ กล่องนี้เป็นกล่องขายอยู่แล้ว") + " (" + source + " @ " + key + ")");
        }

        private static InteractableStorage RaycastStorage(Player p, float distance, out string info)
        {
            info = "no-aim";
            Transform aim = p.look != null ? p.look.aim : null;
            if (aim == null) return null;

            if (!Physics.Raycast(aim.position, aim.forward, out RaycastHit hit, distance, RayMasks.BARRICADE_INTERACT))
            {
                if (!Physics.Raycast(aim.position, aim.forward, out hit, distance))
                { info = "miss"; return null; }
                info = "hit:" + (hit.transform != null ? hit.transform.name : "?") + " (wrong layer)";
                Transform t0 = hit.transform;
                InteractableStorage s0 = t0?.GetComponent<InteractableStorage>() ?? t0?.GetComponentInParent<InteractableStorage>();
                if (s0 != null) { info = "fallback-hit"; return s0; }
                return null;
            }

            Transform t = hit.transform;
            if (t == null) { info = "hit-null"; return null; }
            InteractableStorage s = t.GetComponent<InteractableStorage>() ?? t.GetComponentInParent<InteractableStorage>();
            info = s != null ? "ok" : "not-storage:" + t.name;
            return s;
        }

        private static void SayFallback(Player player, Message msg, string fallback)
        {
            if (player == null) return;
            string text = (msg != null && !string.IsNullOrEmpty(msg.Text)) ? msg.Text : fallback;
            string colorName = msg != null && !string.IsNullOrEmpty(msg.Color) ? msg.Color : "yellow";
            Color color = UnturnedChat.GetColorFromName(colorName, Color.yellow);
            ChatManager.serverSendMessage(text, color, null, player.channel.owner, EChatMode.SAY, null, true);
        }

        // ---- activity rewards ----

        private readonly Dictionary<ulong, float> _lastPvpReward = new Dictionary<ulong, float>();

        private void OnAnyPlayerStat(UnturnedPlayer up, EPlayerStat stat)
        {
            if (up?.Player == null) return;
            HandlePlayerStat(up.CSteamID.m_SteamID, stat);
        }

        public void HandlePlayerStat(ulong steamId, EPlayerStat stat)
        {
            if (Database == null) return;
            SellVaultConfiguration cfg = Configuration.Instance;
            long coins; string kind;

            switch (stat)
            {
                case EPlayerStat.KILLS_ZOMBIES_NORMAL: coins = cfg.ActivityZombieKill; kind = "zombie"; break;
                case EPlayerStat.KILLS_ZOMBIES_MEGA:   coins = cfg.ActivityMegaZombieKill; kind = "mega"; break;
                case EPlayerStat.KILLS_PLAYERS:        coins = cfg.ActivityPlayerKill; kind = "pvp"; break;
                case EPlayerStat.KILLS_ANIMALS:        coins = cfg.ActivityAnimalKill; kind = "animal"; break;
                case EPlayerStat.FOUND_BUILDABLES:     coins = cfg.ActivityBuildPlaced; kind = "build"; break;
                case EPlayerStat.FOUND_RESOURCES:      coins = cfg.ActivityResourceHarvested; kind = "resource"; break;
                default: return;
            }
            if (coins <= 0) return;

            if (kind == "pvp" && cfg.PvpRewardCooldownSeconds > 0)
            {
                float now = Time.realtimeSinceStartup;
                if (_lastPvpReward.TryGetValue(steamId, out float last) && now - last < cfg.PvpRewardCooldownSeconds)
                    return;
                _lastPvpReward[steamId] = now;
            }

            GrantActivity(steamId, kind, coins);
        }

        public void GrantActivity(ulong steamId, string kind, long coins)
        {
            if (Database == null || coins <= 0) return;
            ThreadPool.QueueUserWorkItem(_ =>
            {
                SellDatabase db = Database;
                if (db == null) return;
                db.AddCoins(steamId, coins);
                db.LogActivity(steamId, kind, coins);
            });
        }

        public void GrantOnlineReward(ulong steamId, long coins)
        {
            if (Database == null || coins <= 0) return;
            SellVaultConfiguration cfg = Configuration.Instance;
            ThreadPool.QueueUserWorkItem(_ =>
            {
                SellDatabase db = Database;
                if (db == null) return;
                db.AddCoins(steamId, coins);
                db.LogActivity(steamId, "online", coins);
                if (!cfg.NotifyOnlineReward) return;
                Enqueue(() =>
                {
                    Player p = PlayerTool.getPlayer(new Steamworks.CSteamID(steamId));
                    if (p != null) Say(p, cfg.MsgOnlineReward, coins);
                });
            });
        }

        public void OpenSellVault(UnturnedPlayer up)
        {
            Player p = up.Player;
            if (p == null) return;
            SellVaultConfiguration cfg = Configuration.Instance;

            if (p.movement == null || !p.movement.isSafe)
            { SayFallback(p, cfg.MsgNotInSafeZone, "ใช้ /sell ได้ที่ Safe Zone เท่านั้น"); return; }

            ushort id = cfg.SellVaultStorageId == 0 ? (ushort)328 : cfg.SellVaultStorageId;
            ItemBarricadeAsset asset = Assets.find(EAssetType.ITEM, id) as ItemBarricadeAsset;
            if (asset == null)
            { SayFallback(p, cfg.MsgVaultOpenFailed, "vault asset id=" + id + " ไม่พบ"); return; }

            // spawn just under the player's feet so they're in interaction range but barricade is hidden by terrain/floor
            Vector3 pos = p.transform.position + new Vector3(0f, -2f, 0f);
            Barricade barricade = new Barricade(asset);
            Transform t;
            try
            {
                t = BarricadeManager.dropNonPlantedBarricade(barricade, pos, Quaternion.identity,
                    up.CSteamID.m_SteamID, 0);
            }
            catch (Exception ex)
            { Logger.LogException(ex, "[SellVault] dropNonPlantedBarricade"); SayFallback(p, cfg.MsgVaultOpenFailed, "spawn failed"); return; }

            if (t == null)
            { SayFallback(p, cfg.MsgVaultOpenFailed, "drop returned null"); return; }

            InteractableStorage storage = t.GetComponent<InteractableStorage>()
                ?? t.GetComponentInParent<InteractableStorage>();
            if (storage == null)
            { Logger.LogWarning("[SellVault] spawned barricade has no InteractableStorage"); SayFallback(p, cfg.MsgVaultOpenFailed, "no-storage-component"); SafeDestroyBarricade(t); return; }

            string key = PosKey(storage.transform.position);
            ulong steamId = up.CSteamID.m_SteamID;
            _virtualVaults[steamId] = new VirtualVault { Key = key, Drop = t, Storage = storage };
            Logger.Log("[SellVault] OpenSellVault key=" + key + " for " + steamId);

            try { p.inventory.openStorage(storage); }
            catch (Exception ex)
            {
                Logger.LogException(ex, "[SellVault] openStorage");
                _virtualVaults.Remove(steamId);
                SafeDestroyBarricade(t);
                SayFallback(p, cfg.MsgVaultOpenFailed, "openStorage threw");
            }
        }

        public InteractableStorage TryGetVirtualVaultStorage(ulong steamId)
        {
            return _virtualVaults.TryGetValue(steamId, out VirtualVault v) ? v.Storage : null;
        }

        private void DestroyVirtualVault(ulong steamId)
        {
            if (!_virtualVaults.TryGetValue(steamId, out VirtualVault v)) return;
            _virtualVaults.Remove(steamId);
            try { SafeDestroyBarricade(v.Drop); }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] destroyBarricade"); }
        }

        private static void SafeDestroyBarricade(Transform t)
        {
            if (t == null) return;
            if (!BarricadeManager.tryGetInfo(t, out byte x, out byte y, out ushort plant, out ushort _index,
                    out BarricadeRegion _region, out BarricadeDrop drop))
                return;
            BarricadeManager.destroyBarricade(drop, x, y, plant);
        }

        public InteractableStorage FindNearbySellBox(Vector3 playerPos, float maxDistance)
        {
            if (_boxKeys == null || _boxKeys.Count == 0) return null;
            float bestSqr = maxDistance * maxDistance;
            InteractableStorage best = null;

            foreach (BarricadeRegion region in BarricadeManager.regions)
            {
                if (region?.drops == null) continue;
                foreach (BarricadeDrop drop in region.drops)
                {
                    InteractableStorage s = drop?.interactable as InteractableStorage;
                    if (s == null) continue;
                    Vector3 pos = s.transform.position;
                    float sqr = (pos - playerPos).sqrMagnitude;
                    if (sqr > bestSqr) continue;
                    if (!_boxKeys.Contains(PosKey(pos))) continue;
                    bestSqr = sqr;
                    best = s;
                }
            }
            return best;
        }

        public void OnStorageClosed(Player player, InteractableStorage storage)
        {
            if (player == null || storage == null || _boxKeys == null) return;
            string key = PosKey(storage.transform.position);
            ulong steamId = player.channel.owner.playerID.steamID.m_SteamID;
            bool isVirtual = _virtualVaults.TryGetValue(steamId, out VirtualVault vv) && vv.Key == key;
            if (!isVirtual && !_boxKeys.Contains(key)) return;

            // skip empty box — avoids clearing items the player is currently placing
            int itemCount = storage.items?.items?.Count ?? 0;
            if (itemCount == 0)
            {
                Logger.Log("[SellVault] skip empty key=" + key);
                return;
            }

            // per-box cooldown — server emits resize 0x0 multiple times during use
            float now = Time.realtimeSinceStartup;
            if (_lastProcess.TryGetValue(key, out float last) && now - last < ProcessCooldown)
            {
                Logger.Log("[SellVault] cooldown key=" + key + " dt=" + (now - last).ToString("0.00"));
                return;
            }
            _lastProcess[key] = now;

            Logger.Log("[SellVault] PROCESS key=" + key + " items=" + itemCount + " virtual=" + isVirtual);
            ProcessSellBox(player, storage);
            if (isVirtual) DestroyVirtualVault(steamId);
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
                    long pay = _noCommissionIds.Contains(id) ? (long)Math.Floor(price * amt) : Payout(price, amt, cfg);
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
                    _noCommissionIds = new HashSet<ushort>(Configuration.Instance.NoCommissionItemIds ?? new ushort[0]);
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
