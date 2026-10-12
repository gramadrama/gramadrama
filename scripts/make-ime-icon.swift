// Ikona źródła klawiatury w menu: maski z paska menu GramaDramy, szablon (czarny z przezroczystością),
// 16 i 32 px. Użycie: swift scripts/make-ime-icon.swift branding/ime-icon.tiff
import AppKit

let output = CommandLine.arguments.dropFirst().first ?? "branding/ime-icon.tiff"
let image = NSImage(size: NSSize(width: 16, height: 16))
for scale in [1, 2] {
    let pixels = 16 * scale
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                                     samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                     bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }
    rep.size = NSSize(width: 16, height: 16)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let config = NSImage.SymbolConfiguration(pointSize: 13, weight: .semibold)
    if let symbol = NSImage(systemSymbolName: "theatermasks.fill", accessibilityDescription: nil)?.withSymbolConfiguration(config) {
        let size = symbol.size
        let rect = NSRect(x: (16 - size.width) / 2, y: (16 - size.height) / 2, width: size.width, height: size.height)
        symbol.draw(in: rect)
        // Szablon: kolor czarny, kształt z kanału alfa.
        NSColor.black.set()
        rect.fill(using: .sourceAtop)
    }
    NSGraphicsContext.restoreGraphicsState()
    image.addRepresentation(rep)
}
guard let tiff = image.tiffRepresentation else { exit(1) }
try tiff.write(to: URL(fileURLWithPath: output))
print("✓ \(output)")
