import Foundation
import Vision
import AppKit
// usage: ocr <image.png> -> prints recognized lines top-to-bottom
let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(1) }
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
let obs = (req.results ?? []).sorted { a, b in
    let dy = a.boundingBox.midY - b.boundingBox.midY
    if abs(dy) > 0.008 { return dy > 0 }
    return a.boundingBox.minX < b.boundingBox.minX
}
// group into lines by y
var lines: [[VNRecognizedTextObservation]] = []
for o in obs {
    if var last = lines.last, abs(last[0].boundingBox.midY - o.boundingBox.midY) < 0.008 {
        last.append(o); lines[lines.count-1] = last
    } else { lines.append([o]) }
}
for l in lines {
    print(l.sorted{ $0.boundingBox.minX < $1.boundingBox.minX }
        .compactMap { $0.topCandidates(1).first?.string }.joined(separator: "   "))
}
