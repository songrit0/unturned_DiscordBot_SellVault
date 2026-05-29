using System.Collections.Generic;
using Rocket.API;
using Rocket.Unturned.Player;

namespace SellVault
{
    /// <summary>
    /// <c>/shop</c> - shows the player their personal web shop login link
    /// (https://meowpow.shop/login?id=&lt;SteamID&gt;) plus a hint to set a PIN with /shoppin.
    /// Works whether or not the player has linked their Discord. Permission: <c>sellvault.shop</c>.
    /// </summary>
    public sealed class CommandShop : IRocketCommand
    {
        public AllowedCaller AllowedCaller => AllowedCaller.Player;
        public string Name => "shop";
        public string Help => "Show your web shop login link.";
        public string Syntax => "";
        public List<string> Aliases => new List<string>();
        public List<string> Permissions => new List<string> { "sellvault.shop" };

        public void Execute(IRocketPlayer caller, string[] command)
        {
            SellVaultPlugin plugin = SellVaultPlugin.Instance;
            UnturnedPlayer up = caller as UnturnedPlayer;
            if (plugin?.Database == null || up?.Player == null) return;
            plugin.ShowShopLink(up);
        }
    }
}
