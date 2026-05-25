using System.Collections.Generic;
using Rocket.API;
using Rocket.Unturned.Player;

namespace SellVault
{
    /// <summary>
    /// <c>/link &lt;code&gt;</c> - links your Discord account using a code from the Discord
    /// Welcome Pack button, and grants the welcome pack. Permission: <c>sellvault.link</c>.
    /// </summary>
    public sealed class CommandLink : IRocketCommand
    {
        public AllowedCaller AllowedCaller => AllowedCaller.Player;
        public string Name => "link";
        public string Help => "Link your Discord account with a code from Discord.";
        public string Syntax => "<code>";
        public List<string> Aliases => new List<string>();
        public List<string> Permissions => new List<string> { "sellvault.link" };

        public void Execute(IRocketPlayer caller, string[] command)
        {
            SellVaultPlugin plugin = SellVaultPlugin.Instance;
            UnturnedPlayer up = caller as UnturnedPlayer;
            if (plugin?.Database == null || up?.Player == null) return;

            if (command.Length < 1 || string.IsNullOrEmpty(command[0]))
            {
                SellVaultPlugin.Say(up.Player, plugin.Configuration.Instance.MsgLinkUsage);
                return;
            }
            plugin.LinkAccount(up, command[0].Trim());
        }
    }
}
