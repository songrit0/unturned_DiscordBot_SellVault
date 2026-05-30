using System.Collections.Generic;
using Rocket.API;
using Rocket.Unturned.Player;

namespace SellVault
{
    /// <summary>
    /// <c>/discord</c> (alias <c>/dc</c>) - opens the server's Discord invite in-game (client
    /// browser-request prompt) and prints it in chat as a fallback. Permission: <c>sellvault.discord</c>.
    /// </summary>
    public sealed class CommandDiscord : IRocketCommand
    {
        public AllowedCaller AllowedCaller => AllowedCaller.Player;
        public string Name => "discord";
        public string Help => "Show the server's Discord invite.";
        public string Syntax => "";
        public List<string> Aliases => new List<string> { "dc" };
        public List<string> Permissions => new List<string> { "sellvault.discord" };

        public void Execute(IRocketPlayer caller, string[] command)
        {
            SellVaultPlugin plugin = SellVaultPlugin.Instance;
            UnturnedPlayer up = caller as UnturnedPlayer;
            if (plugin?.Database == null || up?.Player == null) return;
            plugin.ShowDiscordLink(up);
        }
    }
}
