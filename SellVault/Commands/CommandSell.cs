using System.Collections.Generic;
using Rocket.API;
using Rocket.Unturned.Player;

namespace SellVault
{
    /// <summary>
    /// <c>/setsellbox</c> - while you have a storage barricade open, registers it as a sell box.
    /// Players who put market items in it and close it will sell them for coins.
    /// Permission: <c>sellvault.setsellbox</c> (admin).
    /// </summary>
    public sealed class CommandSetSellBox : IRocketCommand
    {
        public AllowedCaller AllowedCaller => AllowedCaller.Player;
        public string Name => "setsellbox";
        public string Help => "Register the storage you currently have open as a sell box.";
        public string Syntax => "";
        public List<string> Aliases => new List<string>();
        public List<string> Permissions => new List<string> { "sellvault.setsellbox" };

        public void Execute(IRocketPlayer caller, string[] command)
        {
            SellVaultPlugin plugin = SellVaultPlugin.Instance;
            UnturnedPlayer up = caller as UnturnedPlayer;
            if (plugin?.Database == null || up?.Player == null) return;
            plugin.SetSellBox(up);
        }
    }
}
