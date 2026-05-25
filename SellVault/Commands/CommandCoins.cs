using System.Collections.Generic;
using Rocket.API;
using Rocket.Unturned.Player;

namespace SellVault
{
    /// <summary><c>/coins</c> - shows your coin balance. Permission: <c>sellvault.coins</c>.</summary>
    public sealed class CommandCoins : IRocketCommand
    {
        public AllowedCaller AllowedCaller => AllowedCaller.Player;
        public string Name => "coins";
        public string Help => "Check your coin balance.";
        public string Syntax => "";
        public List<string> Aliases => new List<string> { "balance" };
        public List<string> Permissions => new List<string> { "sellvault.coins" };

        public void Execute(IRocketPlayer caller, string[] command)
        {
            SellVaultPlugin plugin = SellVaultPlugin.Instance;
            UnturnedPlayer up = caller as UnturnedPlayer;
            if (plugin?.Database == null || up?.Player == null) return;
            plugin.ShowBalance(up);
        }
    }
}
