using Rocket.Unturned.Events;
using Rocket.Unturned.Player;
using SDG.Unturned;
using UnityEngine;
using Logger = Rocket.Core.Logging.Logger;

namespace SellVault
{
    /// <summary>
    /// Per-player component. On close (STORAGE page goes WxH -> 0x0), look up the sellbox barricade
    /// the player is standing next to and hand it to the plugin. We can't rely on
    /// Player.inventory.storage because it's unreliable in this server-side context.
    /// </summary>
    public sealed class SellVaultComponent : MonoBehaviour
    {
        public Player Player;
        private bool _subscribed;
        private float _nextOnlineRewardAt;
        private float _lastMoveAt;
        private Vector3 _lastPos;

        public void Init(Player player)
        {
            Player = player;
            if (Player?.inventory != null && !_subscribed)
            {
                Player.inventory.onInventoryResized += OnInventoryResized;
                _subscribed = true;
            }

            float now = Time.realtimeSinceStartup;
            int interval = SellVaultPlugin.Instance?.Configuration?.Instance?.OnlineIntervalSeconds ?? 300;
            _nextOnlineRewardAt = now + interval;
            _lastMoveAt = now;
            _lastPos = player != null ? player.transform.position : Vector3.zero;
        }

        private void Update()
        {
            if (Player == null) return;
            SellVaultPlugin plugin = SellVaultPlugin.Instance;
            if (plugin == null) return;
            SellVaultConfiguration cfg = plugin.Configuration?.Instance;
            if (cfg == null || cfg.OnlineRewardCoins <= 0 || cfg.OnlineIntervalSeconds <= 0) return;

            float now = Time.realtimeSinceStartup;

            // movement detection (for AFK)
            Vector3 pos = Player.transform.position;
            if ((pos - _lastPos).sqrMagnitude > 0.04f) { _lastMoveAt = now; _lastPos = pos; }

            if (now < _nextOnlineRewardAt) return;
            _nextOnlineRewardAt = now + cfg.OnlineIntervalSeconds;

            bool alive = Player.life != null && !Player.life.isDead;
            bool notAfk = cfg.AfkSeconds <= 0 || (now - _lastMoveAt) < cfg.AfkSeconds;
            if (!alive || !notAfk) return;

            ulong steamId = Player.channel?.owner?.playerID.steamID.m_SteamID ?? 0;
            if (steamId == 0) return;
            plugin.GrantOnlineReward(steamId, cfg.OnlineRewardCoins);
        }

        private void OnInventoryResized(byte page, byte width, byte height)
        {
            SellVaultPlugin.Dbg("[SellVault] resize page=" + page + " " + width + "x" + height);
            if (page != PlayerInventory.STORAGE) return;
            // Server only emits 0x0 here (close). Use that as trigger.
            if (width != 0 || height != 0) return;

            SellVaultPlugin plugin = SellVaultPlugin.Instance;
            if (plugin == null || Player == null) return;

            ulong sid = Player.channel?.owner?.playerID.steamID.m_SteamID ?? 0;
            InteractableStorage s = plugin.TryGetVirtualVaultStorage(sid);
            string src = "virtual";
            if (s == null) { s = plugin.FindNearbySellBox(Player.transform.position, 5f); src = "nearby"; }
            SellVaultPlugin.Dbg("[SellVault] CLOSED src=" + src + " -> " +
                (s != null ? "FOUND @ " + s.transform.position : "none"));
            if (s != null) plugin.OnStorageClosed(Player, s);
        }

        private void OnDestroy()
        {
            if (Player?.inventory != null && _subscribed)
                Player.inventory.onInventoryResized -= OnInventoryResized;
            _subscribed = false;
        }
    }
}
