// Generuje ikonę aplikacji GramaDrama (AppIcon.icns) i podgląd marki.
// Użycie: swift scripts/make-icon.swift <katalog-wyjściowy>
//
// Logo (branding/BRAND.md): dwie maski — wesoła z przodu, smutna lekko przygaszona za nią — jasne na płaskim
// lawendowym zaokrąglonym kwadracie, bez gradientu. Kształt masek czyta z linux/icon.svg (jedno źródło logo),
// a kwadrat jest na siatce ikon macOS.
import AppKit

let outDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "build"
let fm = FileManager.default
try? fm.createDirectory(atPath: outDir, withIntermediateDirectories: true)

// Paleta marki
let lavender = NSColor(srgbRed: 0x66 / 255.0, green: 0x57 / 255.0, blue: 0x9E / 255.0, alpha: 1)  // #66579E
let paper = NSColor(srgbRed: 0xFF / 255.0, green: 0xFE / 255.0, blue: 0xFC / 255.0, alpha: 1)     // #FFFEFC

// linux/icon.svg: kwadrat x=4 y=4 120×120, maski w układzie 64×64 przesunięte o (13.39, 13.16) i skalowane 1.55.
let scriptDir = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
let svg = try! String(contentsOf: scriptDir.appendingPathComponent("../linux/icon.svg").standardizedFileURL, encoding: .utf8)
func svgNumber(_ pattern: String) -> CGFloat {
    let regex = try! NSRegularExpression(pattern: pattern)
    let match = regex.firstMatch(in: svg, range: NSRange(svg.startIndex..., in: svg))!
    return CGFloat(Double(svg[Range(match.range(at: 1), in: svg)!])!)
}
func maskPath(_ id: String) -> String {
    let regex = try! NSRegularExpression(pattern: "id=\"\(id)\"[^>]*\\sd=\"([^\"]+)\"")
    let match = regex.firstMatch(in: svg, range: NSRange(svg.startIndex..., in: svg))!
    return String(svg[Range(match.range(at: 1), in: svg)!])
}
let maskX = svgNumber("translate\\(([0-9.]+) "), maskY = svgNumber("translate\\([0-9.]+ ([0-9.]+)\\)")
let maskScale = svgNumber("scale\\(([0-9.]+)\\)")
let sadOpacity = svgNumber("id=\"sad-mask\" opacity=\"([0-9.]+)\"")

/// Ścieżka SVG z bezwzględnymi M, L, C i Z, przeniesiona przez `point`.
func bezier(_ d: String, _ point: (CGFloat, CGFloat) -> NSPoint) -> NSBezierPath {
    let path = NSBezierPath()
    path.windingRule = .evenOdd  // oczy i usta są wycięte
    var command = Character("M"), n: [CGFloat] = []
    for var token in d.split(separator: " ") {
        // Litera polecenia stoi przy pierwszej liczbie („M35.6”).
        if let letter = token.first, letter.isLetter {
            command = letter
            n = []
            if letter == "Z" { path.close() }
            token = token.dropFirst()
        }
        guard let value = Double(token) else { continue }
        n.append(CGFloat(value))
        switch command {
        case "M" where n.count == 2: path.move(to: point(n[0], n[1])); n = []
        case "L" where n.count == 2: path.line(to: point(n[0], n[1])); n = []
        case "C" where n.count == 6:
            path.curve(to: point(n[4], n[5]), controlPoint1: point(n[0], n[1]), controlPoint2: point(n[2], n[3])); n = []
        default: break
        }
    }
    return path
}

func renderIcon(size: CGFloat) -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = size / 1024

    // Siatka ikon macOS: korpus 824 px z marginesem 100 px
    let body = NSRect(x: 100 * s, y: 100 * s, width: 824 * s, height: 824 * s)
    let path = NSBezierPath(roundedRect: body, xRadius: 185 * s, yRadius: 185 * s)

    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.30)
    shadow.shadowBlurRadius = 24 * s
    shadow.shadowOffset = NSSize(width: 0, height: -10 * s)
    NSGraphicsContext.saveGraphicsState()
    shadow.set()
    lavender.setFill()
    path.fill()
    NSGraphicsContext.restoreGraphicsState()

    // Kwadrat 120 jednostek SVG = korpus 824 px; oś y SVG idzie w dół.
    let unit = 824 * s / 120
    let point = { (x: CGFloat, y: CGFloat) -> NSPoint in
        NSPoint(x: body.minX + (maskX + maskScale * x - 4) * unit, y: body.maxY - (maskY + maskScale * y - 4) * unit)
    }
    paper.withAlphaComponent(sadOpacity).setFill()
    bezier(maskPath("sad-mask"), point).fill()
    paper.setFill()
    bezier(maskPath("happy-mask"), point).fill()

    NSGraphicsContext.restoreGraphicsState()
    return rep
}

func save(_ rep: NSBitmapImageRep, _ path: String) {
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
}

let iconset = "\(outDir)/AppIcon.iconset"
try? fm.createDirectory(atPath: iconset, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    save(renderIcon(size: CGFloat(base)), "\(iconset)/icon_\(base)x\(base).png")
    save(renderIcon(size: CGFloat(base * 2)), "\(iconset)/icon_\(base)x\(base)@2x.png")
}
save(renderIcon(size: 1024), "\(outDir)/AppIcon-1024.png")

let task = Process()
task.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
task.arguments = ["-c", "icns", iconset, "-o", "\(outDir)/AppIcon.icns"]
try task.run()
task.waitUntilExit()
print("✓ \(outDir)/AppIcon.icns")
