using System;
using System.Collections.Generic;
using System.Text;
using MySql.Data.MySqlClient;
using Logger = Rocket.Core.Logging.Logger;

namespace SellVault
{
    public enum LinkResult { Linked, CodeInvalid, SteamAlreadyLinked, DiscordAlreadyLinked, Error }

    /// <summary>Reset cadence — must match the API's reset_type column ('once'|'daily'|'weekly').</summary>
    public enum QuestReset { Once, Daily, Weekly }

    public sealed class QuestDef
    {
        public int Id;
        public string Name;
        public long RewardCoins;
        public QuestReset Reset;
        public Dictionary<ushort, int> Items = new Dictionary<ushort, int>();
    }

    /// <summary>Result of recording a sale against quests — used to fire chat notifications on the main thread.</summary>
    public sealed class QuestCompletion
    {
        public int QuestId;
        public string Name;
        public long RewardCoins;
    }

    /// <summary>
    /// MySQL storage for SellVault, sharing the unified market model with the Discord bot.
    /// Connections opened per call; everything here runs on a background thread (never touch the
    /// Unturned API from this class).
    ///   &lt;p&gt;market      (item_id, name, price, amount[stock], image_url, enabled)
    ///   &lt;p&gt;coins       (steam_id, balance)
    ///   &lt;p&gt;market_log  (id, steam_id, item_id, amount, coins, kind, at)
    ///   &lt;p&gt;sellboxes   (pos_key, added_by)
    ///   &lt;p&gt;links / &lt;p&gt;link_codes  (Discord linking)
    /// </summary>
    public sealed class SellDatabase
    {
        private readonly string _conn;
        private readonly string _market;
        private readonly string _coins;
        private readonly string _log;
        private readonly string _boxes;
        private readonly string _links;
        private readonly string _linkCodes;
        private readonly string _activity;
        private readonly string _quests;
        private readonly string _questItems;
        private readonly string _questProgress;
        private readonly string _questCompletions;

        public SellDatabase(string connectionString, string tablePrefix)
        {
            _conn = connectionString;
            string p = Sanitize(tablePrefix);
            _market = p + "market";
            _coins = p + "coins";
            _log = p + "market_log";
            _boxes = p + "sellboxes";
            _links = p + "links";
            _linkCodes = p + "link_codes";
            _activity = p + "activity_log";
            _quests = p + "quests";
            _questItems = p + "quest_items";
            _questProgress = p + "quest_progress";
            _questCompletions = p + "quest_completions";
            EnsureSchema();
        }

        private static string Sanitize(string raw)
        {
            if (string.IsNullOrEmpty(raw)) return "sv_";
            StringBuilder sb = new StringBuilder();
            foreach (char c in raw) if (char.IsLetterOrDigit(c) || c == '_') sb.Append(c);
            return sb.Length == 0 ? "sv_" : sb.ToString();
        }

