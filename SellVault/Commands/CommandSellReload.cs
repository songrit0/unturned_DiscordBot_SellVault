using System.Collections.Generic;
using Rocket.API;

namespace SellVault
{
    /// <summary><c>/sellreload</c> - reload sell items from the DB. Permission: <c>sellvault.reload</c>.</summary>
    public sealed class CommandSellReload : IRocketCommand
    {
        public AllowedCaller AllowedCaller => AllowedCaller.Both;
        public string Name => "sellreload";
        public string Help => "Reload sell items from the database.";
        public string Syntax => "";
        public List<string> Aliases => new List<string>();
        public List<string> Permissions => new List<string> { "sellvault.reload" };

        public void Execute(IRocketPlayer caller, string[] command)
        {
            SellVaultPlugin plugin = SellVaultPlugin.Instance;
            if (plugin?.Database == null) return;
            plugin.Reload(caller);
        }
    }
}
