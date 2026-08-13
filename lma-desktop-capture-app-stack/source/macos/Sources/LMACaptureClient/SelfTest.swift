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

        // ── Token-refresh logic (issue #535) ────────────────────────────────
        // The decisions that determine whether an expiring token is renewed in
        // time, or a failure is (mis)read as an auth problem. Pure functions, so
        // they are checkable offline — unlike the refresh call itself, which
        // needs a live Cognito pool.
        print("\nToken refresh + failure classification:")

        // JWT `exp` extraction. Payload below is base64url of
        //   {"sub":"selftest","username":"self@example.com","exp":1767225600}
        // (1767225600 = 2026-01-01T00:00:00Z). Regenerate with:
        //   python3 -c 'import base64,json;print(base64.urlsafe_b64encode(json.dumps({...},separators=(",",":")).encode()).decode().rstrip("="))'
        let jwt = "eyJhbGciOiJSUzI1NiJ9."
            + "eyJzdWIiOiJzZWxmdGVzdCIsInVzZXJuYW1lIjoic2VsZkBleGFtcGxlLmNvbSIsImV4cCI6MTc2NzIyNTYwMH0"
            + ".not-a-real-signature"
        check("JWT exp decoded",
              TokenStore.expiry(ofJWT: jwt).map { "\(Int($0.timeIntervalSince1970))" } ?? "nil",
              "1767225600")
        check("JWT username claim decoded",
              TokenStore.jwtPayload(jwt)?["username"] as? String ?? "nil",
              "self@example.com")
        // Anything that isn't a 3-part JWT must yield nil, NOT a crash and not a
        // bogus date — an unreadable token means "don't schedule", not "expired".
        check("non-JWT token → no expiry", TokenStore.expiry(ofJWT: "pasted-opaque-token").map { "\($0)" } ?? "nil", "nil")
        check("JWT without exp → no expiry",
              TokenStore.expiry(ofJWT: "a.eyJzdWIiOiJ4In0.c").map { "\($0)" } ?? "nil", "nil")

        // When to refresh: `refreshLead` (300s) BEFORE exp, never in the past.
        let now = Date(timeIntervalSince1970: 1_767_225_600)
        func until(_ secondsToExp: TimeInterval) -> String {
            String(format: "%.0f", TokenStore.secondsUntilRefresh(
                now: now, exp: now.addingTimeInterval(secondsToExp)))
        }
        check("refresh delay, 1h token", until(3600), "3300")   // 3600 - 300 lead
        check("refresh delay, 301s left", until(301), "1")
        check("refresh delay, inside lead window", until(299), "0")
        check("refresh delay, already expired", until(-60), "0")

        func needs(_ secondsToExp: TimeInterval) -> String {
            "\(TokenStore.needsRefresh(now: now, exp: now.addingTimeInterval(secondsToExp)))"
        }
        check("needsRefresh, fresh token", needs(3600), "false")
        check("needsRefresh, 301s left", needs(301), "false")
        check("needsRefresh, 299s left", needs(299), "true")
        check("needsRefresh, expired", needs(-1), "true")
        // Unknown expiry must NOT trigger speculative refreshes — the reactive
        // path covers opaque tokens instead.
        check("needsRefresh, unknown expiry", "\(TokenStore.needsRefresh(now: now, exp: nil))", "false")

        // Auth vs transient. A false positive spends refreshes on network
        // outages; a false negative leaves an expired token unrefreshed.
        func classify(_ status: Int?, _ code: Int?) -> String {
            let err = code.map { NSError(domain: NSURLErrorDomain, code: $0) }
            return "\(TranscriberSocket.isAuthFailure(status: status, error: err))"
        }
        check("401 → auth", classify(401, nil), "true")
        check("403 → auth", classify(403, nil), "true")
        check("502 → transient", classify(502, nil), "false")
        check("101 upgrade → transient", classify(101, nil), "false")
        // -1011 badServerResponse is how URLSession reports a refused WS upgrade
        // when there is no readable HTTP response.
        check("-1011 (no response) → auth", classify(nil, NSURLErrorBadServerResponse), "true")
        check("-1013 (auth required) → auth", classify(nil, NSURLErrorUserAuthenticationRequired), "true")
        check("-1009 (offline) → transient", classify(nil, NSURLErrorNotConnectedToInternet), "false")
        check("-1001 (timeout) → transient", classify(nil, NSURLErrorTimedOut), "false")
        check("-1003 (bad host) → transient", classify(nil, NSURLErrorCannotFindHost), "false")
        check("no status, no error → transient", classify(nil, nil), "false")
        // A non-URL error (e.g. a POSIX errno) must not be read as auth.
        check("non-URL error → transient",
              "\(TranscriberSocket.isAuthFailure(status: nil, error: NSError(domain: NSPOSIXErrorDomain, code: 32)))",
              "false")

        // Reconnect backoff curve: 0.5s doubling to the 10s cap.
        func backoff(_ attempt: Int) -> String {
            String(format: "%.1f", TranscriberSocket.backoffDelay(attempt: attempt))
        }
        check("backoff #1", backoff(1), "0.5")
        check("backoff #2", backoff(2), "1.0")
        check("backoff #3", backoff(3), "2.0")
        check("backoff #4", backoff(4), "4.0")
        check("backoff #5", backoff(5), "8.0")
        check("backoff #6 (capped)", backoff(6), "10.0")
        check("backoff #20 (capped)", backoff(20), "10.0")

        // Cognito error classification: only a REJECTED credential is terminal.
        // Retrying a rejected refresh token is pointless; giving up on a 5xx or a
        // throttle would sign the user out for no reason.
        func rejected(_ err: SRP.SRPError) -> String { "\(err.isCredentialRejected)" }
        check("NotAuthorizedException → terminal",
              rejected(.http(400, "NotAuthorizedException: Refresh Token has expired")), "true")
        check("UserNotFoundException → terminal",
              rejected(.http(400, "UserNotFoundException: User does not exist")), "true")
        check("TooManyRequestsException → retryable",
              rejected(.http(400, "TooManyRequestsException: Rate exceeded")), "false")
        check("InternalErrorException → retryable",
              rejected(.http(500, "InternalErrorException: try again")), "false")
        check("malformed response → retryable", rejected(.malformed("no AuthenticationResult")), "false")

        print(failures == 0 ? "\nAll self-tests PASSED ✓" : "\n\(failures) self-test(s) FAILED ✗")
        return failures == 0 ? 0 : 1
    }
}
