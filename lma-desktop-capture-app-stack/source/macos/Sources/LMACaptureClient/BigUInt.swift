import Foundation

/// Minimal unsigned big integer — just enough for Cognito SRP (modular
/// exponentiation on a 3072-bit group). Dependency-free on purpose: no SDK,
/// no external package. Not constant-time and not a general-purpose bignum;
/// scoped to the SRP handshake and validated against known-answer tests
/// (see `--selftest`).
///
/// Representation: little-endian base-2^32 words, normalized (no trailing zero
/// words); an empty word array means zero.
struct BigUInt: Equatable, Comparable {
    private(set) var words: [UInt32]

    init() { words = [] }
    init(_ v: UInt32) { words = v == 0 ? [] : [v] }
    private init(rawWords: [UInt32]) { words = rawWords; normalize() }

    private mutating func normalize() {
        while let last = words.last, last == 0 { words.removeLast() }
    }

    var isZero: Bool { words.isEmpty }

    // MARK: - Conversions

    /// Big-endian bytes → BigUInt.
    init(bigEndianBytes bytes: [UInt8]) {
        var w = [UInt32]()
        var i = bytes.count
        while i > 0 {
            var word: UInt32 = 0
            var shift: UInt32 = 0
            var c = 0
            while c < 4 && i > 0 {
                i -= 1
                word |= UInt32(bytes[i]) << shift
                shift += 8; c += 1
            }
            w.append(word)
        }
        self.init(rawWords: w)
    }

    init?(hex: String) {
        var h = hex.hasPrefix("0x") ? String(hex.dropFirst(2)) : hex
        h = h.filter { !$0.isWhitespace }
        if h.isEmpty { self.init(); return }
        if h.count % 2 != 0 { h = "0" + h }
        var bytes = [UInt8]()
        var idx = h.startIndex
        while idx < h.endIndex {
            let next = h.index(idx, offsetBy: 2)
            guard let byte = UInt8(h[idx..<next], radix: 16) else { return nil }
            bytes.append(byte); idx = next
        }
        self.init(bigEndianBytes: bytes)
    }

    /// Minimal-length big-endian bytes (empty for zero).
    func toBigEndianBytes() -> [UInt8] {
        if words.isEmpty { return [] }
        var bytes = [UInt8]()
        for word in words {
            bytes.append(UInt8(word & 0xff))
            bytes.append(UInt8((word >> 8) & 0xff))
            bytes.append(UInt8((word >> 16) & 0xff))
            bytes.append(UInt8((word >> 24) & 0xff))
        }
        bytes.reverse() // little-endian words → big-endian bytes
        while bytes.count > 1 && bytes.first == 0 { bytes.removeFirst() }
        return bytes
    }

    /// Big-endian bytes zero-padded (on the left) to at least `minLength`.
    /// Used for SRP's PAD() where values must be fixed-width to the modulus.
    func toBytes(minLength: Int) -> [UInt8] {
        var b = toBigEndianBytes()
        if b.count < minLength {
            b = [UInt8](repeating: 0, count: minLength - b.count) + b
        }
        return b
    }

    func toHex() -> String {
        let bytes = toBigEndianBytes()
        if bytes.isEmpty { return "0" }
        // Top byte without a leading zero nibble (matches Python's format(x,'x')).
        var s = String(bytes[0], radix: 16)
        for b in bytes.dropFirst() { s += String(format: "%02x", b) }
        return s
    }

    // MARK: - Bit access

    var bitWidth: Int {
        guard let last = words.last else { return 0 }
        return (words.count - 1) * 32 + (32 - last.leadingZeroBitCount)
    }

    func bit(_ i: Int) -> Bool {
        let w = i / 32, b = i % 32
        if w >= words.count { return false }
        return (words[w] >> UInt32(b)) & 1 == 1
    }

    // MARK: - Comparison

    static func < (l: BigUInt, r: BigUInt) -> Bool {
        if l.words.count != r.words.count { return l.words.count < r.words.count }
        var i = l.words.count - 1
        while i >= 0 {
            if l.words[i] != r.words[i] { return l.words[i] < r.words[i] }
            i -= 1
        }
        return false
    }

