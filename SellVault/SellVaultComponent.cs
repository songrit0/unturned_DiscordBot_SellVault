using SDG.Unturned;
using UnityEngine;

namespace SellVault
{
    /// <summary>
    /// Per-player component that tracks the storage the player currently has open and fires when
    /// they close it. Opening a storage resizes the STORAGE page to its size; closing resizes it to
    /// 0x0 (same signal RFVault uses). We capture the InteractableStorage on open so we still have
    /// it at close time, then hand it to the plugin to check if it's a registered sell box.
    /// </summary>
    public sealed class SellVaultComponent : MonoBehaviour
    {
        public Player Player;
        private InteractableStorage _openStorage;
        private bool _subscribed;

        public void Init(Player player)
        {
            Player = player;
            if (Player?.inventory != null && !_subscribed)
            {
                Player.inventory.onInventoryResized += OnInventoryResized;
                _subscribed = true;
            }
        }

        private void OnInventoryResized(byte page, byte width, byte height)
        {
            if (page != PlayerInventory.STORAGE)
                return;

            if (width > 0 && height > 0)
            {
                // a storage just opened — remember which one
                _openStorage = Player != null && Player.inventory != null ? Player.inventory.storage : null;
            }
            else // 0x0 -> closed
            {
                InteractableStorage s = _openStorage;
                _openStorage = null;
                if (s != null)
                    SellVaultPlugin.Instance?.OnStorageClosed(Player, s);
            }
        }

        private void OnDestroy()
        {
            if (Player?.inventory != null && _subscribed)
                Player.inventory.onInventoryResized -= OnInventoryResized;
            _subscribed = false;
            _openStorage = null;
        }
    }
}
