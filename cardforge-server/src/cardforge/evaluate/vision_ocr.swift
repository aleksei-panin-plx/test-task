import Foundation
import Vision

// One process for the batch. No network, language correction or confidence filtering.
let input = FileHandle.standardInput.readDataToEndOfFile()
let paths = try JSONDecoder().decode([String].self, from: input)
var rows: [[String: Any]] = []
for path in paths {
    do {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.recognitionLanguages = ["en-US", "ru-RU", "zh-Hans"]
        let handler = VNImageRequestHandler(url: URL(fileURLWithPath: path), options: [:])
        try handler.perform([request])
        let words: [[String: Any]] = (request.results ?? []).compactMap { observation in
            guard let text = observation.topCandidates(1).first else { return nil }
            let box = observation.boundingBox
            return ["text": text.string, "confidence": text.confidence,
                    "bbox_bottom_left": [box.origin.x, box.origin.y, box.width, box.height]]
        }
        rows.append(["path": path, "status": "ok", "observations": words,
                     "revision": request.revision])
    } catch {
        rows.append(["path": path, "status": "unavailable", "error": String(describing: error)])
    }
}
let output = try JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys])
FileHandle.standardOutput.write(output)