    // MARK: - Arithmetic

    static func + (l: BigUInt, r: BigUInt) -> BigUInt {
        var out = [UInt32](); var carry: UInt64 = 0
        let n = max(l.words.count, r.words.count)
        out.reserveCapacity(n + 1)
        for i in 0..<n {
            let lw = i < l.words.count ? UInt64(l.words[i]) : 0
            let rw = i < r.words.count ? UInt64(r.words[i]) : 0
            let s = lw + rw + carry
            out.append(UInt32(s & 0xffffffff)); carry = s >> 32
        }
        if carry > 0 { out.append(UInt32(carry)) }
        return BigUInt(rawWords: out)
    }

    /// Precondition: l >= r.
    static func - (l: BigUInt, r: BigUInt) -> BigUInt {
        var out = [UInt32](); var borrow: Int64 = 0
        out.reserveCapacity(l.words.count)
        for i in 0..<l.words.count {
            let lw = Int64(l.words[i])
            let rw = i < r.words.count ? Int64(r.words[i]) : 0
            var d = lw - rw - borrow
            if d < 0 { d += (1 << 32); borrow = 1 } else { borrow = 0 }
            out.append(UInt32(d))
        }
        return BigUInt(rawWords: out)
    }

    static func * (l: BigUInt, r: BigUInt) -> BigUInt {
        if l.isZero || r.isZero { return BigUInt() }
        var out = [UInt32](repeating: 0, count: l.words.count + r.words.count)
        for i in 0..<l.words.count {
            var carry: UInt64 = 0
            let lw = UInt64(l.words[i])
            for j in 0..<r.words.count {
                let idx = i + j
                let p = lw * UInt64(r.words[j]) + UInt64(out[idx]) + carry
                out[idx] = UInt32(p & 0xffffffff); carry = p >> 32
            }
            var idx = i + r.words.count
            while carry > 0 {
                let s = UInt64(out[idx]) + carry
                out[idx] = UInt32(s & 0xffffffff); carry = s >> 32; idx += 1
            }
        }
        return BigUInt(rawWords: out)
    }

    static func % (l: BigUInt, r: BigUInt) -> BigUInt { l.quotientAndRemainder(dividingBy: r).remainder }

    // MARK: - Division (Knuth Algorithm D, base 2^32)

    func quotientAndRemainder(dividingBy divisor: BigUInt) -> (quotient: BigUInt, remainder: BigUInt) {
        precondition(!divisor.isZero, "BigUInt division by zero")
        if self < divisor { return (BigUInt(), self) }
        if divisor.words.count == 1 { return divmodSmall(divisor.words[0]) }

        let b: UInt64 = 1 << 32
        let n = divisor.words.count
        let m = words.count - n

        // D1. Normalize so the divisor's top word has its high bit set.
        let shift = divisor.words[n - 1].leadingZeroBitCount
        let v = divisor.shiftedLeft(bits: shift)
        var u = shiftedLeft(bits: shift).words
        while u.count < m + n + 1 { u.append(0) }

        var q = [UInt32](repeating: 0, count: m + 1)
        let vHigh = UInt64(v.words[n - 1])
        let vSecond = UInt64(v.words[n - 2])

        var j = m
        while j >= 0 {
            // D3. Estimate qhat.
            let dividend = (UInt64(u[j + n]) << 32) | UInt64(u[j + n - 1])
            var qhat = dividend / vHigh
            var rhat = dividend % vHigh
            while qhat >= b || qhat * vSecond > (rhat << 32) + UInt64(u[j + n - 2]) {
                qhat -= 1; rhat += vHigh
                if rhat >= b { break }
            }
            // D4. Multiply and subtract.
            var borrow: Int64 = 0
            var carry: UInt64 = 0
            for i in 0..<n {
                let p = qhat * UInt64(v.words[i]) + carry
                carry = p >> 32
                let sub = Int64(u[j + i]) - borrow - Int64(p & 0xffffffff)
                if sub < 0 { u[j + i] = UInt32(sub + (1 << 32)); borrow = 1 }
                else { u[j + i] = UInt32(sub); borrow = 0 }
            }
            let sub = Int64(u[j + n]) - borrow - Int64(carry)
            if sub < 0 { u[j + n] = UInt32(sub + (1 << 32)); borrow = 1 }
            else { u[j + n] = UInt32(sub); borrow = 0 }
            // D6. If we over-subtracted, add the divisor back and fix qhat.
            if borrow != 0 {
                qhat -= 1
                var c: UInt64 = 0
                for i in 0..<n {
                    let s = UInt64(u[j + i]) + UInt64(v.words[i]) + c
                    u[j + i] = UInt32(s & 0xffffffff); c = s >> 32
                }
                u[j + n] = UInt32((UInt64(u[j + n]) + c) & 0xffffffff)
            }
            q[j] = UInt32(qhat)
            j -= 1
        }
        // D8. Denormalize the remainder.
        let rem = BigUInt(rawWords: Array(u[0..<n])).shiftedRight(bits: shift)
        return (BigUInt(rawWords: q), rem)
    }

