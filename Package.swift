// swift-tools-version:5.10
import PackageDescription

/// Swift Testing z Command Line Tools: framework i lib_TestingInterop.dylib, których szuka test przy starcie.
let commandLineToolsTesting = ["/Library/Developer/CommandLineTools/Library/Developer/Frameworks",
                               "/Library/Developer/CommandLineTools/Library/Developer/usr/lib"]

let package = Package(
    name: "GramaDrama",
    platforms: [.macOS(.v14)],
    dependencies: [
        // WhisperKit (dawniej argmaxinc/WhisperKit): Whisper na Core ML / Neural Engine, do dyktowania.
        .package(url: "https://github.com/argmaxinc/argmax-oss-swift", exact: "1.1.0"),
    ],
    targets: [
        // Czysta logika bez macOS-owych API (AX, schowek, okna): język, tryby, prompty, post-processing, diff.
        // To ta część, którą wersja na Linuksa powiela — wspólne przypadki w tests/ i bench/.
        .target(name: "GramaCore", dependencies: ["GramaIMEProtocol"], path: "Sources/GramaCore"),
        // Protokół aplikacja ↔ metoda wprowadzania i tabela aplikacji — czyste typy, bez API macOS.
        // Osobno od GramaCore, żeby binarka IME zmieniała się tylko przy zmianach w tym, czego sama używa.
        .target(name: "GramaIMEProtocol", path: "Sources/GramaIMEProtocol"),
        // Gniazdo Unix i sprawdzanie podpisu procesu po drugiej stronie (wspólne dla aplikacji i IME).
        .target(name: "GramaIPC", dependencies: ["GramaIMEProtocol"], path: "Sources/GramaIPC"),
        // Łapanie wyjątków Objective-C z wywołań IMK (martwy klient rzuca NSException, której Swift nie złapie).
        .target(name: "GramaIMEShim", path: "Sources/GramaIMEShim"),
        // Metoda wprowadzania „Polski (GramaDrama)”: przepuszcza klawisze, podaje i podmienia tekst pola.
        .executableTarget(
            name: "GramaIME",
            dependencies: ["GramaIMEProtocol", "GramaIPC", "GramaIMEShim"],
            path: "Sources/GramaIME",
            linkerSettings: [.linkedFramework("InputMethodKit"), .linkedLibrary("bsm")]),
        // Host native messaging wtyczki przeglądarki: przekłada ramki Edge/Chrome na linie gniazda aplikacji.
        // Osobny, mały program w pakiecie aplikacji (bez AppKit), z własnym identyfikatorem podpisu.
        .executableTarget(name: "GramaBrowserHost", dependencies: ["GramaIPC"], path: "Sources/GramaBrowserHost",
                          linkerSettings: [.linkedLibrary("bsm")]),
        // Rozmowa ze skonfigurowanymi usługami (Ollama, LanguageTool), wspólna pamięć zapytań, ustawienia i log zdarzeń.
        .target(name: "GramaBackend", dependencies: ["GramaCore"], path: "Sources/GramaBackend"),
        // Aplikacja na macOS: Accessibility, schowek, skróty, panel, sprawdzanie na żywo, dyktowanie.
        .executableTarget(
            name: "GramaDrama",
            dependencies: ["GramaCore", "GramaBackend", "GramaIMEProtocol", "GramaIPC",
                           .product(name: "WhisperKit", package: "argmax-oss-swift")],
            path: "Sources/GramaDrama",
            linkerSettings: [.linkedLibrary("bsm")]),
        // Swift Testing z samych Command Line Tools (bez Xcode): uruchamiaj przez scripts/test.sh, który podaje
        // ścieżkę do frameworka wszystkim celom (także plikowi uruchamiającemu testy, który generuje SwiftPM).
        .testTarget(
            name: "GramaCoreTests", dependencies: ["GramaCore", "GramaIMEProtocol"], path: "tests/GramaCoreTests",
            linkerSettings: [.unsafeFlags(commandLineToolsTesting.flatMap { ["-Xlinker", "-rpath", "-Xlinker", $0] })]),
    ]
)
