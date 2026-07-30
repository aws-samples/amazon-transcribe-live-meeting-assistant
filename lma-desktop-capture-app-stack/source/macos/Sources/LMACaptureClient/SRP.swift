import Foundation
import CryptoKit

/// Cognito USER_SRP_AUTH login — the SAME flow the LMA web UI uses (Amplify
/// defaults to SRP), implemented dependency-free so this client stays SDK-free.
///
/// Two round-trips to the Cognito Identity Provider JSON API (no SigV4 needed —
/// these two calls are unauthenticated):
///   1. InitiateAuth (USER_SRP_AUTH)  → server salt, SRP_B, secret block
///   2. RespondToAuthChallenge (PASSWORD_VERIFIER) → tokens (access/id/refresh)
///
/// The password is NEVER sent: SRP proves knowledge of it via a challenge. This
/// matches the security posture of the web UI exactly. SRP-6a math per the
/// Cognito variant (SHA-256, the quirky pad_hex, HKDF-derived key, timestamped
/// HMAC signature) — validated byte-for-byte against pycognito in SelfTest.
///
/// NOTE: This is the dependency-free path. AWS Amplify Swift is the recommended
/// PRODUCTION auth (adds token refresh + Keychain session persistence + MFA),
/// but Amplify's Auth plugin requires the Data Protection Keychain, which needs
/// a keychain-access-groups / application-identifier entitlement and therefore
/// a real Apple signing identity — it cannot run under an ad-hoc-signed local
/// build. This SRP path has no such requirement, so it works for the prototype.
enum SRP {
    /// RFC 3526 3072-bit MODP group prime — the group Cognito uses. Generator g=2.
    static let nHex =
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
        "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"

    struct Tokens { let accessToken: String; let idToken: String; let refreshToken: String }

    enum SRPError: Error, CustomStringConvertible {
        case http(Int, String)
        case malformed(String)
        case challenge(String)
        var description: String {
            switch self {
            case .http(let c, let b): return "Cognito HTTP \(c): \(b)"
            case .malformed(let m): return "Malformed Cognito response: \(m)"
            case .challenge(let c): return "Unexpected auth challenge: \(c)"
            }
        }
    }

    // MARK: - Public entry point

    /// Perform the full SRP login. `region` derives the endpoint; `clientId` is
    /// the Cognito App Client ID; `poolId` is the full user pool id (region_xxx).
    static func login(username: String, password: String,
                      poolId: String, clientId: String, region: String) async throws -> Tokens {
        let N = BigUInt(hex: nHex)!
        let g = BigUInt(2)

        // Client ephemeral: a random, A = g^a mod N.
        let a = BigUInt.random(bytes: 128) % N
        let A = g.power(a, modulus: N)
        if (A % N).isZero { throw SRPError.malformed("A % N == 0") }
        let srpA = A.toHex()

        // 1) InitiateAuth (USER_SRP_AUTH)
        let init1: [String: Any] = [
            "AuthFlow": "USER_SRP_AUTH",
            "ClientId": clientId,
            "AuthParameters": ["USERNAME": username, "SRP_A": srpA],
        ]
        let r1 = try await call(region: region, target: "InitiateAuth", body: init1)
        guard let challenge = r1["ChallengeName"] as? String, challenge == "PASSWORD_VERIFIER",
              let params = r1["ChallengeParameters"] as? [String: Any],
              let saltHex = params["SALT"] as? String,
              let srpBHex = params["SRP_B"] as? String,
              let secretBlock = params["SECRET_BLOCK"] as? String,
              let userIdForSrp = params["USER_ID_FOR_SRP"] as? String else {
            throw SRPError.challenge(r1["ChallengeName"] as? String ?? "no PASSWORD_VERIFIER challenge")
        }
        let poolName = poolId.contains("_") ? String(poolId.split(separator: "_", maxSplits: 1)[1]) : poolId

        // Compute the password-proof signature (Cognito SRP variant). Extracted
        // into a pure function so it can be validated offline against a
        // pycognito-derived known-answer (see SelfTest.run()).
        let timestamp = cognitoTimestamp()
        let signature = try computeSignature(
            poolName: poolName, userId: userIdForSrp, password: password,
            saltHex: saltHex, srpBHex: srpBHex, secretBlockB64: secretBlock,
            timestamp: timestamp, aHex: a.toHex(), aBigA: A)

        // 2) RespondToAuthChallenge (PASSWORD_VERIFIER)
        let resp: [String: Any] = [
            "ChallengeName": "PASSWORD_VERIFIER",
            "ClientId": clientId,
            "ChallengeResponses": [
                "USERNAME": userIdForSrp,
                "PASSWORD_CLAIM_SECRET_BLOCK": secretBlock,
                "PASSWORD_CLAIM_SIGNATURE": signature,
                "TIMESTAMP": timestamp,
            ],
        ]
        let r2 = try await call(region: region, target: "RespondToAuthChallenge", body: resp)
        if let next = r2["ChallengeName"] as? String {
            throw SRPError.challenge("server requires additional challenge '\(next)' (e.g. MFA / new password) — not supported by this prototype")
        }
        guard let auth = r2["AuthenticationResult"] as? [String: Any],
              let access = auth["AccessToken"] as? String,
              let id = auth["IdToken"] as? String else {
            throw SRPError.malformed("no AuthenticationResult in RespondToAuthChallenge")
        }
        let refresh = auth["RefreshToken"] as? String ?? ""
        return Tokens(accessToken: access, idToken: id, refreshToken: refresh)
    }