        private void EnsureSchema()
        {
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    Exec(c, "CREATE TABLE IF NOT EXISTS `" + _market + "` ("
                        + "`item_id` INT UNSIGNED PRIMARY KEY,`name` VARCHAR(64) NOT NULL,"
                        + "`price` DOUBLE NOT NULL DEFAULT 0,`amount` INT NOT NULL DEFAULT 0,"
                        + "`image_url` VARCHAR(512) NULL,`enabled` TINYINT(1) NOT NULL DEFAULT 1"
                        + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;");
                    Exec(c, "CREATE TABLE IF NOT EXISTS `" + _coins + "` ("
                        + "`steam_id` BIGINT UNSIGNED PRIMARY KEY,`balance` BIGINT NOT NULL DEFAULT 0"
                        + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;");
                    Exec(c, "CREATE TABLE IF NOT EXISTS `" + _log + "` ("
                        + "`id` INT AUTO_INCREMENT PRIMARY KEY,`steam_id` BIGINT UNSIGNED NOT NULL,"
                        + "`item_id` INT UNSIGNED NOT NULL,`amount` INT NOT NULL,`coins` BIGINT NOT NULL,"
                        + "`kind` VARCHAR(8) NOT NULL,`at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                        + "INDEX `idx_steam` (`steam_id`)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;");
                    Exec(c, "CREATE TABLE IF NOT EXISTS `" + _boxes + "` ("
                        + "`pos_key` VARCHAR(40) PRIMARY KEY,`added_by` BIGINT UNSIGNED NOT NULL DEFAULT 0,"
                        + "`added_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                        + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;");
                    Exec(c, "CREATE TABLE IF NOT EXISTS `" + _links + "` ("
                        + "`steam_id` BIGINT UNSIGNED PRIMARY KEY,`discord_id` BIGINT UNSIGNED NOT NULL UNIQUE,"
                        + "`linked_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                        + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;");
                    Exec(c, "CREATE TABLE IF NOT EXISTS `" + _linkCodes + "` ("
                        + "`code` VARCHAR(32) PRIMARY KEY,`discord_id` BIGINT UNSIGNED NOT NULL,"
                        + "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                        + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;");
                    Exec(c, "CREATE TABLE IF NOT EXISTS `" + _activity + "` ("
                        + "`id` INT AUTO_INCREMENT PRIMARY KEY,`steam_id` BIGINT UNSIGNED NOT NULL,"
                        + "`kind` VARCHAR(24) NOT NULL,`coins` BIGINT NOT NULL,"
                        + "`at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                        + "INDEX `idx_steam` (`steam_id`),INDEX `idx_kind` (`kind`)"
                        + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;");
                }
                Logger.Log("[SellVault] MySQL tables ready.");
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] EnsureSchema failed"); }
        }

        private static void Exec(MySqlConnection c, string sql)
        {
            using (MySqlCommand cmd = new MySqlCommand(sql, c)) cmd.ExecuteNonQuery();
        }

        /// <summary>Loads enabled market items as item_id -> base price (for in-game selling).</summary>
        public Dictionary<ushort, double> LoadMarketPrices()
        {
            Dictionary<ushort, double> map = new Dictionary<ushort, double>();
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand(
                        "SELECT `item_id`,`price` FROM `" + _market + "` WHERE `enabled`=1;", c))
                    using (MySqlDataReader r = cmd.ExecuteReader())
                        while (r.Read())
                            map[(ushort)Convert.ToUInt32(r["item_id"])] = Convert.ToDouble(r["price"]);
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] LoadMarketPrices failed"); }
            return map;
        }

