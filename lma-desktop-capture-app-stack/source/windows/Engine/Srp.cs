using System.Globalization;
using System.Net.Http;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace LMA;

/// <summary>
/// Cognito USER_SRP_AUTH login — the SAME flow the LMA web UI uses (Amplify
/// defaults to SRP), implemented dependency-free so this client stays SDK-free.
///
/// Two round-trips to the Cognito Identity Provider JSON API (no SigV4 needed —
/// these two calls are unauthenticated):
///   1. InitiateAuth (USER_SRP_AUTH)  → server salt, SRP_B, secret block
///   2. RespondToAuthChallenge (PASSWORD_VERIFIER) → tokens (access/id/refresh)
///
/// The password is NEVER sent: SRP proves knowledge of it via a challenge. SRP-6a
/// math per the Cognito variant (SHA-256, the quirky pad_hex, HKDF-derived key,
/// timestamped HMAC signature) — validated byte-for-byte against pycognito in
/// SelfTest. Ported from macOS SRP.swift; big-number math uses .NET's built-in
/// System.Numerics.BigInteger (unsigned big-endian) instead of a hand-rolled bignum.
/// </summary>
public static class Srp
{
    /// <summary>RFC 3526 3072-bit MODP group prime — the group Cognito uses. Generator g=2.</summary>
    public const string NHex =
        "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1" +
        "29024E088A67CC74020BBEA63B139B22514A08798E3404DD" +
        "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245" +
        "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED" +
        "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D" +
        "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F" +
        "83655D23DCA3AD961C62F356208552BB9ED529077096966D" +
        "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B" +
        "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9" +
        "DE2BCBF6955817183995497CEA956AE515D2261898FA0510" +
        "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64" +
        "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7" +
        "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B" +
        "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C" +
        "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31" +
        "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF";

    public readonly record struct Tokens(string AccessToken, string IdToken, string RefreshToken);

    public sealed class SrpException : Exception
    {
        public SrpException(string message) : base(message) { }
    }

    private static readonly HttpClient Http = new();

    // MARK: - Public entry point

    /// <summary>
    /// Perform the full SRP login. `region` derives the endpoint; `clientId` is
    /// the Cognito App Client ID; `poolId` is the full user pool id (region_xxx).
    /// </summary>
    public static async Task<Tokens> LoginAsync(string username, string password,
        string poolId, string clientId, string region)
    {
        var N = FromHex(NHex);
        var g = new BigInteger(2);

        // Client ephemeral: a random, A = g^a mod N.
        var a = RandomBig(128) % N;
        var A = BigInteger.ModPow(g, a, N);
        if ((A % N).IsZero) throw new SrpException("A % N == 0");
        var srpA = ToHex(A);

        // 1) InitiateAuth (USER_SRP_AUTH)
        var init1 = new Dictionary<string, object>
        {
            ["AuthFlow"] = "USER_SRP_AUTH",
            ["ClientId"] = clientId,
            ["AuthParameters"] = new Dictionary<string, object> { ["USERNAME"] = username, ["SRP_A"] = srpA },
        };
        var r1 = await CallAsync(region, "InitiateAuth", init1);

        if (!(r1.TryGetProperty("ChallengeName", out var chEl) && chEl.GetString() == "PASSWORD_VERIFIER"
              && r1.TryGetProperty("ChallengeParameters", out var pp)))
        {
            var got = r1.TryGetProperty("ChallengeName", out var cn) ? cn.GetString() : null;
            throw new SrpException($"Unexpected auth challenge: {got ?? "no PASSWORD_VERIFIER challenge"}");
        }
        string saltHex = pp.GetProperty("SALT").GetString()!;
        string srpBHex = pp.GetProperty("SRP_B").GetString()!;
        string secretBlock = pp.GetProperty("SECRET_BLOCK").GetString()!;
        string userIdForSrp = pp.GetProperty("USER_ID_FOR_SRP").GetString()!;

        var poolName = poolId.Contains('_') ? poolId.Split('_', 2)[1] : poolId;

        // Compute the password-proof signature (Cognito SRP variant). Extracted
        // into a pure function so it can be validated offline against a
        // pycognito-derived known-answer (see SelfTest.Run()).
        var timestamp = CognitoTimestamp();
        var signature = ComputeSignature(
            poolName: poolName, userId: userIdForSrp, password: password,
            saltHex: saltHex, srpBHex: srpBHex, secretBlockB64: secretBlock,
            timestamp: timestamp, aHex: ToHex(a), aBigA: A);

        // 2) RespondToAuthChallenge (PASSWORD_VERIFIER)
        var resp = new Dictionary<string, object>
        {
            ["ChallengeName"] = "PASSWORD_VERIFIER",
            ["ClientId"] = clientId,
            ["ChallengeResponses"] = new Dictionary<string, object>
            {
                ["USERNAME"] = userIdForSrp,
                ["PASSWORD_CLAIM_SECRET_BLOCK"] = secretBlock,
                ["PASSWORD_CLAIM_SIGNATURE"] = signature,
                ["TIMESTAMP"] = timestamp,
            },
        };
        var r2 = await CallAsync(region, "RespondToAuthChallenge", resp);
        if (r2.TryGetProperty("ChallengeName", out var next))
        {
            throw new SrpException(
                $"server requires additional challenge '{next.GetString()}' (e.g. MFA / new password) — not supported by this prototype");
        }
        if (!r2.TryGetProperty("AuthenticationResult", out var auth))
            throw new SrpException("no AuthenticationResult in RespondToAuthChallenge");
        var access = auth.GetProperty("AccessToken").GetString()!;
        var id = auth.GetProperty("IdToken").GetString()!;
        var refresh = auth.TryGetProperty("RefreshToken", out var rt) ? (rt.GetString() ?? "") : "";
        return new Tokens(access, id, refresh);
    }

