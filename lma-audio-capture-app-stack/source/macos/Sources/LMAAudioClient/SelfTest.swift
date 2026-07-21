import Foundation

/// Known-answer tests for BigUInt and the Cognito SRP signature, run via
/// `--selftest`. Expected values were computed independently in Python
/// (arbitrary-precision int) and pycognito (the reference SRP impl), including
/// a modpow on the REAL Cognito 3072-bit SRP group and a full password-proof
/// signature. If any of these fail, do NOT trust the SRP login path.
enum SelfTest {
    static func run() -> Int32 {
        var failures = 0
        func check(_ name: String, _ got: String, _ want: String) {
            if got.lowercased() == want.lowercased() {
                print("  ✓ \(name)")
            } else {
                failures += 1
                print("  ✗ \(name)\n      got:  \(got)\n      want: \(want)")
            }
        }

        print("BigUInt + SRP known-answer tests:")

        let a = BigUInt(hex: "deadbeef")!, b = BigUInt(hex: "cafebabe")!
        check("mul deadbeef*cafebabe", (a * b).toHex(), "b092ab7b88cf5b62")

        // 10^50 mod 97
        let ten50 = BigUInt(hex: "446c3b15f9926687d2c40534fdb564000000000000")!
        check("mod 10^50 % 97", (ten50 % BigUInt(97)).toHex(), "5e")

        let big1 = BigUInt(hex: "27e41b3246bec9b16e398115")!   // 12345678901234567890123456789
        let big2 = BigUInt(hex: "db4da5f7ef412b1")!            // 987654321987654321
        check("add", (big1 + big2).toHex(), "27e41b325473a410ed2d93c6")
        check("sub", (big1 - big2).toHex(), "27e41b323909ef51ef456e64")
        let (q, r) = big1.quotientAndRemainder(dividingBy: big2)
        check("div quotient", q.toHex(), "2e90edc82")
        check("div remainder", r.toHex(), "b90985b1789e733")

        // Multi-word Knuth division (isolates the algorithm the 3072-bit modpow
        // relies on): A/D and A%D for large multi-word operands.
        let bigA = BigUInt(hex: "deadbeefcafebabe0123456789abcdeffedcba98765432101122334455667788")!
        let bigD = BigUInt(hex: "99887766554433221100998877665544")!
        let (bq, br) = bigA.quotientAndRemainder(dividingBy: bigD)
        check("multiword div quotient", bq.toHex(), "1734afe32fb5105279be47f64fe6e469a")
        check("multiword div remainder", br.toHex(), "2553f2109aceeda9c8d9a5646b4e94a0")

        // Independent small-modulus modpow cross-check (isolates modpow from the
        // 3072-bit case): 7^2019 mod 1000000 = 973143 (verified in Python).
        check("modpow small 7^2019 % 1e6",
              BigUInt(7).power(BigUInt(hex: "7e3")!, modulus: BigUInt(hex: "f4240")!).toHex(),
              "ed957") // 973143 == 0xed957

        // The critical one: g^x mod N on the Cognito 3072-bit group.
        let N = BigUInt(hex: SRP.nHex)!
        let g = BigUInt(2)
        let x = BigUInt(hex: "1234567890ABCDEF1234567890ABCDEF")!
        let want = "72763f1db9ed9ca695d9479af81531f9cee603f3d5e27cf9437ede782855c4c3"
            + "efe9ae016974f36a46dffce5c72b21c0fcd9726b8b3d829fdff8112ca9d6bc7b"
            + "89c52c80d1e7f2713c1888c68b43d46c22c6257a6ab441cb78a7ef235620e707"
            + "ce32bbb91c29cb57be52d4de173b41f5504fc40874576be271ad8e8e269c0a2e"
            + "3c601b68a52cd32f7e50ff86a9c764e8afbd052fe29dfe953de029370f78879b"
            + "187145e5cddadb0bf95f2ed3cd2e37a3bd1f706c83e002d652dea11f8768bf25"
            + "0a4cf6e338e99589a22b7a0f2b1c7039044be729caab0d4af4c55201ebfa6469"
            + "4dd75b5cf9e82c582c038214ada8638820e5787c5dba74e81d39d54f60167a1d"
            + "fdb09888eefd9db206a32682842a44f8b2f6452f616b0a84f189fb0bda4e96db"
            + "f0347f17aed6227a549577d498886ba525be77bdfebff65bc880e7bdf7c5a66d"
            + "cc42f05412efd1fdac2eec9144f69f7b3978188caa03c4628606ce8a2424c65a"
            + "16b73042bc5db262fa4a9a167e2eb59e97dfa2bd7f4d8e7fa801a6c2ae5d558e"
        check("modpow g^x mod N (3072-bit)", g.power(x, modulus: N).toHex(), want)

        // SRP signature known-answer: fixed inputs, expected value computed
        // independently by pycognito (the reference impl). Validates padHex, k,
        // u, x, S, HKDF, and the HMAC signature end-to-end — offline, no network.
        let userId = "b85123f0-e0d1-702b-6161-a5e787cf8fd5"
        let aHex = "1234567890abcdef1234567890abcdef1234567890abcdef"
        let A = BigUInt(2).power(BigUInt(hex: aHex)!, modulus: N)
        do {
            let sig = try SRP.computeSignature(
                poolName: "1YC53wwJv",
                userId: userId,
                password: "S1mpl3t0n!",
                saltHex: "a1b2c3d4e5f60718293a4b5c6d7e8f90",
                srpBHex: "2f" + String(repeating: "ab", count: 191),
                secretBlockB64: Data("the-secret-block-bytes-1234567890".utf8).base64EncodedString(),
                timestamp: "Tue Jul 7 12:34:56 UTC 2026",
                aHex: aHex, aBigA: A)
            check("SRP signature (pycognito KAT)", sig, "c6lvwT+X7HJCHwEo2wQQezH5ONGWuuhsBVrri3hJ5qk=")
        } catch {
            failures += 1
            print("  ✗ SRP signature threw: \(error)")
        }

        print(failures == 0 ? "\nAll self-tests PASSED ✓" : "\n\(failures) self-test(s) FAILED ✗")
        return failures == 0 ? 0 : 1
    }
}
