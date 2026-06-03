using System;
using System.Collections.Generic;
using System.Net.Http;
using System.Text;
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
        private float _nextPriceRefreshAt;
        private int _priceRefreshInFlight;
        private List<QuestDef> _activeQuests = new List<QuestDef>();

        // single reusable client for the shop API (socket reuse; one instance is the documented pattern)
        private static readonly HttpClient _http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };

        private struct SaleEntry { public ushort Id; public int Amount; public long Coins; }

        // Per-player coins-sold-today tally (in-memory; seeded from market_log on connect, reset at midnight).
        // Backs the MaxDailySellCoins cap so the limit check is instant on the main thread.
        private sealed class DailyTally { public string Day; public long Coins; }
        private readonly Dictionary<ulong, DailyTally> _dailySold = new Dictionary<ulong, DailyTally>();

        protected override void Load()
        {
            Instance = this;
            DatabaseSection db = Configuration.Instance.Database;
            Database = new SellDatabase(db.ConnectionString, db.TablePrefix);
            _prices = Database.LoadMarketPrices();
            _boxKeys = new HashSet<string>(Database.LoadSellBoxKeys());
            _noCommissionIds = new HashSet<ushort>(Configuration.Instance.NoCommissionItemIds ?? new ushort[0]);
            if (Configuration.Instance.QuestCheckEnabled)
            {
                try { _activeQuests = Database.LoadActiveQuests(); }
                catch (Exception ex) { Logger.LogException(ex, "[SellVault] initial LoadActiveQuests"); }
            }

            U.Events.OnPlayerConnected += OnConnected;
            U.Events.OnPlayerDisconnected += OnDisconnected;
            Rocket.Unturned.Events.UnturnedPlayerEvents.OnPlayerUpdateStat += OnAnyPlayerStat;
            foreach (SteamPlayer sp in Provider.clients)
                if (sp?.player != null) { AttachComponent(sp.player); SeedDailyTally(sp.playerID.steamID.m_SteamID); }

            Logger.Log("SellVault loaded. Market=" + _prices.Count + " items, SellBoxes=" + _boxKeys.Count +
                       ", Commission=" + Configuration.Instance.BaseCommissionPercent + "%.");

            float refresh = Configuration.Instance.PriceRefreshIntervalSeconds;
            if (refresh > 0f)
            {
                _nextPriceRefreshAt = Time.realtimeSinceStartup + refresh;
                Logger.Log("[SellVault] Price auto-refresh every " + refresh.ToString("0.##") + "s.");
            }
            else
            {
                Logger.Log("[SellVault] Price auto-refresh disabled (use /sellreload to refresh).");
            }
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

        private void OnConnected(UnturnedPlayer p) { AttachComponent(p.Player); SeedDailyTally(p.CSteamID.m_SteamID); }

        private void OnDisconnected(UnturnedPlayer p)
        {
            SellVaultComponent c = p.Player?.gameObject.GetComponent<SellVaultComponent>();
            if (c != null) { _components.Remove(c); UnityEngine.Object.Destroy(c); }
            _dailySold.Remove(p.CSteamID.m_SteamID);
        }

        // ---- daily sell cap ----

        private static string DayKey(DateTime now) => now.ToString("yyyy-MM-dd");

        /// <summary>Background-load today's sold-coin total for a player into the in-memory tally.</summary>
        private void SeedDailyTally(ulong steamId)
        {
            if (Configuration.Instance.MaxDailySellCoins <= 0) return;
            ThreadPool.QueueUserWorkItem(_ =>
            {
                SellDatabase db = Database;
                if (db == null) return;
                long coins = db.GetTodaySellCoins(steamId, DateTime.Today);
                string day = DayKey(DateTime.Now);
                Enqueue(() =>
                {
                    // Don't clobber a sale already tallied this session today (its log write may not be
                    // committed yet) — keep the higher of the DB total and the in-memory total.
                    if (_dailySold.TryGetValue(steamId, out DailyTally ex) && ex.Day == day)
                        ex.Coins = Math.Max(ex.Coins, coins);
                    else
                        _dailySold[steamId] = new DailyTally { Day = day, Coins = coins };
                });
            });
        }

        /// <summary>Coins this player has earned from selling today (0 if not seeded / new day).</summary>
        private long GetTallyCoins(ulong steamId)
        {
            return _dailySold.TryGetValue(steamId, out DailyTally t) && t.Day == DayKey(DateTime.Now) ? t.Coins : 0;
        }

        private void AddTallyCoins(ulong steamId, long delta)
        {
            string today = DayKey(DateTime.Now);
            if (!_dailySold.TryGetValue(steamId, out DailyTally t) || t.Day != today)
            {
                t = new DailyTally { Day = today, Coins = 0 };
                _dailySold[steamId] = t;
            }
            t.Coins += delta;
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

            TickPriceRefresh();
        }

        private void TickPriceRefresh()
        {
            float interval = Configuration?.Instance?.PriceRefreshIntervalSeconds ?? 0f;
            if (interval <= 0f) return;
            float now = Time.realtimeSinceStartup;
            if (now < _nextPriceRefreshAt) return;
            _nextPriceRefreshAt = now + interval;

            if (Interlocked.CompareExchange(ref _priceRefreshInFlight, 1, 0) != 0) return;
            bool questCheck = Configuration?.Instance?.QuestCheckEnabled ?? false;
            ThreadPool.QueueUserWorkItem(_ =>
            {
                try
                {
                    SellDatabase db = Database;
                    if (db == null) return;
                    Dictionary<ushort, double> prices = db.LoadMarketPrices();
                    List<QuestDef> quests = questCheck ? db.LoadActiveQuests() : new List<QuestDef>();
                    Enqueue(() => { _prices = prices; _activeQuests = quests; });
                }
                catch (Exception ex)
                {
                    Logger.LogException(ex, "[SellVault] auto price refresh");
                }
                finally
                {
                    Interlocked.Exchange(ref _priceRefreshInFlight, 0);
                }
            });
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
            List<ItemJar> marketJars = new List<ItemJar>();
            List<ItemJar> nonMarketJars = new List<ItemJar>();

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
                    marketJars.Add(jar);
                    log.Add(new SaleEntry { Id = id, Amount = amt, Coins = pay });
                }
                else
                {
                    nonMarketJars.Add(jar);
                }
            }

            // Daily sell-coin cap (0 = disabled). All-or-nothing: if this box would push the player
            // over the cap, sell nothing and return everything (market + non-market) untouched.
            if (cfg.MaxDailySellCoins > 0 && sold > 0)
            {
                long soldToday = GetTallyCoins(steamId);
                if (soldToday + total > cfg.MaxDailySellCoins)
                {
                    foreach (ItemJar jar in marketJars)
                        if (!player.inventory.tryAddItem(jar.item, true))
                            ItemManager.dropItem(jar.item, pos, true, true, true);
                    foreach (ItemJar jar in nonMarketJars)
                        if (!player.inventory.tryAddItem(jar.item, true))
                            ItemManager.dropItem(jar.item, pos, true, true, true);
                    v.clear();
                    SayLimit(player, cfg.MsgDailyLimitReached, soldToday, cfg.MaxDailySellCoins);
                    return;
                }
            }

            // Within the cap: return non-market items, sell the rest.
            foreach (ItemJar jar in nonMarketJars)
            {
                if (!player.inventory.tryAddItem(jar.item, true))
                    ItemManager.dropItem(jar.item, pos, true, true, true);
                returned++;
            }

            v.clear();

            if (sold > 0)
                AddTallyCoins(steamId, total);

            if (sold > 0)
            {
                Say(player, cfg.MsgSold, total, sold);
                long credit = total;
                bool questCheck = cfg.QuestCheckEnabled;
                List<QuestDef> quests = questCheck ? _activeQuests : null;
                ThreadPool.QueueUserWorkItem(_ =>
                {
                    SellDatabase db = Database;
                    if (db == null) return;
                    db.AddCoins(steamId, credit);
                    foreach (SaleEntry e in log)
                    {
                        db.AddStock(e.Id, e.Amount);
                        db.LogSale(steamId, e.Id, e.Amount, e.Coins);
                        if (!questCheck || quests == null || quests.Count == 0) continue;
                        List<QuestCompletion> done = db.RecordSaleForQuests(steamId, e.Id, e.Amount, quests);
                        if (done == null || done.Count == 0) continue;
                        foreach (QuestCompletion qc in done)
                        {
                            db.AddCoins(steamId, qc.RewardCoins);
                            db.LogActivity(steamId, "quest", qc.RewardCoins);
                            NotifyQuestComplete(steamId, qc);
                        }
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

        // ---- web shop / PIN ----

        /// <summary>
        /// <c>/shop</c> - shows the player their personal web login link plus a /shoppin hint.
        /// Pure string + chat; no DB or network, so it runs synchronously on the game thread.
        /// Works for unlinked players (the link only needs their SteamID).
        /// </summary>
        public void ShowShopLink(UnturnedPlayer up)
        {
            Player player = up.Player;
            if (player == null) return;
            SellVaultConfiguration cfg = Configuration.Instance;

            string baseUrl = cfg.WebShopLoginUrl ?? "https://meowpow.shop/login";
            string url = baseUrl + "?id=" + up.CSteamID.m_SteamID;

            // Ask the client to open the link (Steam-overlay browser, with a confirm prompt).
            // This is the same vanilla server->client RPC RichMessageAnnouncer uses; no asset needed.
            // Fall back to a hard-coded bilingual prompt if the config field isn't in the live XML.
            string prompt = (cfg.MsgShopBrowserPrompt != null && !string.IsNullOrEmpty(cfg.MsgShopBrowserPrompt.Text))
                ? cfg.MsgShopBrowserPrompt.Text
                : "เปิดหน้าเว็บร้านค้า? | Open the web shop?";
            try { player.sendBrowserRequest(prompt, url); }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] /shop sendBrowserRequest"); }

            // Keep the chat link + PIN hint as a fallback (overlay disabled / prompt dismissed).
            SayUrl(player, cfg.MsgShopLink, url);
            Say(player, cfg.MsgShopPinHint);
        }

        // Hard-coded Discord invite default so /discord works without a live-config XML edit.
        private const string DefaultDiscordUrl = "https://discord.gg/45c4XChMWN";

        /// <summary>
        /// <c>/discord</c> (alias <c>/dc</c>) - opens the server's Discord invite via the client
        /// browser-request prompt, and chat-prints it as a fallback. Static URL (no SteamID).
        /// Mirrors <see cref="ShowShopLink"/>. Runs on the game/main thread.
        /// </summary>
        public void ShowDiscordLink(UnturnedPlayer up)
        {
            Player player = up.Player;
            if (player == null) return;
            SellVaultConfiguration cfg = Configuration.Instance;

            string url = string.IsNullOrEmpty(cfg.DiscordUrl) ? DefaultDiscordUrl : cfg.DiscordUrl;

            string prompt = (cfg.MsgDiscordPrompt != null && !string.IsNullOrEmpty(cfg.MsgDiscordPrompt.Text))
                ? cfg.MsgDiscordPrompt.Text
                : "เข้าดิสคอร์ดเซิร์ฟเวอร์? | Join our Discord?";
            try { player.sendBrowserRequest(prompt, url); }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] /discord sendBrowserRequest"); }

            // Chat fallback (overlay disabled / prompt dismissed).
            SayUrl(player, cfg.MsgDiscordLink, url);
        }

        /// <summary>
        /// <c>/shoppin &lt;pin&gt;</c> - posts the player's SteamID + 6-digit PIN to the shop API
        /// (X-Bot-Secret header) so the PIN can be hashed server-side. The PIN must already be
        /// validated as 6 digits by the caller. DB/network on a background thread, chat reply via
        /// Enqueue, mirroring <see cref="LinkAccount"/>.
        /// </summary>
        public void SetWebPin(UnturnedPlayer up, string pin)
        {
            Player player = up.Player;
            if (player == null) return;
            ulong steamId = up.CSteamID.m_SteamID;
            SellVaultConfiguration cfg = Configuration.Instance;
            ApiSection api = cfg.Api;

            ThreadPool.QueueUserWorkItem(_ =>
            {
                bool ok = false;
                try
                {
                    if (api == null || string.IsNullOrEmpty(api.BaseUrl))
                    {
                        Logger.LogWarning("[SellVault] /shoppin: Api.BaseUrl not configured.");
                    }
                    else
                    {
                        string url = api.BaseUrl.TrimEnd('/') + "/bot/steam-pin";
                        string body = "{\"steam_id\":\"" + steamId + "\",\"pin\":\"" + pin + "\"}";
                        using (HttpRequestMessage req = new HttpRequestMessage(HttpMethod.Post, url))
                        {
                            req.Content = new StringContent(body, Encoding.UTF8, "application/json");
                            if (!string.IsNullOrEmpty(api.BotSecret))
                                req.Headers.Add("X-Bot-Secret", api.BotSecret);
                            // Skip ngrok's browser-warning interstitial so a stray HTML page can't
                            // come back as a 2xx and masquerade as a successful PIN set.
                            req.Headers.Add("ngrok-skip-browser-warning", "true");

                            // blocking on this background thread by design (no async path in commands)
                            using (HttpResponseMessage resp = _http.SendAsync(req).GetAwaiter().GetResult())
                            {
                                // Require both a 2xx AND a JSON content type: the web/Firebase host
                                // SPA-rewrites unknown POSTs to 200 + index.html (text/html), which
                                // would otherwise look like success. The real API returns JSON.
                                string mediaType = resp.Content?.Headers?.ContentType?.MediaType ?? "";
                                bool isJson = mediaType.IndexOf("json", StringComparison.OrdinalIgnoreCase) >= 0;
                                ok = resp.IsSuccessStatusCode && isJson;
                                if (!ok)
                                    Logger.LogWarning("[SellVault] /shoppin POST not accepted (status="
                                                      + (int)resp.StatusCode + ", contentType=\"" + mediaType
                                                      + "\") for steam " + steamId + ".");
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    Logger.LogException(ex, "[SellVault] /shoppin POST failed for steam " + steamId);
                    ok = false;
                }

                bool success = ok;
                Enqueue(() =>
                {
                    Player p = PlayerTool.getPlayer(new CSteamID(steamId));
                    if (p == null) return;
                    Say(p, success ? cfg.MsgPinSet : cfg.MsgPinFailed);
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

        /// <summary>Like <see cref="Say"/> but substitutes the {url} placeholder (for /shop).</summary>
        public static void SayUrl(Player player, Message msg, string url)
        {
            if (player == null || msg == null || string.IsNullOrEmpty(msg.Text)) return;
            string text = msg.Text.Replace("{url}", url ?? "");
            Color color = UnturnedChat.GetColorFromName(msg.Color, Color.white);
            ChatManager.serverSendMessage(text, color, null, player.channel.owner, EChatMode.SAY, null, true);
        }

        /// <summary>Daily-cap message with {sold}/{limit} placeholders. Falls back to a built-in bilingual
        /// string if the field isn't present in the live config XML (so older configs still inform the player).</summary>
        private static void SayLimit(Player player, Message msg, long sold, long limit)
        {
            if (player == null) return;
            string template = (msg != null && !string.IsNullOrEmpty(msg.Text))
                ? msg.Text
                : "⛔ ขายเกินลิมิตวันนี้ (วันละ {limit}, วันนี้ {sold}) — คืนของแล้ว | Daily sell limit reached ({limit}/day, {sold} today) — items returned";
            string colorName = (msg != null && !string.IsNullOrEmpty(msg.Color)) ? msg.Color : "red";
            string text = template.Replace("{sold}", sold.ToString()).Replace("{limit}", limit.ToString());
            Color color = UnturnedChat.GetColorFromName(colorName, Color.red);
            ChatManager.serverSendMessage(text, color, null, player.channel.owner, EChatMode.SAY, null, true);
        }

        private void NotifyQuestComplete(ulong steamId, QuestCompletion qc)
        {
            Message msg = Configuration?.Instance?.MessageQuestComplete;
            if (msg == null || string.IsNullOrEmpty(msg.Text)) return;
            string text = msg.Text
                .Replace("{name}", qc.Name ?? "")
                .Replace("{reward_coins}", qc.RewardCoins.ToString());
            string colorName = msg.Color;
            Enqueue(() =>
            {
                Player p = PlayerTool.getPlayer(new CSteamID(steamId));
                if (p == null) return;
                Color color = UnturnedChat.GetColorFromName(colorName, Color.green);
                ChatManager.serverSendMessage(text, color, null, p.channel.owner, EChatMode.SAY, null, true);
            });
        }
    }
}