    // MARK: - Cognito JSON API transport

    private static async Task<JsonElement> CallAsync(string region, string target, Dictionary<string, object> body)
    {
        var url = $"https://cognito-idp.{region}.amazonaws.com/";
        using var req = new HttpRequestMessage(HttpMethod.Post, url);
        var json = JsonSerializer.Serialize(body);
        req.Content = new StringContent(json, Encoding.UTF8);
        req.Content.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("application/x-amz-json-1.1");
        req.Headers.TryAddWithoutValidation("X-Amz-Target", $"AWSCognitoIdentityProviderService.{target}");

        using var res = await Http.SendAsync(req);
        var text = await res.Content.ReadAsStringAsync();
        JsonElement obj;
        try { obj = JsonDocument.Parse(string.IsNullOrEmpty(text) ? "{}" : text).RootElement.Clone(); }
        catch { obj = JsonDocument.Parse("{}").RootElement.Clone(); }

        if ((int)res.StatusCode != 200)
        {
            // Cognito error bodies look like {"__type":"NotAuthorizedException","message":"..."}
            var type = obj.TryGetProperty("__type", out var t) ? t.GetString() : "HTTPError";
            var m = obj.TryGetProperty("message", out var mm) ? mm.GetString() : text;
            throw new SrpException($"Cognito HTTP {(int)res.StatusCode}: {type}: {m}");
        }
        return obj;
    }

    // MARK: - Signature computation (Cognito SRP variant, hex-based)

    /// <summary>
    /// Pure, deterministic computation of the PASSWORD_VERIFIER signature.
    /// Mirrors pycognito exactly (validated byte-for-byte in SelfTest.Run()):
    ///   k   = hexHash("00" + N_HEX + "0" + G_HEX)
    ///   u   = hexHash(padHex(A) + padHex(B))
    ///   x   = hexHash(padHex(salt) + sha256hex(poolName+userId+":"+password))
    ///   S   = (B - k*g^x)^(a + u*x) mod N
    ///   key = HKDF(ikm=padHex(S) bytes, salt=padHex(u) bytes)  [Caldera Derived Key, 16B]
    ///   sig = base64( HMAC-SHA256(key, poolName|userId|secretBlock|timestamp) )
    /// `aBigA` is the already-computed A (avoids recomputing g^a).
    /// </summary>
    public static string ComputeSignature(string poolName, string userId, string password,
        string saltHex, string srpBHex, string secretBlockB64,
        string timestamp, string aHex, BigInteger aBigA)
    {
        var N = FromHex(NHex);
        var g = new BigInteger(2);
        var A = aBigA;
        var B = FromHex(srpBHex);
        if ((B % N).IsZero) throw new SrpException("B % N == 0");
        var a = FromHex(aHex);

        // k = hexHash("00" + N_HEX + "0" + G_HEX)   (note the literal "0" before g)
        var k = FromHex(HexHash("00" + NHex + "0" + "2"));

        // u = hexHash(padHex(A) + padHex(B))
        var u = FromHex(HexHash(PadHex(ToHex(A)) + PadHex(ToHex(B))));
        if (u.IsZero) throw new SrpException("u == 0");

        // x = hexHash( padHex(salt) + sha256hex(poolName + userId + ":" + password) )
        var idHashHex = Sha256Hex(Encoding.UTF8.GetBytes($"{poolName}{userId}:{password}"));
        var x = FromHex(HexHash(PadHex(saltHex) + idHashHex));

        // S = (B - k*g^x)^(a + u*x) mod N
        var gx = BigInteger.ModPow(g, x, N);
        var kgx = (k * gx) % N;
        var baseVal = (B + N - kgx) % N;   // (B - k*g^x) mod N, underflow-safe
        var exp = a + (u * x);
        var S = BigInteger.ModPow(baseVal, exp, N);

        // HKDF (Caldera Derived Key), 16-byte key.
        var ikm = HexToBytes(PadHex(ToHex(S)));
        var saltBytes = HexToBytes(PadHex(ToHex(u)));
        var key = DerivedKey(ikm, saltBytes);

        var secretBlockData = Convert.FromBase64String(secretBlockB64);
        var msg = new List<byte>();
        msg.AddRange(Encoding.UTF8.GetBytes(poolName));
        msg.AddRange(Encoding.UTF8.GetBytes(userId));
        msg.AddRange(secretBlockData);
        msg.AddRange(Encoding.UTF8.GetBytes(timestamp));
        var sig = HmacSha256(key, msg.ToArray());
        return Convert.ToBase64String(sig);
    }