        /// <summary>Increase (or decrease) stock for a market item after a sale.</summary>
        public void AddStock(ushort itemId, int delta)
        {
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand(
                        "UPDATE `" + _market + "` SET `amount` = `amount` + @d WHERE `item_id`=@i;", c))
                    {
                        cmd.Parameters.AddWithValue("@d", delta);
                        cmd.Parameters.AddWithValue("@i", itemId);
                        cmd.ExecuteNonQuery();
                    }
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] AddStock failed"); }
        }

        public long AddCoins(ulong steamId, long delta)
        {
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand(
                        "INSERT INTO `" + _coins + "` (steam_id, balance) VALUES (@s,@d) "
                        + "ON DUPLICATE KEY UPDATE balance = balance + @d;", c))
                    {
                        cmd.Parameters.AddWithValue("@s", steamId);
                        cmd.Parameters.AddWithValue("@d", delta);
                        cmd.ExecuteNonQuery();
                    }
                    return GetCoins(steamId);
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] AddCoins failed"); return -1; }
        }

        public long GetCoins(ulong steamId)
        {
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand(
                        "SELECT `balance` FROM `" + _coins + "` WHERE `steam_id`=@s LIMIT 1;", c))
                    {
                        cmd.Parameters.AddWithValue("@s", steamId);
                        object o = cmd.ExecuteScalar();
                        return o == null || o == DBNull.Value ? 0 : Convert.ToInt64(o);
                    }
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] GetCoins failed"); return -1; }
        }

        public void LogActivity(ulong steamId, string kind, long coins)
        {
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand(
                        "INSERT INTO `" + _activity + "` (steam_id,kind,coins) VALUES (@s,@k,@c);", c))
                    {
                        cmd.Parameters.AddWithValue("@s", steamId);
                        cmd.Parameters.AddWithValue("@k", kind ?? "?");
                        cmd.Parameters.AddWithValue("@c", coins);
                        cmd.ExecuteNonQuery();
                    }
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] LogActivity failed"); }
        }

        public void LogSale(ulong steamId, ushort itemId, int amount, long coins)
        {
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand(
                        "INSERT INTO `" + _log + "` (steam_id,item_id,amount,coins,kind) VALUES (@s,@i,@a,@c,'sell');", c))
                    {
                        cmd.Parameters.AddWithValue("@s", steamId);
                        cmd.Parameters.AddWithValue("@i", itemId);
                        cmd.Parameters.AddWithValue("@a", amount);
                        cmd.Parameters.AddWithValue("@c", coins);
                        cmd.ExecuteNonQuery();
                    }
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] LogSale failed"); }
        }

        /// <summary>
        /// Total coins this player has earned from SELLING since <paramref name="dayStart"/> (local midnight).
        /// Used to enforce the per-day sell cap; only kind='sell' rows count (activity/quest coins excluded).
        /// </summary>
        public long GetTodaySellCoins(ulong steamId, DateTime dayStart)
        {
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand(
                        "SELECT COALESCE(SUM(`coins`),0) FROM `" + _log + "` "
                        + "WHERE `steam_id`=@s AND `kind`='sell' AND `at` >= @start;", c))
                    {
                        cmd.Parameters.AddWithValue("@s", steamId);
                        cmd.Parameters.AddWithValue("@start", dayStart);
                        object o = cmd.ExecuteScalar();
                        return o == null || o == DBNull.Value ? 0 : Convert.ToInt64(o);
                    }
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] GetTodaySellCoins failed"); return 0; }
        }

        // ---- sell boxes ----

        public List<string> LoadSellBoxKeys()
        {
            List<string> keys = new List<string>();
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand("SELECT `pos_key` FROM `" + _boxes + "`;", c))
                    using (MySqlDataReader r = cmd.ExecuteReader())
                        while (r.Read()) keys.Add(Convert.ToString(r["pos_key"]));
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] LoadSellBoxKeys failed"); }
            return keys;
        }

        public void AddSellBox(string posKey, ulong by)
        {
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand(
                        "INSERT IGNORE INTO `" + _boxes + "` (pos_key, added_by) VALUES (@k,@b);", c))
                    {
                        cmd.Parameters.AddWithValue("@k", posKey);
                        cmd.Parameters.AddWithValue("@b", by);
                        cmd.ExecuteNonQuery();
                    }
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] AddSellBox failed"); }
        }

        public void RemoveSellBox(string posKey)
        {
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand("DELETE FROM `" + _boxes + "` WHERE pos_key=@k;", c))
                    {
                        cmd.Parameters.AddWithValue("@k", posKey);
                        cmd.ExecuteNonQuery();
                    }
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] RemoveSellBox failed"); }
        }

        // ---- linking (unchanged) ----

        public LinkResult ConsumeLinkCode(string code, ulong steamId, out ulong discordId)
        {
            discordId = 0;
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand(
                        "SELECT 1 FROM `" + _links + "` WHERE `steam_id`=@s LIMIT 1;", c))
                    {
                        cmd.Parameters.AddWithValue("@s", steamId);
                        if (cmd.ExecuteScalar() != null) return LinkResult.SteamAlreadyLinked;
                    }
                    ulong did;
                    using (MySqlCommand cmd = new MySqlCommand(
                        "SELECT `discord_id` FROM `" + _linkCodes + "` WHERE `code`=@c LIMIT 1;", c))
                    {
                        cmd.Parameters.AddWithValue("@c", code);
                        object o = cmd.ExecuteScalar();
                        if (o == null || o == DBNull.Value) return LinkResult.CodeInvalid;
                        did = Convert.ToUInt64(o);
                    }
                    using (MySqlCommand cmd = new MySqlCommand(
                        "SELECT 1 FROM `" + _links + "` WHERE `discord_id`=@d LIMIT 1;", c))
                    {
                        cmd.Parameters.AddWithValue("@d", did);
                        if (cmd.ExecuteScalar() != null) return LinkResult.DiscordAlreadyLinked;
                    }
                    using (MySqlCommand cmd = new MySqlCommand(
                        "INSERT INTO `" + _links + "` (steam_id, discord_id) VALUES (@s,@d);", c))
                    {
                        cmd.Parameters.AddWithValue("@s", steamId);
                        cmd.Parameters.AddWithValue("@d", did);
                        cmd.ExecuteNonQuery();
                    }
                    using (MySqlCommand cmd = new MySqlCommand(
                        "DELETE FROM `" + _linkCodes + "` WHERE `code`=@c;", c))
                    {
                        cmd.Parameters.AddWithValue("@c", code);
                        cmd.ExecuteNonQuery();
                    }
                    discordId = did;
                    return LinkResult.Linked;
                }
            }
            catch (Exception ex)
            {
                Logger.LogException(ex, "[SellVault] ConsumeLinkCode failed");
                return LinkResult.Error;
            }
        }

        // ---- quests ----

        /// <summary>Load currently-active quest definitions (enabled + within start/end window) and their item requirements.</summary>
        public List<QuestDef> LoadActiveQuests()
        {
            Dictionary<int, QuestDef> byId = new Dictionary<int, QuestDef>();
            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    using (MySqlCommand cmd = new MySqlCommand(
                        "SELECT `id`,`name`,`reward_coins`,`reset_type` FROM `" + _quests + "` "
                        + "WHERE `enabled`=1 AND (`start_at` IS NULL OR `start_at`<=NOW()) "
                        + "AND (`end_at` IS NULL OR `end_at`>NOW());", c))
                    using (MySqlDataReader r = cmd.ExecuteReader())
                    {
                        while (r.Read())
                        {
                            QuestDef q = new QuestDef
                            {
                                Id = Convert.ToInt32(r["id"]),
                                Name = r["name"] == DBNull.Value ? "" : Convert.ToString(r["name"]),
                                RewardCoins = Convert.ToInt64(r["reward_coins"]),
                                Reset = ParseReset(Convert.ToString(r["reset_type"]))
                            };
                            byId[q.Id] = q;
                        }
                    }
                    if (byId.Count == 0) return new List<QuestDef>();

                    using (MySqlCommand cmd = new MySqlCommand(
                        "SELECT `quest_id`,`item_id`,`qty_required` FROM `" + _questItems + "` "
                        + "WHERE `quest_id` IN (" + string.Join(",", new List<int>(byId.Keys).ToArray()) + ");", c))
                    using (MySqlDataReader r = cmd.ExecuteReader())
                    {
                        while (r.Read())
                        {
                            int qid = Convert.ToInt32(r["quest_id"]);
                            if (!byId.TryGetValue(qid, out QuestDef q)) continue;
                            ushort iid = (ushort)Convert.ToUInt32(r["item_id"]);
                            int qty = Convert.ToInt32(r["qty_required"]);
                            q.Items[iid] = qty;
                        }
                    }
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] LoadActiveQuests failed"); }
            return new List<QuestDef>(byId.Values);
        }

        private static QuestReset ParseReset(string s)
        {
            if (string.IsNullOrEmpty(s)) return QuestReset.Once;
            switch (s.Trim().ToLowerInvariant())
            {
                case "daily": return QuestReset.Daily;
                case "weekly": return QuestReset.Weekly;
                default: return QuestReset.Once;
            }
        }

        /// <summary>
        /// Period key matching the API: 'lifetime' for once, yyyy-MM-dd for daily, yyyy-Www (ISO) for weekly.
        /// Uses local <see cref="DateTime.Now"/> like the rest of the plugin.
        /// </summary>
        public static string ComputePeriodKey(QuestReset reset, DateTime now)
        {
            switch (reset)
            {
                case QuestReset.Daily:
                    return now.ToString("yyyy-MM-dd");
                case QuestReset.Weekly:
                    return now.Year.ToString("0000") + "-W" + IsoWeek(now).ToString("00");
                default:
                    return "lifetime";
            }
        }

        // ISO 8601 week number (no System.Globalization.ISOWeek on net48 / Mono).
        private static int IsoWeek(DateTime date)
        {
            DayOfWeek dow = date.DayOfWeek;
            int delta = (int)dow == 0 ? -3 : 4 - (int)dow; // shift to Thursday of same ISO week
            DateTime thursday = date.Date.AddDays(delta);
            DateTime jan1 = new DateTime(thursday.Year, 1, 1);
            int days = (thursday - jan1).Days;
            return (days / 7) + 1;
        }

        /// <summary>
        /// For one sold item-id+amount, update progress on all matching active quests and detect completions.
        /// Returns the list of quests newly completed (winner of the completions PK race) — caller pays the reward.
        ///
        /// SQL per sold item (only for quests whose item_id matches):
        ///   INSERT INTO sv_quest_progress (...) VALUES (...) ON DUPLICATE KEY UPDATE sold_qty = sold_qty + @a;
        ///   SELECT 1 FROM sv_quest_completions WHERE quest_id=? AND steam_id=? AND period_key=? LIMIT 1;
        ///   (if not yet completed)
        ///   SELECT item_id, qty_required, COALESCE(sold_qty,0) FROM sv_quest_items
        ///     LEFT JOIN sv_quest_progress USING (quest_id, item_id) -- filtered to this steam+period
        ///     WHERE quest_id = ?;
        ///   (if all qty satisfied)
        ///   INSERT INTO sv_quest_completions (quest_id, steam_id, period_key) VALUES (?,?,?);
        ///     -- PK (quest_id,steam_id,period_key) lets us swallow duplicate-key races; affected-rows tells the winner.
        /// </summary>
        public List<QuestCompletion> RecordSaleForQuests(ulong steamId, ushort itemId, int amount, List<QuestDef> quests)
        {
            List<QuestCompletion> completed = new List<QuestCompletion>();
            if (quests == null || quests.Count == 0 || amount <= 0) return completed;
            DateTime now = DateTime.Now;

            try
            {
                using (MySqlConnection c = new MySqlConnection(_conn))
                {
                    c.Open();
                    foreach (QuestDef q in quests)
                    {
                        if (!q.Items.ContainsKey(itemId)) continue;
                        string period = ComputePeriodKey(q.Reset, now);

                        // 1) bump progress for this (quest, steam, item, period)
                        using (MySqlCommand cmd = new MySqlCommand(
                            "INSERT INTO `" + _questProgress + "` (quest_id, steam_id, item_id, period_key, sold_qty) "
                            + "VALUES (@q,@s,@i,@p,@a) "
                            + "ON DUPLICATE KEY UPDATE sold_qty = sold_qty + @a;", c))
                        {
                            cmd.Parameters.AddWithValue("@q", q.Id);
                            cmd.Parameters.AddWithValue("@s", steamId);
                            cmd.Parameters.AddWithValue("@i", itemId);
                            cmd.Parameters.AddWithValue("@p", period);
                            cmd.Parameters.AddWithValue("@a", amount);
                            cmd.ExecuteNonQuery();
                        }

                        // 2) skip if already completed this period
                        using (MySqlCommand cmd = new MySqlCommand(
                            "SELECT 1 FROM `" + _questCompletions + "` "
                            + "WHERE quest_id=@q AND steam_id=@s AND period_key=@p LIMIT 1;", c))
                        {
                            cmd.Parameters.AddWithValue("@q", q.Id);
                            cmd.Parameters.AddWithValue("@s", steamId);
                            cmd.Parameters.AddWithValue("@p", period);
                            if (cmd.ExecuteScalar() != null) continue;
                        }

                        // 3) is every required item satisfied?
                        if (!IsQuestSatisfied(c, q, steamId, period)) continue;

                        // 4) claim the completion (PK protects against concurrent winners)
                        int rows;
                        using (MySqlCommand cmd = new MySqlCommand(
                            "INSERT IGNORE INTO `" + _questCompletions + "` (quest_id, steam_id, period_key) "
                            + "VALUES (@q,@s,@p);", c))
                        {
                            cmd.Parameters.AddWithValue("@q", q.Id);
                            cmd.Parameters.AddWithValue("@s", steamId);
                            cmd.Parameters.AddWithValue("@p", period);
                            rows = cmd.ExecuteNonQuery();
                        }
                        if (rows > 0)
                            completed.Add(new QuestCompletion { QuestId = q.Id, Name = q.Name, RewardCoins = q.RewardCoins });
                    }
                }
            }
            catch (Exception ex) { Logger.LogException(ex, "[SellVault] RecordSaleForQuests failed"); }
            return completed;
        }

        private bool IsQuestSatisfied(MySqlConnection c, QuestDef q, ulong steamId, string period)
        {
            // Pull current sold_qty per required item; verify each meets qty_required.
            using (MySqlCommand cmd = new MySqlCommand(
                "SELECT qi.item_id, qi.qty_required, COALESCE(qp.sold_qty,0) AS sold_qty "
                + "FROM `" + _questItems + "` qi "
                + "LEFT JOIN `" + _questProgress + "` qp "
                + "  ON qp.quest_id = qi.quest_id AND qp.item_id = qi.item_id "
                + "  AND qp.steam_id = @s AND qp.period_key = @p "
                + "WHERE qi.quest_id = @q;", c))
            {
                cmd.Parameters.AddWithValue("@q", q.Id);
                cmd.Parameters.AddWithValue("@s", steamId);
                cmd.Parameters.AddWithValue("@p", period);
                int rowCount = 0;
                using (MySqlDataReader r = cmd.ExecuteReader())
                {
                    while (r.Read())
                    {
                        rowCount++;
                        int req = Convert.ToInt32(r["qty_required"]);
                        long sold = Convert.ToInt64(r["sold_qty"]);
                        if (sold < req) return false;
                    }
                }
                return rowCount > 0;
            }
        }
    }
}
