import Foundation
import CoreAudio

/// Enumerate audio INPUT devices and resolve a persisted selection, for the
/// Settings mic picker.
///
/// Devices are identified by their CoreAudio UID (a stable string like
/// "BuiltInMicrophoneDevice" or a USB serial-derived id) — NOT the transient
/// AudioDeviceID integer, which changes across reboots/hotplug. An empty UID
/// means "System Default": follow whatever the user picks in Sound settings
/// (the pre-Settings behavior, and the fallback when a chosen device is
/// unplugged).
enum MicDevices {
    struct Device: Identifiable, Equatable {
        let uid: String
        let name: String
        var id: String { uid }
    }

    /// All devices that have at least one input stream, sorted by name.
    static func list() -> [Device] {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr,
              size > 0 else { return [] }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr
        else { return [] }

        return ids.compactMap { devId -> Device? in
            guard inputChannelCount(devId) > 0,
                  let uid = stringProperty(devId, kAudioDevicePropertyDeviceUID),
                  let name = stringProperty(devId, kAudioObjectPropertyName) else { return nil }
            return Device(uid: uid, name: name)
        }
        .sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }

    /// AudioDeviceID for a stored UID, or nil if that device isn't connected
    /// right now (caller should fall back to the system default).
    static func deviceID(forUID uid: String) -> AudioDeviceID? {
        guard !uid.isEmpty else { return nil }
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr
        else { return nil }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr
        else { return nil }
        return ids.first { stringProperty($0, kAudioDevicePropertyDeviceUID) == uid }
    }

    // MARK: - CoreAudio property helpers

    private static func inputChannelCount(_ devId: AudioDeviceID) -> Int {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(devId, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
        let bufListPtr = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { bufListPtr.deallocate() }
        guard AudioObjectGetPropertyData(devId, &addr, 0, nil, &size, bufListPtr) == noErr else { return 0 }
        let bufList = bufListPtr.assumingMemoryBound(to: AudioBufferList.self)
        return UnsafeMutableAudioBufferListPointer(bufList).reduce(0) { $0 + Int($1.mNumberChannels) }
    }

    private static func stringProperty(_ devId: AudioDeviceID, _ selector: AudioObjectPropertySelector) -> String? {
        var addr = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var cf: CFString? = nil
        var size = UInt32(MemoryLayout<CFString?>.size)
        let err = withUnsafeMutablePointer(to: &cf) { ptr in
            AudioObjectGetPropertyData(devId, &addr, 0, nil, &size, ptr)
        }
        guard err == noErr, let s = cf else { return nil }
        return s as String
    }
}
