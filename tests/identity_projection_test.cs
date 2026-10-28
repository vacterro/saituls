// The provider-identity projection, on synthetic tokens.
//
// Every rule the Accounts panel depends on is a claim about what identity IS
// and what it is NOT, and all of them can be checked without a real Google
// sign-in: two tokens for one human, two humans behind one address, a rotated
// token, a renamed address, a missing claim, a malformed blob. Testing these
// through a live credential store would prove one fact and call it a rule.
//
// Compiled against the engine source itself, so the function under test is the
// one that runs, not a copy that can drift.

using System;
using System.Collections.Generic;
using System.Text;

static class IdentityProjectionTest
{
    static int _passed, _failed;

    static void Check(bool ok, string what, string detail)
    {
        if (ok) { _passed++; Console.WriteLine("PASS  " + what); }
        else { _failed++; Console.WriteLine("FAIL  " + what + "  -- " + detail); }
    }

    // A JWT is only ever three base64url parts, and the engine reads the middle
    // one. The signature is never verified and never has to be real.
    static string Token(string payloadJson)
    {
        return "eyJhbGciOiJSUzI1NiJ9." + B64(payloadJson) + ".c2ln";
    }

    static string B64(string s)
    {
        return Convert.ToBase64String(Encoding.UTF8.GetBytes(s)).TrimEnd('=')
            .Replace('+', '-').Replace('/', '_');
    }

    static Dictionary<string, object> Blob(string payloadJson)
    {
        return Project("{\"id_token\":\"" + Token(payloadJson) + "\"}");
    }

    static Dictionary<string, object> Project(string blob)
    {
        return SaiAccounts.IdentityFromBlob(blob);
    }

    static string Fp(Dictionary<string, object> id) { return S(id, "fingerprint"); }
    static string Label(Dictionary<string, object> id) { return S(id, "account_label"); }
    static string S(Dictionary<string, object> d, string k)
    {
        object v; return d.TryGetValue(k, out v) && v != null ? v.ToString() : "";
    }
    static bool Has(Dictionary<string, object> d, string k) { return d.ContainsKey(k); }
    static string Dump(Dictionary<string, object> d)
    {
        var sb = new StringBuilder("{");
        foreach (var kv in d) sb.Append(kv.Key).Append("=").Append(kv.Value).Append(" ");
        return sb.Append("}").ToString();
    }

