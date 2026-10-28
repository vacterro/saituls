using System;
using System.Collections.Generic;
using System.Drawing;

// The Add Account provider catalog.
//
// One source of truth for "which providers exist, which of them work today, and
// what does the operator get when they pick one". The dialog used to carry a
// three-item ComboBox and a `pIdx == 1 ? "claude" : ...` ternary, so every new
// provider meant editing the widget AND the branch that read it, and nothing in
// the codebase could answer "does this provider actually work".
//
// It lives under FolderAutomation/ rather than beside SAITULS.cs because every
// build path in this repository compiles exactly two things: SAITULS.cs and
// FolderAutomation/*.cs. A new top-level file would simply never be built, which
// is the same class of silent-omission defect this catalog exists to prevent.

namespace Saituls
{
    /// How honestly a provider's support can be described. The point of the
    /// enumeration is that "visible" and "usable" are different facts.
    public enum ProviderAvailability
    {
        /// Creation, persistence, launch and auth all exist and are exercised.
        Available,
        /// In the catalog and visible, but there is no backend for it yet. The
        /// operator may look; they may not add.
        Planned,
        /// A backend exists in some form but is not yet verified end to end.
        Experimental,
        /// Known, deliberately not offered.
        Unavailable,
    }

    public sealed class ProviderEntry
    {
        public string ProviderId = "";
        public string DisplayName = "";
        /// Drives the account record, the context backend and the engine's own
        /// provider table. Empty exactly when nothing can be created yet.
        public string AuthFamily = "";
        public ProviderAvailability Availability = ProviderAvailability.Planned;
        public string DescriptionKey = "";
        public string Description { get { return AccountsLoc.T(DescriptionKey); } }
        public string IconKey = "";
        /// The tag the operator sees next to the name. Short on purpose: this is
        /// a label in a dropdown, not a status page.
        public string AvailabilityTag
        {
            get
            {
                switch (Availability)
                {
                    case ProviderAvailability.Available: return AccountsLoc.T("ProviderAvailable");
                    case ProviderAvailability.Experimental: return AccountsLoc.T("ProviderExperimental");
                    case ProviderAvailability.Unavailable: return AccountsLoc.T("ProviderUnavailable");
                    default: return AccountsLoc.T("ProviderPlanned");
                }
            }
        }
        /// The one question the Add Account button asks.
        public bool SupportsAddNow { get { return Availability == ProviderAvailability.Available && ProviderId.Length > 0; } }
        public bool SupportsWindowsManagedContext;
        public bool SupportsCurrentUserContext;
        /// Why it is not available, in one sentence. Empty for a live provider.
        public string SupportNoteKey = "";
        public string SupportNote { get { return SupportNoteKey.Length == 0 ? "" : AccountsLoc.T(SupportNoteKey); } }

        /// What the note area says for this provider. Never a blank string: the
        /// operator always gets either what will happen or why it cannot.
        public string OperatorNote()
        {
            if (SupportsAddNow) return Description;
            return SupportNote.Length > 0 ? SupportNote
                : AccountsLoc.T("ProviderPlannedHelp");
        }

        /// The dropdown row. Name first, then the readiness tag, so the honest
        /// part is visible without expanding anything.
        public string ListText
        {
            get { return SupportsAddNow ? DisplayName : DisplayName + "  (" + AvailabilityTag + ")"; }
        }
    }

    public static class ProviderCatalog
    {
        // Order is the roadmap, not the alphabet: what works today is reachable
        // first, and everything else follows in the order it is intended to
        // arrive. It is stable, so the operator learns one position per entry.
        static readonly ProviderEntry[] Entries = new[]
        {
            new ProviderEntry {
                ProviderId = "antigravity", DisplayName = "Antigravity (Google)",
                AuthFamily = "google_id_token",
                Availability = ProviderAvailability.Available,
                DescriptionKey = "ProviderAntigravityHelp",
                IconKey = "antigravity",
                SupportsWindowsManagedContext = true,
                SupportsCurrentUserContext = true,
            },
            new ProviderEntry {
                ProviderId = "claude", DisplayName = "Claude Code",
                AuthFamily = "cli_environment",
                Availability = ProviderAvailability.Available,
                DescriptionKey = "ProviderClaudeHelp",
                IconKey = "claude",
                SupportsWindowsManagedContext = false,
                SupportsCurrentUserContext = true,
            },
            new ProviderEntry {
                ProviderId = "codex", DisplayName = "Codex",
                AuthFamily = "cli_environment",
                Availability = ProviderAvailability.Available,
                DescriptionKey = "ProviderCodexHelp",
                IconKey = "codex",
                SupportsWindowsManagedContext = false,
                SupportsCurrentUserContext = true,
            },
            new ProviderEntry {
                ProviderId = "zcode", DisplayName = "ZCode",
                AuthFamily = "pending_verification",
                Availability = ProviderAvailability.Planned,
                DescriptionKey = "ProviderPlanned",
                IconKey = "zcode",
                SupportNoteKey = "ProviderZcodeHelp",
            },
            new ProviderEntry {
                ProviderId = "opencode", DisplayName = "OpenCode",
                AuthFamily = "cli_environment",
                Availability = ProviderAvailability.Planned,
                DescriptionKey = "ProviderPlanned",
                IconKey = "opencode",
                // The launcher and its context menus exist; the account backend
                // does not. Saying so is more useful than pretending otherwise.
                SupportNoteKey = "ProviderOpencodeHelp",
            },
            new ProviderEntry {
                ProviderId = "cline", DisplayName = "Cline",
                AuthFamily = "cli_environment",
                Availability = ProviderAvailability.Planned,
                DescriptionKey = "ProviderPlanned",
                IconKey = "cline",
                SupportNoteKey = "ProviderClineHelp",
            },
            new ProviderEntry {
                ProviderId = "gemini-cli", DisplayName = "Gemini CLI",
                AuthFamily = "cli_environment",
                Availability = ProviderAvailability.Planned,
                DescriptionKey = "ProviderPlanned",
                IconKey = "gemini",
                SupportNoteKey = "ProviderGeminiHelp",
            },
        };

        /// The catalog, in presentation order.
        public static IList<ProviderEntry> All { get { return Array.AsReadOnly(Entries); } }

        public static int Count { get { return Entries.Length; } }

        public static ProviderEntry At(int index)
        {
            if (index < 0 || index >= Entries.Length) return null;
            return Entries[index];
        }

        public static ProviderEntry ById(string providerId)
        {
            foreach (ProviderEntry e in Entries)
                if (string.Equals(e.ProviderId, providerId, StringComparison.OrdinalIgnoreCase)) return e;
            return null;
        }

        /// Everything the dialog binds to, as display strings. Kept here so the
        /// widget never assembles a row itself.
        public static string[] ListTexts()
        {
            var outp = new string[Entries.Length];
            for (int i = 0; i < Entries.Length; i++) outp[i] = Entries[i].ListText;
            return outp;
        }

        /// The provider_id to hand the engine, or "" when there is nothing to
        /// hand it. A planned provider returns "" by construction, so the caller
        /// cannot pass a name the backend has never heard of.
        public static string EngineProviderId(int index)
        {
            ProviderEntry e = At(index);
            return e != null && e.SupportsAddNow ? e.ProviderId : "";
        }
    }
}