    // MARK: - Crypto helpers

    private static byte[] Sha256(byte[] bytes) => SHA256.HashData(bytes);

    /// <summary>
    /// SHA-256 as a lowercase hex string, zero-padded to 64 chars (Cognito's
    /// hash_sha256 does this so leading-zero digests keep full width).
    /// </summary>
    private static string Sha256Hex(byte[] bytes)
    {
        var h = Convert.ToHexString(Sha256(bytes)).ToLowerInvariant();
        return h.PadLeft(64, '0');
    }

    /// <summary>hex_hash: SHA-256 of the bytes decoded from a hex string, as hex.</summary>
    private static string HexHash(string hexString) => Sha256Hex(HexToBytes(hexString));

    /// <summary>
    /// Cognito pad_hex: ensure even length; if the top nibble is 8..F prepend
    /// "00" (a sign byte) so the value reads as unsigned. NOT full-width padding.
    /// </summary>
    public static string PadHex(string hexIn)
    {
        var h = hexIn;
        if (h.Length % 2 == 1)
            h = "0" + h;
        else if (h.Length > 0 && "89abcdefABCDEF".Contains(h[0]))
            h = "00" + h;
        return h;
    }

    private static byte[] HexToBytes(string hex)
    {
        var bytes = new List<byte>(hex.Length / 2);
        int i = 0;
        while (i + 2 <= hex.Length)
        {
            if (byte.TryParse(hex.AsSpan(i, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var b))
                bytes.Add(b);
            i += 2;
        }
        return bytes.ToArray();
    }

    private static byte[] HmacSha256(byte[] key, byte[] message) => new HMACSHA256(key).ComputeHash(message);

    /// <summary>
    /// Cognito "Caldera Derived Key" HKDF: prk = HMAC(salt, ikm);
    /// okm = HMAC(prk, "Caldera Derived Key"||0x01); first 16 bytes.
    /// </summary>
    private static byte[] DerivedKey(byte[] ikm, byte[] salt)
    {
        var info = Encoding.UTF8.GetBytes("Caldera Derived Key").Concat(new byte[] { 0x01 }).ToArray();
        var prk = HmacSha256(salt, ikm);
        var okm = HmacSha256(prk, info);
        return okm.Take(16).ToArray();
    }

    /// <summary>
    /// Cognito timestamp: "ddd MMM d HH:mm:ss 'UTC' yyyy", invariant (en-US) names,
    /// UTC, no leading zero on day-of-month (matches pycognito's format exactly).
    /// </summary>
    private static string CognitoTimestamp()
    {
        var now = DateTime.UtcNow;
        // A single 'd' specifier inside a longer format = day with no leading zero.
        return now.ToString("ddd MMM d HH:mm:ss 'UTC' yyyy", CultureInfo.InvariantCulture);
    }

    // MARK: - BigInteger <-> hex (unsigned, big-endian) helpers

    /// <summary>Parse an unsigned big-endian hex string into a non-negative BigInteger.</summary>
    public static BigInteger FromHex(string hex)
    {
        var h = hex.StartsWith("0x") ? hex.Substring(2) : hex;
        h = new string(h.Where(c => !char.IsWhiteSpace(c)).ToArray());
        if (h.Length == 0) return BigInteger.Zero;
        if (h.Length % 2 != 0) h = "0" + h;
        var bytes = HexToBytes(h);
        // isUnsigned + isBigEndian: interpret exactly as Cognito's unsigned hex.
        return new BigInteger(bytes, isUnsigned: true, isBigEndian: true);
    }

    /// <summary>
    /// Minimal-length lowercase hex (no leading zero nibble), matching Swift's
    /// BigUInt.toHex() / Python's format(x, 'x'). Zero → "0".
    /// </summary>
    public static string ToHex(BigInteger v)
    {
        if (v.IsZero) return "0";
        var bytes = v.ToByteArray(isUnsigned: true, isBigEndian: true);
        // Top byte without a leading zero nibble.
        var s = bytes[0].ToString("x", CultureInfo.InvariantCulture);
        for (int i = 1; i < bytes.Length; i++) s += bytes[i].ToString("x2", CultureInfo.InvariantCulture);
        return s;
    }

    /// <summary>Cryptographically-random non-negative BigInteger of `n` bytes.</summary>
    private static BigInteger RandomBig(int n)
    {
        var b = RandomNumberGenerator.GetBytes(n);
        return new BigInteger(b, isUnsigned: true, isBigEndian: true);
    }
}
