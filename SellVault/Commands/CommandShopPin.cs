using System.Collections.Generic;
using Rocket.API;
using Rocket.Unturned.Player;

namespace SellVault
{
    /// <summary>
    /// <c>/shoppin &lt;pin&gt;</c> - sets a 6-digit PIN used to log in on the web shop with your
    /// Steam ID. The PIN is sent to the API over HTTPS and hashed server-side; this plugin never
    /// stores it. PIN must be exactly 6 digits. Permission: <c>sellvault.shoppin</c>.
    /// </summary>
    public sealed class CommandShopPin : IRocketCommand
    {
        public AllowedCaller AllowedCaller => AllowedCaller.Player;
        public string Name => "shoppin";
        public string Help => "Set your 6-digit web shop login PIN.";
        public string Syntax => "<pin>";
        public List<string> Aliases => new List<string>();
        public List<string> Permissions => new List<string> { "sellvault.shoppin" };

        public void Execute(IRocketPlayer caller, string[] command)
        {
            SellVaultPlugin plugin = SellVaultPlugin.Instance;
            UnturnedPlayer up = caller as UnturnedPlayer;
            if (plugin?.Database == null || up?.Player == null) return;

            string pin = command.Length >= 1 ? command[0].Trim() : null;
            if (!IsSixDigits(pin))
            {
                SellVaultPlugin.Say(up.Player, plugin.Configuration.Instance.MsgPinUsage);
                return;
            }
            plugin.SetWebPin(up, pin);
        }

        private static bool IsSixDigits(string s)
        {
            if (string.IsNullOrEmpty(s) || s.Length != 6) return false;
            foreach (char c in s)
                if (c < '0' || c > '9') return false;
            return true;
        }
    }
}
