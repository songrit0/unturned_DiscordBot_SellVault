using System;
using System.Collections.Generic;
using System.Text;
using MySql.Data.MySqlClient;
using Logger = Rocket.Core.Logging.Logger;

namespace SellVault
{
    public enum LinkResult { Linked, CodeInvalid, SteamAlreadyLinked, DiscordAlreadyLinked, Error }

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
    }
}
