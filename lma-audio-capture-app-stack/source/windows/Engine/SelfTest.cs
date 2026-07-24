using System.Numerics;

namespace LMA;

/// <summary>
/// Known-answer tests for the big-number math and the Cognito SRP signature, run
/// via `--selftest`. Expected values were computed independently in Python
/// (arbitrary-precision int) and pycognito (the reference SRP impl), including a
/// modpow on the REAL Cognito 3072-bit SRP group and a full password-proof
/// signature. If any of these fail, do NOT trust the SRP login path.
///
/// Ported from macOS SelfTest.swift. The BigUInt-arithmetic vectors (mul/mod/
/// add/sub/div) are kept as sanity checks on the .NET BigInteger hex round-trip,
/// while the two load-bearing SRP vectors (g^x mod N, and the end-to-end
/// signature) are preserved byte-for-byte.
/// </summary>
public static class SelfTest
{
    public static int Run()
    {
        int failures = 0;
        void Check(string name, string got, string want)
        {
            if (string.Equals(got, want, StringComparison.OrdinalIgnoreCase))
            {
                Console.WriteLine($"  ✓ {name}");
            }
            else
            {
                failures++;
                Console.WriteLine($"  ✗ {name}\n      got:  {got}\n      want: {want}");
            }
        }

        Console.WriteLine("BigInteger + SRP known-answer tests:");

        var a = Srp.FromHex("deadbeef");
        var b = Srp.FromHex("cafebabe");
        Check("mul deadbeef*cafebabe", Srp.ToHex(a * b), "b092ab7b88cf5b62");

        // 10^50 mod 97
        var ten50 = Srp.FromHex("446c3b15f9926687d2c40534fdb564000000000000");
        Check("mod 10^50 % 97", Srp.ToHex(ten50 % new BigInteger(97)), "5e");

        var big1 = Srp.FromHex("27e41b3246bec9b16e398115");   // 12345678901234567890123456789
        var big2 = Srp.FromHex("db4da5f7ef412b1");            // 987654321987654321
        Check("add", Srp.ToHex(big1 + big2), "27e41b325473a410ed2d93c6");
        Check("sub", Srp.ToHex(big1 - big2), "27e41b323909ef51ef456e64");
        Check("div quotient", Srp.ToHex(big1 / big2), "2e90edc82");
        Check("div remainder", Srp.ToHex(big1 % big2), "b90985b1789e733");

        // Multi-word division (isolates the algorithm the 3072-bit modpow relies on).
        var bigA = Srp.FromHex("deadbeefcafebabe0123456789abcdeffedcba98765432101122334455667788");
        var bigD = Srp.FromHex("99887766554433221100998877665544");
        Check("multiword div quotient", Srp.ToHex(bigA / bigD), "1734afe32fb5105279be47f64fe6e469a");
        Check("multiword div remainder", Srp.ToHex(bigA % bigD), "2553f2109aceeda9c8d9a5646b4e94a0");

        // Independent small-modulus modpow cross-check.
        // 7^2019 mod 1000000 = 973143 (verified in Python). 0x7e3 == 2019, 0xf4240 == 1e6.
        Check("modpow small 7^2019 % 1e6",
            Srp.ToHex(BigInteger.ModPow(new BigInteger(7), Srp.FromHex("7e3"), Srp.FromHex("f4240"))),
            "ed957"); // 973143 == 0xed957

        // The critical one: g^x mod N on the Cognito 3072-bit group.
        var N = Srp.FromHex(Srp.NHex);
        var g = new BigInteger(2);
        var x = Srp.FromHex("1234567890ABCDEF1234567890ABCDEF");
        var want =
            "72763f1db9ed9ca695d9479af81531f9cee603f3d5e27cf9437ede782855c4c3" +
            "efe9ae016974f36a46dffce5c72b21c0fcd9726b8b3d829fdff8112ca9d6bc7b" +
            "89c52c80d1e7f2713c1888c68b43d46c22c6257a6ab441cb78a7ef235620e707" +
            "ce32bbb91c29cb57be52d4de173b41f5504fc40874576be271ad8e8e269c0a2e" +
            "3c601b68a52cd32f7e50ff86a9c764e8afbd052fe29dfe953de029370f78879b" +
            "187145e5cddadb0bf95f2ed3cd2e37a3bd1f706c83e002d652dea11f8768bf25" +
            "0a4cf6e338e99589a22b7a0f2b1c7039044be729caab0d4af4c55201ebfa6469" +
            "4dd75b5cf9e82c582c038214ada8638820e5787c5dba74e81d39d54f60167a1d" +
            "fdb09888eefd9db206a32682842a44f8b2f6452f616b0a84f189fb0bda4e96db" +
            "f0347f17aed6227a549577d498886ba525be77bdfebff65bc880e7bdf7c5a66d" +
            "cc42f05412efd1fdac2eec9144f69f7b3978188caa03c4628606ce8a2424c65a" +
            "16b73042bc5db262fa4a9a167e2eb59e97dfa2bd7f4d8e7fa801a6c2ae5d558e";
        Check("modpow g^x mod N (3072-bit)", Srp.ToHex(BigInteger.ModPow(g, x, N)), want);

        // SRP signature known-answer: fixed inputs, expected value computed
        // independently by pycognito (the reference impl). Validates padHex, k,
        // u, x, S, HKDF, and the HMAC signature end-to-end — offline, no network.
        var userId = "b85123f0-e0d1-702b-6161-a5e787cf8fd5";
        var aHex = "1234567890abcdef1234567890abcdef1234567890abcdef";
        var A = BigInteger.ModPow(new BigInteger(2), Srp.FromHex(aHex), N);
        try
        {
            var secretBlockB64 = Convert.ToBase64String(
                System.Text.Encoding.UTF8.GetBytes("the-secret-block-bytes-1234567890"));
            var sig = Srp.ComputeSignature(
                poolName: "1YC53wwJv",
                userId: userId,
                password: "S1mpl3t0n!",
                saltHex: "a1b2c3d4e5f60718293a4b5c6d7e8f90",
                srpBHex: "2f" + string.Concat(Enumerable.Repeat("ab", 191)),
                secretBlockB64: secretBlockB64,
                timestamp: "Tue Jul 7 12:34:56 UTC 2026",
                aHex: aHex, aBigA: A);
            Check("SRP signature (pycognito KAT)", sig, "c6lvwT+X7HJCHwEo2wQQezH5ONGWuuhsBVrri3hJ5qk=");
        }
        catch (Exception e)
        {
            failures++;
            Console.WriteLine($"  ✗ SRP signature threw: {e}");
        }

        Console.WriteLine(failures == 0 ? "\nAll self-tests PASSED ✓" : $"\n{failures} self-test(s) FAILED ✗");
        return failures == 0 ? 0 : 1;
    }
}