    // MARK: - Cognito JSON API transport

    private static func call(region: String, target: String, body: [String: Any]) async throws -> [String: Any] {
        let url = URL(string: "https://cognito-idp.\(region).amazonaws.com/")!
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/x-amz-json-1.1", forHTTPHeaderField: "Content-Type")
        req.setValue("AWSCognitoIdentityProviderService.\(target)", forHTTPHeaderField: "X-Amz-Target")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await URLSession.shared.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        guard code == 200 else {
            // Cognito error bodies look like {"__type":"NotAuthorizedException","message":"..."}
            let type = obj["__type"] as? String ?? "HTTPError"
            let m = obj["message"] as? String ?? String(data: data, encoding: .utf8) ?? ""
            throw SRPError.http(code, "\(type): \(m)")
        }
        return obj
    }

    // MARK: - Signature computation (Cognito SRP variant, hex-based)

    /// Pure, deterministic computation of the PASSWORD_VERIFIER signature.
    /// Mirrors pycognito exactly (validated byte-for-byte in SelfTest.run()):
    ///   k   = hexHash("00" + N_HEX + "0" + G_HEX)
    ///   u   = hexHash(padHex(A) + padHex(B))
    ///   x   = hexHash(padHex(salt) + sha256hex(poolName+userId+":"+password))
    ///   S   = (B - k*g^x)^(a + u*x) mod N
    ///   key = HKDF(ikm=padHex(S) bytes, salt=padHex(u) bytes)  [Caldera Derived Key, 16B]
    ///   sig = base64( HMAC-SHA256(key, poolName|userId|secretBlock|timestamp) )
    /// `aBigA` is the already-computed A (avoids recomputing g^a).
    static func computeSignature(poolName: String, userId: String, password: String,
                                 saltHex: String, srpBHex: String, secretBlockB64: String,
                                 timestamp: String, aHex: String, aBigA: BigUInt) throws -> String {
        let N = BigUInt(hex: nHex)!
        let g = BigUInt(2)
        let A = aBigA
        let B = BigUInt(hex: srpBHex)!
        if (B % N).isZero { throw SRPError.malformed("B % N == 0") }
        let a = BigUInt(hex: aHex)!

        // k = hexHash("00" + N_HEX + "0" + G_HEX)   (note the literal "0" before g)
        let k = BigUInt(hex: hexHash("00" + nHex + "0" + "2"))!

        // u = hexHash(padHex(A) + padHex(B))
        let u = BigUInt(hex: hexHash(padHex(A.toHex()) + padHex(B.toHex())))!
        if u.isZero { throw SRPError.malformed("u == 0") }

        // x = hexHash( padHex(salt) + sha256hex(poolName + userId + ":" + password) )
        let idHashHex = sha256hex(Array("\(poolName)\(userId):\(password)".utf8))
        let x = BigUInt(hex: hexHash(padHex(saltHex) + idHashHex))!

        // S = (B - k*g^x)^(a + u*x) mod N
        let gx = g.power(x, modulus: N)
        let kgx = (k * gx) % N
        let base = (B + N - kgx) % N          // (B - k*g^x) mod N, underflow-safe
        let exp = a + (u * x)
        let S = base.power(exp, modulus: N)

        // HKDF (Caldera Derived Key), 16-byte key.
        let ikm = hexToBytes(padHex(S.toHex()))
        let saltBytes = hexToBytes(padHex(u.toHex()))
        let key = derivedKey(ikm: ikm, salt: saltBytes)

        guard let secretBlockData = Data(base64Encoded: secretBlockB64) else {
            throw SRPError.malformed("SECRET_BLOCK not base64")
        }
        var msg = [UInt8]()
        msg += Array(poolName.utf8)
        msg += Array(userId.utf8)
        msg += [UInt8](secretBlockData)
        msg += Array(timestamp.utf8)
        let sig = hmacSHA256(key: key, message: msg)
        return Data(sig).base64EncodedString()
    }