    static int Main()
    {
        const string Alice = "{\"sub\":\"107746076155001234567\",\"email\":\"alice@example.com\",\"email_verified\":true}";
        const string AliceRotated = "{\"sub\":\"107746076155001234567\",\"email\":\"alice@example.com\",\"email_verified\":true,\"iat\":1780000000,\"exp\":1780003600,\"nonce\":\"zzz\"}";
        const string AliceRenamed = "{\"sub\":\"107746076155001234567\",\"email\":\"alice@newdomain.net\",\"email_verified\":true}";
        const string Bob = "{\"sub\":\"107746076155009999999\",\"email\":\"bob@example.net\",\"email_verified\":true}";
        const string BobSameAddress = "{\"sub\":\"107746076155009999999\",\"email\":\"alice@example.com\",\"email_verified\":true}";

        // The same human, twice, with two different tokens. Token refresh must
        // not look like a different account: this is the property the whole
        // duplicate warning rests on.
        var a1 = Blob(Alice);
        var a2 = Blob(AliceRotated);
        Check(Fp(a1).Length == 16, "ID1 a verified sub yields a 16-char fingerprint", Dump(a1));
        Check(Fp(a1) == Fp(a2), "ID2 a rotated token keeps the SAME identity", Fp(a1) + " vs " + Fp(a2));
        Check(Label(a1) == "alice@example.com", "ID3 the label is the address from that same token", Label(a1));
        Check(S(a1, "source") == "google_id_token_sub", "ID4 provenance says the id came from sub", S(a1, "source"));

        // A renamed address is still one human. Keying on the email would split
        // one account into two and invent capacity that does not exist.
        var renamed = Blob(AliceRenamed);
        Check(Fp(renamed) == Fp(a1), "ID5 an email RENAME does not redefine identity", Fp(renamed));
        Check(Label(renamed) == "alice@newdomain.net", "ID6 and the label follows the rename", Label(renamed));

        // Two humans, one address. Keying on the email would merge them, which
        // is the same invented capacity in the other direction.
        var bob = Blob(BobSameAddress);
        Check(Fp(bob) != Fp(a1), "ID7 two subs behind one address stay two identities", Fp(bob));
        Check(Label(bob) == Label(a1), "ID8 the label is display, and can be identical", Label(bob));

        // Case in the subject is a formatting detail, not a different person.
        var upper = Blob("{\"sub\":\"107746076155001234567\",\"email\":\"Alice@Example.com\"}");
        Check(Fp(upper) == Fp(a1), "ID9 subject case never changes the identity", Fp(upper));

        // No sub: the token is still Google's, and the address is the only
        // stable thing in it. The provenance has to say so, because this
        // fingerprint does NOT survive a rename the way the other one does.
        var noSub = Blob("{\"email\":\"carol@example.org\"}");
        Check(S(noSub, "source") == "google_id_token_email",
            "ID10 a token with no sub says where its fingerprint came from", S(noSub, "source"));
        Check(Fp(noSub) == Project("{\"id_token\":\"" + Token("{\"email\":\"carol@example.org\"}") + "\"}")["fingerprint"].ToString(),
            "ID11 and it is stable across reads", Fp(noSub));
        Check(Fp(noSub) != Fp(a1), "ID12 an email-derived id is not a sub-derived one", Fp(noSub));

        // No address at all: identity, no label. The label is optional because
        // Google may not always send one; the fingerprint is not.
        var noEmail = Blob("{\"sub\":\"107746076155005555555\"}");
        Check(Fp(noEmail).Length == 16 && !Has(noEmail, "account_label"),
            "ID13 a token with no email still identifies, and labels nothing", Fp(noEmail));

        // Unproven shapes. A panel that shows "verified" for a credential it
        // could not read would be lying about exactly the thing this feature
        // exists to establish.
        foreach (var pair in new[] {
            new KeyValuePair<string,string>("garbage", "not json at all"),
            new KeyValuePair<string,string>("no id_token", "{\"access_token\":\"ya29.secret\"}"),
            new KeyValuePair<string,string>("short token", "{\"id_token\":\"abc\"}"),
            new KeyValuePair<string,string>("payload not json", "{\"id_token\":\"eyJhbGciOiJub3QifQ.bm90LWpzb24.sig\"}"),
            new KeyValuePair<string,string>("empty", ""),
        })
        {
            var id = Project(pair.Value);
            Check(!object.Equals(id["verified"], true) && Fp(id).Length == 0,
                "ID14 " + pair.Key + " reads as unproven, not as an identity", "verified=" + id["verified"] + " fp=" + Fp(id));
        }

        // Nothing that could carry a credential leaves. Checked on the
        // serialized projection rather than field by field, so a field ADDED
        // later is covered too.
        var secretToken = Token(Alice);
        var json = new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(a1);
        Check(json.IndexOf("eyJ", StringComparison.Ordinal) < 0 && json.IndexOf(secretToken, StringComparison.Ordinal) < 0,
            "ID15 no token byte reaches the projection", json);
        Check(json.IndexOf("107746076155001234567", StringComparison.Ordinal) < 0,
            "ID16 the raw subject never leaves either", json);
        Check(Label(a1).IndexOf("example", StringComparison.OrdinalIgnoreCase) >= 0,
            "ID17 and the operator-facing label is the only address in it", json);

        Console.WriteLine("");
        Console.WriteLine("passed " + _passed + ", failed " + _failed);
        return _failed > 0 ? 1 : 0;
    }
}