    private func divmodSmall(_ d: UInt32) -> (BigUInt, BigUInt) {
        var q = [UInt32](repeating: 0, count: words.count)
        var rem: UInt64 = 0
        var i = words.count - 1
        while i >= 0 {
            let cur = (rem << 32) | UInt64(words[i])
            q[i] = UInt32(cur / UInt64(d)); rem = cur % UInt64(d); i -= 1
        }
        return (BigUInt(rawWords: q), BigUInt(UInt32(rem)))
    }

    // MARK: - Shifts

    func shiftedLeft(bits: Int) -> BigUInt {
        if bits == 0 || isZero { return self }
        let wordShift = bits / 32, bitShift = bits % 32
        var out = [UInt32](repeating: 0, count: wordShift)
        if bitShift == 0 {
            out.append(contentsOf: words)
        } else {
            var carry: UInt32 = 0
            for w in words {
                out.append((w << UInt32(bitShift)) | carry)
                carry = w >> UInt32(32 - bitShift)
            }
            if carry > 0 { out.append(carry) }
        }
        return BigUInt(rawWords: out)
    }

    func shiftedRight(bits: Int) -> BigUInt {
        if bits == 0 || isZero { return self }
        let wordShift = bits / 32, bitShift = bits % 32
        if wordShift >= words.count { return BigUInt() }
        let src = Array(words[wordShift...])
        if bitShift == 0 { return BigUInt(rawWords: src) }
        var out = [UInt32](repeating: 0, count: src.count)
        var carry: UInt32 = 0
        var i = src.count - 1
        while i >= 0 {
            let w = src[i]
            out[i] = (w >> UInt32(bitShift)) | carry
            carry = w << UInt32(32 - bitShift)
            i -= 1
        }
        return BigUInt(rawWords: out)
    }

    // MARK: - Modular exponentiation (square-and-multiply)

    func power(_ exponent: BigUInt, modulus: BigUInt) -> BigUInt {
        precondition(!modulus.isZero, "modpow with zero modulus")
        if modulus == BigUInt(1) { return BigUInt() }
        var result = BigUInt(1)
        var base = self % modulus
        let bits = exponent.bitWidth
        var i = 0
        while i < bits {
            if exponent.bit(i) { result = (result * base) % modulus }
            i += 1
            if i < bits { base = (base * base) % modulus }
        }
        return result
    }

    // MARK: - Random

    /// Cryptographically-random BigUInt of `bytes` bytes (top bit not forced;
    /// SRP reduces the private value mod N anyway).
    static func random(bytes: Int) -> BigUInt {
        var rng = SystemRandomNumberGenerator()
        var b = [UInt8](repeating: 0, count: bytes)
        for i in 0..<bytes { b[i] = UInt8.random(in: 0...255, using: &rng) }
        return BigUInt(bigEndianBytes: b)
    }
}
