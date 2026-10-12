import Darwin
import Foundation
import GramaIPC

// Host native messaging wtyczki GramaDrama. Uruchamia go przeglądarka (Edge, Chrome) na czas połączenia
// wtyczki (`chrome.runtime.connectNative`), z pochodzeniem wtyczki jako pierwszym argumentem.
// Sam niczego nie rozumie: ramki z przeglądarki (4 bajty długości + JSON) przekłada na linie JSON do gniazda
// aplikacji i z powrotem. Gdy aplikacja nie działa, czeka na nią i mówi wtyczce, że jej nie ma.

signal(SIGPIPE, SIG_IGN)

let arguments = CommandLine.arguments.dropFirst()
// Przeglądarka podaje pochodzenie wtyczki; manifest hosta i tak wpuszcza tylko naszą — to druga kontrola.
guard arguments.first == BrowserLink.extensionOrigin else {
    FileHandle.standardError.write("GramaBrowserHost: nieznane pochodzenie \(arguments.first ?? "-")\n".data(using: .utf8)!)
    exit(1)
}

/// Bundle ID przeglądarki, która nas uruchomiła: pierwszy przodek, którego plik leży w pakiecie `.app`.
func browserBundleID() -> String {
    var pid = getppid()
    for _ in 0..<4 where pid > 1 {
        var buffer = [CChar](repeating: 0, count: Int(MAXPATHLEN) * 4)
        if proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 {
            let path = String(cString: buffer)
            if let range = path.range(of: ".app/"), let id = Bundle(path: String(path[..<range.lowerBound]) + ".app")?.bundleIdentifier {
                return id
            }
        }
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.size
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        guard sysctl(&mib, 4, &info, &size, nil, 0) == 0 else { break }
        pid = info.kp_eproc.e_ppid
    }
    return "unknown"
}

let browser = browserBundleID()
let lock = NSLock()
/// Połączenie z aplikacją (-1: brak). Zapis do niego i do stdout pod `lock`.
var appFD: Int32 = -1
/// Ostatnie „hello” wtyczki — powtarzane po każdym (ponownym) połączeniu z aplikacją.
var extensionHello: Data?

func line(_ object: [String: Any]) -> Data? {
    guard var data = try? JSONSerialization.data(withJSONObject: object) else { return nil }
    data.append(0x0A)
    return data
}

func writeFrame(_ json: Data) {
    var length = UInt32(json.count).littleEndian
    var frame = Data(bytes: &length, count: 4)
    frame.append(json)
    lock.lock()
    _ = UnixSocket.writeAll(1, frame)
    lock.unlock()
}

func sendStatus(_ connected: Bool) {
    if let data = try? JSONSerialization.data(withJSONObject: ["type": "status", "app": connected]) { writeFrame(data) }
}

func readExactly(_ count: Int) -> Data? {
    var data = Data(count: count)
    var got = 0
    while got < count {
        let n = data.withUnsafeMutableBytes { read(0, $0.baseAddress! + got, count - got) }
        if n < 0, errno == EINTR { continue }
        guard n > 0 else { return nil }
        got += n
    }
    return data
}

// Aplikacja → wtyczka: połączenie z gniazdem (z ponawianiem, póki aplikacja nie działa), potem linie na ramki.
// Koniec tego połączenia (aplikacja zamknięta, przebudowana albo nas odrzuciła) = koniec hosta: wtyczka od razu
// uruchamia nowy. Po instalacji nowej wersji stary proces ma podmieniony plik na dysku, więc jego podpis już się
// nie sprawdza — ponawianie z tego samego procesu nigdy by się nie udało.
Thread.detachNewThread {
    while true {
        guard let fd = UnixSocket.connect(to: BrowserLink.socketPath) else {
            sleep(2)
            continue
        }
        UnixSocket.setSendTimeout(fd, 1)
        guard PeerCheck.verify(fd: fd, identifier: "pl.gramadrama.app") else {
            close(fd)
            sleep(5)
            continue
        }
        lock.lock()
        appFD = fd
        var greeting: [Data] = []
        if let hello = line(["type": "host", "browser": browser, "pid": Int(getpid())]) { greeting.append(hello) }
        if let hello = extensionHello { greeting.append(hello) }
        for data in greeting { _ = UnixSocket.writeAll(fd, data) }
        lock.unlock()
        sendStatus(true)

        let reader = LineReader(fd: fd, maxBytes: BrowserLink.maxMessageBytes)
        while let data = reader.next() {
            guard var object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { continue }
            // Termin prośby jest w zegarze Maca (`systemUptime`) — wtyczka dostaje, ile ms zostało.
            if let deadline = object["deadline"] as? Double {
                object["expiresInMs"] = max(0, Int((deadline - ProcessInfo.processInfo.systemUptime) * 1000))
                object["deadline"] = nil
            }
            guard let json = try? JSONSerialization.data(withJSONObject: object) else { continue }
            writeFrame(json)
        }
        lock.lock()
        appFD = -1
        close(fd)
        lock.unlock()
        sendStatus(false)
        exit(0)
    }
}

// Wtyczka → aplikacja. Koniec stdin = przeglądarka zamknęła połączenie: kończymy.
while let header = readExactly(4) {
    let length = Int(header.withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) }.littleEndian)
    guard length > 0, length <= BrowserLink.maxMessageBytes, let body = readExactly(length) else { break }
    // Ponowne kodowanie daje jedną linię (JSON z przeglądarki może mieć dowolne odstępy).
    guard let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any], let data = line(object) else { continue }
    lock.lock()
    if object["type"] as? String == "hello" { extensionHello = data }
    if appFD >= 0, !UnixSocket.writeAll(appFD, data) {
        shutdown(appFD, SHUT_RDWR)
    }
    lock.unlock()
}
exit(0)