    // MARK: - Crypto helpers

    private static func sha256(_ bytes: [UInt8]) -> [UInt8] { Array(SHA256.hash(data: Data(bytes))) }

    /// SHA-256 as a lowercase hex string, zero-padded to 64 chars (Cognito's
    /// hash_sha256 does this so leading-zero digests keep full width).
    private static func sha256hex(_ bytes: [UInt8]) -> String {
        let h = sha256(bytes).map { String(format: "%02x", $0) }.joined()
        return String(repeating: "0", count: 64 - h.count) + h
    }

    /// hex_hash: SHA-256 of the bytes decoded from a hex string, as hex.
    private static func hexHash(_ hexString: String) -> String {
        sha256hex(hexToBytes(hexString))
    }

    /// Cognito pad_hex: ensure even length; if the top nibble is 8..F prepend
    /// "00" (a sign byte) so the value reads as unsigned. NOT full-width padding.
    static func padHex(_ hexIn: String) -> String {
        var h = hexIn
        if h.count % 2 == 1 {
            h = "0" + h
        } else if let first = h.first, "89abcdefABCDEF".contains(first) {
            h = "00" + h
        }
        return h
    }

    private static func hexToBytes(_ hex: String) -> [UInt8] {
        var bytes = [UInt8](); bytes.reserveCapacity(hex.count / 2)
        var idx = hex.startIndex
        while idx < hex.endIndex, let next = hex.index(idx, offsetBy: 2, limitedBy: hex.endIndex) {
            if let b = UInt8(hex[idx..<next], radix: 16) { bytes.append(b) }
            idx = next
        }
        return bytes
    }

    private static func hmacSHA256(key: [UInt8], message: [UInt8]) -> [UInt8] {
        let mac = HMAC<SHA256>.authenticationCode(for: Data(message), using: SymmetricKey(data: Data(key)))
        return Array(mac)
    }

    /// Cognito "Caldera Derived Key" HKDF: prk = HMAC(salt, ikm);
    /// okm = HMAC(prk, "Caldera Derived Key"||0x01); first 16 bytes.
    private static func derivedKey(ikm: [UInt8], salt: [UInt8]) -> [UInt8] {
        let info = Array("Caldera Derived Key".utf8) + [0x01]
        let prk = hmacSHA256(key: salt, message: ikm)
        let okm = hmacSHA256(key: prk, message: info)
        return Array(okm.prefix(16))
    }

    /// Cognito timestamp: "EEE MMM d HH:mm:ss 'UTC' yyyy", en-US, UTC, no
    /// leading zero on day-of-month (matches pycognito's format exactly).
    private static func cognitoTimestamp() -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "EEE MMM d HH:mm:ss 'UTC' yyyy"
        return f.string(from: Date())
    }
}
